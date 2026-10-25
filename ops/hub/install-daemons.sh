#!/bin/sh
# Move the hub's four services from per-user LaunchAgents to system
# LaunchDaemons so they start at boot without a login (#239 / 239ec238),
# install the autocut launchd timer (#258) when the in-process host is not
# enabled, and then run the backup agent's own installer as the owner
# (ops/hub/install-backup-agent.sh; decision c68ec655 — the backup stays a
# per-user LaunchAgent on purpose, see docs/ops/hub-daemons.md "DB backups").
# This is the services' deploy path: every run restarts all four services.
# To (re)prove only the backup, run install-backup-agent.sh by itself.
# Idempotent. Run:  sudo ops/hub/install-daemons.sh [--wait]
# Refuses (exit 75) while an autocut is running; --wait waits up to 15 min.
# Rollback:        sudo ops/hub/uninstall-daemons.sh
# See docs/ops/hub-daemons.md. Unit lists and helpers: launchdaemons/lib.sh.

# (the argument check comes before the sudo re-exec: a typo must not cost a password prompt)
case "${1:-}" in --wait|"") ;; *) echo "unknown argument '$1' (only --wait is accepted)" >&2; exit 64 ;; esac
[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"

unset CDPATH   # before the `cd` below: a caller's CDPATH would make it print and wander
unset SNOWLINE_OWNER_HOME   # lib.sh honours it for the backup script's sake; an installer is the OWNER's
# shellcheck disable=SC1091
. "$(cd "$(dirname "$0")" && pwd)/launchdaemons/lib.sh"
autocut_guard "$@"

STAMP="$(date +%Y%m%d)"
fail=0
svc_fail=0   # the services only: what gates the backup step below
# The backup step at the end (install-backup-agent.sh) only works from the
# checkout the agent's plist names: say so NOW, not after the services have
# been cycled.
backup_here=yes
backup_canonical_here || { echo "note: $BACKUP_GUARD_WHY; the backup step at the end is skipped" >&2; backup_here=no; }

# --- the services: per-user agent -> system daemon ---
# The daemon plist goes on disk first (nothing is torn down before its
# replacement exists), then the agent is stopped, waited for and moved
# aside, then the daemon is loaded (RunAtLoad + KeepAlive: the bootstrap
# starts it; a kickstart would only kill it mid-startup).
for svc in $SERVICES; do
  label="dev.snowline.$svc"
  echo "== $label"
  # the platform service hosts the in-process autocut: a cut that began in
  # the moments since autocut_guard must not be killed by this restart —
  # re-check right before touching it; skip and say which case it is
  if [ "$svc" = platform ]; then
    case "$(lock_state "$AUTOCUT_LOCK")" in
      free) ;;
      held) echo "   an autocut started since the guard; NOT restarting $label mid-cut, nor the other services on top of the old platform — re-run once it is done" >&2; fail=1; svc_fail=1; break ;;
      *) echo "   cannot probe $AUTOCUT_LOCK (perl missing? unreadable?); NOT restarting $label blind, nor the other services — fix that and re-run" >&2; fail=1; svc_fail=1; break ;;
    esac
  fi
  place_unit "$LAUNCHDAEMONS_SRC/$label.plist" "$DAEMONS_DIR/$label.plist" root:wheel || { fail=1; svc_fail=1; continue; }
  # stop the agent (ignore: it may not be loaded) and wait for it to be
  # gone — a service runs its whole startup before binding, so the daemon
  # must never start while the agent is still shutting down ...
  # ... its plist moved aside at once, so that whatever happens next only
  # the daemon can come up at the next boot/login (never both) ...
  if [ -f "$AGENTS/$label.plist" ]; then
    mv "$AGENTS/$label.plist" "$AGENTS/$label.plist.disabled-$STAMP"
    echo "   agent plist -> $label.plist.disabled-$STAMP"
  fi
  # ... and the daemon started only once the agent is really gone. launchd
  # SIGKILLs the agent after its exit timeout, so the registration goes
  # within seconds of that; a teardown still registered after bootout_wait's
  # 60 s gets a further two minutes, with a line, because giving up here
  # leaves the service with NOTHING loaded until a re-run or a boot.
  if ! bootout_wait "gui/$OWNER_UID/$label"; then
    echo "   agent $label still tearing down after 60s; waiting up to 2 more minutes rather than leave $svc unserved"
    if ! wait_unloaded "gui/$OWNER_UID/$label" 120; then
      echo "   agent $label still registered after 3 min; NOT starting the daemon on top of it — once it is gone NOTHING serves $svc until this install is re-run (or the next boot loads the daemon plist, which is in place)" >&2
      fail=1; svc_fail=1
      continue
    fi
  fi
  load_unit "$label" system "$DAEMONS_DIR/$label.plist" norecord || { fail=1; svc_fail=1; }
done

# --- system-only timers (autocut): bootstrapped, never kickstarted ---
# The launchd autocut timer is the ALTERNATIVE to the in-process autocut host
# (docs "Auto-cut"); when the host is enabled (~/.config/snowline/
# autocut.enabled) the timer is not installed, or two cutters would fire each
# hour. The timer is for a hub that runs the host disabled on purpose.
for timer in $TIMERS; do
  label="dev.snowline.$timer"
  echo "== $label (timer; not kickstarted)"
  # (autocut.disabled, the kill switch, pauses the host but does not make
  # room for the timer: lifted, both would fire — so the timer is never
  # installed while the host is enabled, paused or not)
  if [ "$timer" = autocut ] && [ -e "$OWNER_HOME/.config/snowline/autocut.enabled" ]; then
    if [ -e "$OWNER_HOME/.config/snowline/autocut.disabled" ]; then paused=" (paused by autocut.disabled for now)"; else paused=""; fi
    if launchctl print "system/$label" >/dev/null 2>&1; then
      echo "   in-process autocut host is enabled$paused AND this timer is loaded: two cutters — remove one (docs 'Auto-cut')" >&2
      fail=1
    elif [ -f "$DAEMONS_DIR/$label.plist" ]; then
      # a stale plist would be loaded again at the next boot, next to the host
      rm -f "${DAEMONS_DIR:?}/${label:?}.plist"
      echo "   in-process autocut host is enabled; removed the stale (unloaded) timer plist $DAEMONS_DIR/$label.plist"
    else
      echo "   in-process autocut host is enabled; timer not installed"
    fi
    continue
  fi
  if unit_unchanged "$label" system "$LAUNCHDAEMONS_SRC/$label.plist" "$DAEMONS_DIR/$label.plist"; then
    echo "   unchanged, confirmed loaded; left as it is"
    continue
  fi
  # autocut was refused up front by autocut_guard; a cut that started since
  # is a plain check here — still running means skip, never kill it
  if timer_skip "$timer" "the install"; then fail=1; continue; fi
  place_unit "$LAUNCHDAEMONS_SRC/$label.plist" "$DAEMONS_DIR/$label.plist" root:wheel || { fail=1; continue; }
  load_unit "$label" system "$DAEMONS_DIR/$label.plist" || fail=1
done

echo "== waiting up to 60s for /health"
urls="platform|http://100.81.176.75:8850/health pm|http://127.0.0.1:8802/health governance|http://127.0.0.1:8801/health memory|http://127.0.0.1:8803/health"
for entry in $urls; do
  name="${entry%%|*}"
  url="${entry#*|}"
  i=0
  ok=no
  while [ "$i" -lt 60 ]; do
    if curl -fsS -m 2 -o /dev/null "$url" 2>/dev/null; then ok=yes; break; fi
    sleep 1
    i=$((i + 1))
  done
  echo "   $name $url: $([ "$ok" = yes ] && echo UP || echo DOWN)"
  [ "$ok" = yes ] || { fail=1; svc_fail=1; }
done

# --- the backup agent: its own installer, as the owner ---
# After the services are up, so its proving run cannot land in a boot
# migration's window. A per-user agent on purpose (docs "DB backups"); the
# installer is idempotent and is also the thing to run by itself, without
# sudo, whenever the backup needs (re)proving.
# Its outcome does not alter a non-zero services/timers result (an
# automation wrapper keys on that); when they are fine and the backup step
# is not, the exit is 3 — distinct, never silently green. Run with --deploy: a backup in progress is skipped rather
# than waited on, and the proof is bounded more tightly, so a service
# rollout is never held for long by the backup step.
echo "== dev.snowline.backup (ops/hub/install-backup-agent.sh, as the owner)"
if [ "$svc_fail" != 0 ]; then
  # the ordering exists so the proving dump never lands in a boot
  # migration's window; with a service DOWN that is where it would land
  echo "== backup agent: SKIPPED (a service is DOWN or failed to install; its proving dump must not land in a migration) — run ops/hub/install-backup-agent.sh as the owner once /health is clean" >&2
elif [ "$backup_here" = no ]; then
  # (not green: a moved checkout means the unit fails with EX_CONFIG every hour)
  echo "== backup agent: NOT CHECKED (see the note at the top) — the unit may be failing every hour; run ops/hub/install-backup-agent.sh from the canonical checkout" >&2
  backup_fail=1
elif sudo -u "#$OWNER_UID" -H /bin/sh "$OPS_HUB/install-backup-agent.sh" --deploy; then
  echo "== backup agent: OK"
else
  echo "== backup agent: NOT OK (see above) — fix and re-run ops/hub/install-backup-agent.sh as the owner; the services and timers are fine (exit 3 says exactly that)" >&2
  backup_fail=1
fi
# exit: the services' and timers' own result when it is non-zero; else 3
# when the backup step failed (a deploy that leaves the hourly backup
# broken must not read as green); else 0
[ "$fail" -eq 0 ] || exit "$fail"
[ "${backup_fail:-0}" -eq 0 ] || exit 3
exit 0
