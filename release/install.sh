#!/bin/sh
# Snowline packaged-spoke bootstrap (macOS distribution spec §5, issue #203).
#
#   curl -fsSL https://github.com/snowlinedev/Snowline/releases/latest/download/install.sh | sh
#
# This script is deliberately THIN — a public asset needing no auth, run
# exactly once per machine. Everything past "does `snowline` exist yet" is
# `snowline stack sync`'s job (spec §5): install.sh only gets ONE service
# (the platform, which carries the `snowline` CLI itself) far enough onto
# PATH to hand off.
#
# POSIX sh on purpose (curl | sh has no shell of its own to insist on) — no
# bashisms: no arrays, no `[[`, no `local`, no `function` keyword.
#
# Not yet a release asset as of train v0.1.0 (the release-pipeline item
# a0ef1bd4 predates this one) — for that train the operator uploads this
# file to the release by hand. It ships automatically from the next train
# the cutter builds.

set -eu

PLATFORM_REPO="snowlinedev/Snowline"
APP_SUPPORT="$HOME/Library/Application Support/Snowline"
VENV_ROOT="$APP_SUPPORT/venvs/platform"
LOCAL_BIN="$HOME/.local/bin"

log() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# -- 1. prerequisites --------------------------------------------------------

case "$(uname -s)" in
  Darwin) ;;
  *) die "this installer is macOS-only (macOS distribution spec §1)" ;;
esac

command -v brew >/dev/null 2>&1 || die \
  "Homebrew is required — install it from https://brew.sh, then re-run this script"

if ! brew list postgresql@16 >/dev/null 2>&1; then
  die "postgresql@16 is required — run: brew install postgresql@16 && brew services start postgresql@16, then re-run this script"
fi

command -v gh >/dev/null 2>&1 || die \
  "the GitHub CLI (gh) is required — install it (brew install gh), run: gh auth login, then re-run this script"

if ! gh auth status >/dev/null 2>&1; then
  die "gh is not authenticated — run: gh auth login (the pm wheel fetch needs it — its repo is private), then re-run this script"
fi

# -- 2. uv + a managed Python 3.12 -------------------------------------------

if ! command -v uv >/dev/null 2>&1; then
  log "installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || die "uv install did not put uv on PATH — check $HOME/.local/bin"

log "ensuring a uv-managed Python 3.12..."
uv python install 3.12

# -- 3. fetch the platform service's release assets for the latest train ----

train="$(gh release view --repo "$PLATFORM_REPO" --json tagName --jq .tagName)"
[ -n "$train" ] || die "could not resolve the latest release on $PLATFORM_REPO"
log "bootstrapping the platform service at train $train"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
gh release download "$train" --repo "$PLATFORM_REPO" \
  --pattern "snowline_platform-*.whl" \
  --pattern "snowline_plugin_sdk-*.whl" \
  --pattern "requirements-platform.txt" \
  --dir "$work" --clobber

platform_wheel="$(ls "$work"/snowline_platform-*.whl 2>/dev/null | head -n1)"
sdk_wheel="$(ls "$work"/snowline_plugin_sdk-*.whl 2>/dev/null | head -n1)"
[ -n "$platform_wheel" ] || die "no platform wheel found in the $train release assets"
[ -n "$sdk_wheel" ] || die "no sdk wheel found in the $train release assets"

# -- 4. build the platform service venv for this train -----------------------

venv_dir="$VENV_ROOT/$train"
mkdir -p "$VENV_ROOT"
uv venv --python 3.12 "$venv_dir"
uv pip install --python "$venv_dir/bin/python" \
  "$platform_wheel" "$sdk_wheel" \
  -r "$work/requirements-platform.txt" \
  --find-links "$work"

# -- 5. point venvs/platform/current at it. NOT the sync-style atomic rename:
# BSD `mv -f` onto an existing symlink-to-directory moves the temp INTO the
# old venv instead of replacing the link (#210 review — a re-run silently
# stayed on the old train). remove+ln has a momentary no-`current` window,
# which is harmless here: nothing runs from this venv during bootstrap
# (sync's os.replace handles the live case). -------------------------------

rm -f "$VENV_ROOT/current"
ln -s "$venv_dir" "$VENV_ROOT/current"

# -- 6. symlink ~/.local/bin/snowline THROUGH current (spec §5 — never at a
# train-versioned path, or a later sync repoints `current` while PATH stays
# pinned to the bootstrap venv and the keep-2 GC eventually deletes it) -----

mkdir -p "$LOCAL_BIN"
tmp_snowline="$LOCAL_BIN/snowline.tmp-new"
rm -f "$tmp_snowline"
ln -s "$VENV_ROOT/current/bin/snowline" "$tmp_snowline"
mv -f "$tmp_snowline" "$LOCAL_BIN/snowline"

case ":$PATH:" in
  *":$LOCAL_BIN:"*) ;;
  *) log "note: $LOCAL_BIN is not on PATH — add it to your shell profile" ;;
esac

# -- 7. hand off to `snowline stack sync` — it owns everything else ---------

log "handing off to snowline stack sync..."
exec "$LOCAL_BIN/snowline" stack sync --role spoke "$@"
