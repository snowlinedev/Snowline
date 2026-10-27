#!/bin/sh
# Install (or reload) the hourly DB backup agent and prove it with one run.
# Run as the hub owner, from the canonical checkout, no sudo:
#   ops/hub/install-backup-agent.sh
# Exit 0 only when the proving run logged "run exit 0".
set -u
label="dev.snowline.backup"
domain="gui/$(id -u)"
here="$(cd "$(dirname "$0")" && pwd)"
src="$here/launchagents/$label.plist"
dst="$HOME/Library/LaunchAgents/$label.plist"
log_file="$HOME/Library/Logs/snowline-backup.log"
lock="$HOME/Library/Application Support/Snowline/backup.lock.d"

[ "$(id -u)" != 0 ] || { echo "run as the hub owner, not root: the agent is per-user" >&2; exit 64; }
# launchd runs the script path named in the plist; it must be this checkout's.
script="$(plutil -extract ProgramArguments.1 raw -o - "$src")" || { echo "cannot read $src" >&2; exit 1; }
[ "$here/snowline-backup.sh" -ef "$script" ] || { echo "the plist runs $script; run this from that checkout" >&2; exit 1; }
# Never boot the unit out mid-dump.
holder="$(cat "$lock/pid" 2>/dev/null)"
if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
    echo "a backup run is in progress; re-run when it finishes" >&2; exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
cp "$src" "$dst"
launchctl bootout "$domain/$label" 2>/dev/null
i=0; while launchctl print "$domain/$label" >/dev/null 2>&1 && [ "$i" -lt 30 ]; do sleep 1; i=$((i + 1)); done
launchctl bootstrap "$domain" "$dst" || { echo "bootstrap refused (no login session for $domain?)" >&2; exit 1; }

# Prove it: fire one run through launchd (its own session) and wait for the
# run's exit line in the log.
start="$(date -u +%FT%TZ)"
sleep 1
launchctl kickstart "$domain/$label" || { echo "kickstart refused" >&2; exit 1; }
echo "proving run fired; waiting for it (up to 10 min)"
i=0
while [ "$i" -lt 600 ]; do
    code="$(awk -v s="$start" '$1 > s && $2 == "run" && $3 == "exit" { c = $4 } END { print c }' "$log_file" 2>/dev/null)"
    [ -n "$code" ] && break
    sleep 2; i=$((i + 2))
done
tail -n 10 "$log_file" | sed 's/^/   /'
case "$code" in
    0) echo "OK: $label loaded and its proving run dumped every store" ;;
    "") echo "no result after 10 min; check $log_file and 'launchctl print $domain/$label'" >&2; exit 1 ;;
    *) echo "FAILED: the proving run exited $code; see $log_file" >&2; exit 1 ;;
esac
