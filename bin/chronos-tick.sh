#!/bin/bash
# Chronos tick. launchd runs this every 5 minutes (io.github.chronos.tick).
#
# For each job that is due (enabled, scheduled today, past fire time + grace, inside its catch-up window,
# not already ran / claimed / failed for the day, not paused): claim it atomically (mkdir) and start
# chronos-run.sh in the background. The grace period gives a live interactive Claude Code session first
# shot at the job; Chronos is the durable backstop.
#
#   Pause everything:  touch <home_dir>/PAUSED           (default ~/.chronos/PAUSED)
#   Pause one job:     touch <home_dir>/paused-jobs/<id>
#   Test overrides:    CHRONOS_CONFIG=<alt config.json>  CHRONOS_NOW=2026-10-05T09:00
#                      CHRONOS_DRYRUN=1  (print what would run, claim nothing, spawn nothing)
#
# After the clock jobs, an EVENT pass (v0.2.0) evaluates the triggers of every job that has any (file, gmail, github,
# webhook) and starts one run per event, with the same atomic claim. Event runs are untrusted: see chronos-run.sh.
#
# If jobs.json is missing or unparsable the tick logs the error and does nothing (never crash-loops).
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PYBIN="$(command -v python3 || echo /usr/bin/python3)"
# v0.3.0 (N3): every Python step runs as a child under a watchdog that kill -9s it from outside after CH_STEP_DEADLINE_S
# (see chronos-deadline.sh). A step stuck in the kernel (a privacy prompt nobody sees) then costs one tick, never the scheduler.
. "$DIR/chronos-deadline.sh"
ENVOUT="$(ch_step 60 env "$PYBIN" "$DIR/chronos" env)" || { echo "chronos: cannot read config" >&2; exit 0; }
eval "$ENVOUT"
mkdir -p "$CH_STATE" "$CH_LOGS" "$CH_HOME"
DRY="${CHRONOS_DRYRUN:-}"
[ -z "$DRY" ] && date "+%Y-%m-%dT%H:%M:%S" > "$CH_HOME/chronos.beat"

DUE=$(ch_step "$CH_STEP_DEADLINE_S" due "$PYBIN" "$DIR/chronos" due 2>>"$CH_LOGS/chronos.log")
RC=$?
if [ "$RC" -ne 0 ]; then
  if [ "$RC" -eq 137 ]; then
    echo "$(date '+%F %T') TICK-ERROR the due check was killed at its deadline; this tick does nothing, the next one tries again" >> "$CH_LOGS/chronos.log"
  else
    echo "$(date '+%F %T') TICK-ERROR jobs file missing or unparsable ($CH_JOBS_FILE); doing nothing" >> "$CH_LOGS/chronos.log"
  fi
  exit 0
fi

while IFS='|' read -r id ds; do
  [ -n "$id" ] || continue
  if [ -n "$DRY" ]; then echo "DRYRUN would claim $id ($ds)"; continue; fi
  # claim: atomic mkdir, stale claims swept first (same rule as `chronos claim`)
  [ "$(ch_step "$CH_STEP_DEADLINE_S" claim "$PYBIN" "$DIR/chronos" claim "$id")" = "CLAIMED" ] || continue
  echo "$(date '+%F %T') CLAIMED $id -> spawning run" >> "$CH_LOGS/chronos.log"
  nohup /bin/bash "$DIR/chronos-run.sh" "$id" "$ds" >> "$CH_LOGS/chronos.log" 2>&1 &
done <<< "$DUE"

# ---- session-wait notifications (v0.3.0, S7.4). A session job that has waited session_wait_notify_min with no live session
# is announced once per (id, ds) through the `notify` command, with CHRONOS_JOB exported so the command can pick the job's thread.
if [ -z "$DRY" ]; then
  WAITS=$(ch_step "$CH_STEP_DEADLINE_S" wait-notify "$PYBIN" "$DIR/chronos" runlog wait-notify 2>>"$CH_LOGS/chronos.log") || WAITS=""
  while IFS='|' read -r wid wds wmsg; do
    [ -n "$wid" ] && [ -n "$wmsg" ] || continue
    echo "$(date '+%F %T') WAITING $wid ($wds): no live session; notifying" >> "$CH_LOGS/chronos.log"
    CHRONOS_JOB="$wid" CHRONOS_EVENT=session_wait "$DIR/chronos-notify" "$wmsg" >/dev/null 2>&1 || true
  done <<< "$WAITS"
fi

# ---- event pass. Runs AFTER the clock jobs are claimed, so a slow Gmail/GitHub poll never delays a scheduled job
# (the Python side has a 150 s hard stop). Each stdout line is  id|eventhash|eventfile|type.
if [ -n "$DRY" ]; then
  ch_step "$CH_TRIGGER_DEADLINE_S" triggers "$PYBIN" "$DIR/chronos" triggers --dry 2>>"$CH_LOGS/chronos.log" || true
elif [ -e "$CH_HOME/headless.blocked" ]; then
  # headless.blocked: no headless Claude runs (an event run is one) until Retry clears it. Command jobs are clock jobs and already ran above.
  echo "$(date '+%F %T') TRIGGERS skipped: headless runs are blocked (see headless.blocked)" >> "$CH_LOGS/chronos.log"
else
  HITS=$(ch_step "$CH_TRIGGER_DEADLINE_S" triggers "$PYBIN" "$DIR/chronos" triggers 2>>"$CH_LOGS/chronos.log") || HITS=""
  while IFS='|' read -r tid thash tfile ttype; do
    [ -n "$tid" ] && [ -n "$thash" ] || continue
    case "$thash" in *[!a-f0-9]*) continue ;; esac
    [ "$(ch_step "$CH_STEP_DEADLINE_S" claim-event "$PYBIN" "$DIR/chronos" claim-event "$tid" "$thash")" = "CLAIMED" ] || { echo "$(date '+%F %T') TRIGGER claim busy $tid $thash" >> "$CH_LOGS/chronos.log"; continue; }
    echo "$(date '+%F %T') CLAIMED $tid TRIGGER $ttype $thash -> spawning event run" >> "$CH_LOGS/chronos.log"
    nohup /bin/bash "$DIR/chronos-run.sh" "$tid" "$(ch_step "$CH_STEP_DEADLINE_S" today "$PYBIN" "$DIR/chronos" today)" "$tfile" "$thash" "$ttype" >> "$CH_LOGS/chronos.log" 2>&1 &
  done <<< "$HITS"
fi
exit 0
