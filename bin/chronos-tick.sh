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
ENVOUT="$("$PYBIN" "$DIR/chronos" env)" || { echo "chronos: cannot read config" >&2; exit 0; }
eval "$ENVOUT"
mkdir -p "$CH_STATE" "$CH_LOGS" "$CH_HOME"
DRY="${CHRONOS_DRYRUN:-}"
[ -z "$DRY" ] && date "+%Y-%m-%dT%H:%M:%S" > "$CH_HOME/chronos.beat"

if ! DUE=$("$PYBIN" "$DIR/chronos" due 2>>"$CH_LOGS/chronos.log"); then
  echo "$(date '+%F %T') TICK-ERROR jobs file missing or unparsable ($CH_JOBS_FILE); doing nothing" >> "$CH_LOGS/chronos.log"
  exit 0
fi

while IFS='|' read -r id ds; do
  [ -n "$id" ] || continue
  if [ -n "$DRY" ]; then echo "DRYRUN would claim $id ($ds)"; continue; fi
  # claim: atomic mkdir, stale claims swept first (same rule as `chronos claim`)
  [ "$("$PYBIN" "$DIR/chronos" claim "$id")" = "CLAIMED" ] || continue
  echo "$(date '+%F %T') CLAIMED $id -> spawning run" >> "$CH_LOGS/chronos.log"
  nohup /bin/bash "$DIR/chronos-run.sh" "$id" "$ds" >> "$CH_LOGS/chronos.log" 2>&1 &
done <<< "$DUE"

# ---- event pass. Runs AFTER the clock jobs are claimed, so a slow Gmail/GitHub poll never delays a scheduled job
# (the Python side has a 150 s hard stop). Each stdout line is  id|eventhash|eventfile|type.
if [ -n "$DRY" ]; then
  "$PYBIN" "$DIR/chronos" triggers --dry 2>>"$CH_LOGS/chronos.log" || true
else
  HITS=$("$PYBIN" "$DIR/chronos" triggers 2>>"$CH_LOGS/chronos.log") || HITS=""
  while IFS='|' read -r tid thash tfile ttype; do
    [ -n "$tid" ] && [ -n "$thash" ] || continue
    case "$thash" in *[!a-f0-9]*) continue ;; esac
    [ "$("$PYBIN" "$DIR/chronos" claim-event "$tid" "$thash")" = "CLAIMED" ] || { echo "$(date '+%F %T') TRIGGER claim busy $tid $thash" >> "$CH_LOGS/chronos.log"; continue; }
    echo "$(date '+%F %T') CLAIMED $tid TRIGGER $ttype $thash -> spawning event run" >> "$CH_LOGS/chronos.log"
    nohup /bin/bash "$DIR/chronos-run.sh" "$tid" "$("$PYBIN" "$DIR/chronos" today)" "$tfile" "$thash" "$ttype" >> "$CH_LOGS/chronos.log" 2>&1 &
  done <<< "$HITS"
fi
exit 0
