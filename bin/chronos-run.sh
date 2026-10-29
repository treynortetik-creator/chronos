#!/bin/bash
# Chronos runner: one job, headless, already claimed (by chronos-tick.sh, the UI or `chronos run`).
#
#   chronos-run.sh <job-id> [YYYY-MM-DD]                          a clock (trusted) run
#   chronos-run.sh <job-id> <YYYY-MM-DD> <event-file> <hash> <type>   an event (UNTRUSTED) run
#
# THE 409 LESSON: a headless `claude` that loads the Telegram channel plugin starts a SECOND getUpdates
# poller on the same bot token. Telegram answers HTTP 409 (conflict) and the live session's channel
# dies. So every headless run disables those plugins through --settings (config: disable_plugins).
# Delivery from here is a plain command (config: notify), which never conflicts with polling.
#
# EVENT RUNS read text from outside (an email, a pull request, a webhook body, a file name), so that text is
# untrusted and the child NEVER gets --dangerously-skip-permissions (and none of claude_args, which may
# contain it). It runs with --permission-mode=default, a narrow --allowedTools list (job setting
# `allowed_tools`, default Read/Grep/Glob plus the notify sender), --disallowedTools for secret paths (deny
# beats allow), --tools limited to the built-ins that list needs and, unless the list names an MCP tool,
# --strict-mcp-config so no MCP server loads. The `=` flag forms matter: --tools and --allowedTools are
# variadic and would otherwise swallow the prompt argument.
#
# RESTRICTED CLOCK RUNS (v0.2.2): a job with "restricted": true gets the same narrow treatment on its scheduled runs
# (--permission-mode=default, its allowed_tools, --tools, the secret-path denies, --strict-mcp-config, no claude_args,
# so never --dangerously-skip-permissions). The default stays unchanged: a job without the field runs with claude_args
# exactly as before. A restricted run cannot write its own report or marker, so Chronos does it from the final message
# (the same way it does for an event run).
#
# Every run uses --output-format=stream-json so chronos_runlog.py can record tokens, cost and the rate-limit
# readings (runs.jsonl, rate-limits.json) and turn the stream into the plain-text log.
#
# COMMAND JOBS (v0.2.1): a job with "kind": "command" runs its "command" string through `/bin/bash -c` in the workspace
# instead of starting claude. Same claim, watchdog, log, notify and done-marker; no prompt, no tokens. Success is
# exit 0 inside the watchdog. A command job never runs on an event.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
umask 077
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PYBIN="$(command -v python3 || echo /usr/bin/python3)"
# v0.3.0 (N3): every Python step runs as a child under a kill -9 watchdog (see chronos-deadline.sh), so a step stuck in the kernel
# (a privacy prompt nobody sees) costs this run, never a hung process that holds its claim forever.
. "$DIR/chronos-deadline.sh"
ENVOUT="$(ch_step 60 env "$PYBIN" "$DIR/chronos" env)" || exit 1      # (not `eval "$(...)" || ...`: eval of an empty string succeeds)
eval "$ENVOUT"
chx() { ch_step "${CH_STEP_DEADLINE_S:-60}" "$1" "$PYBIN" "$DIR/chronos" "$@"; }
[ -n "$CH_PATH_EXTRA" ] && export PATH="$CH_PATH_EXTRA:$PATH"
export CHRONOS_RUN=1            # tells the optional SessionStart hook to stay silent inside this run

id="${1:-}"; D="${2:-$(date +%Y-%m-%d)}"
EVFILE="${3:-}"; EHASH="${4:-}"; ETYPE="${5:-}"; EVENT=""
[ -n "$EHASH" ] && EVENT=1
case "$id" in ""|*[!a-z0-9-]*) echo "bad job id"; exit 1 ;; esac
case "$EHASH" in *[!a-f0-9]*) echo "bad event hash"; exit 1 ;; esac
case "$ETYPE" in *[!a-z]*) echo "bad event type"; exit 1 ;; esac
case "$D" in [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;; *) echo "bad date"; exit 1 ;; esac
mkdir -p "$CH_STATE/reports" "$CH_LOGS"
LOG="$CH_LOGS/$id-$D-$(date +%H%M).log"
REPORT="$CH_STATE/reports/$id-$D.md"
RAN="$CH_STATE/ran-$id-$D"; CLAIM="$CH_STATE/claim-$id-$D"; FAILED="$CH_STATE/failed-$id-$D"
if [ -n "$EVENT" ]; then
  # an event run has its own claim, done-marker, log and report, and never touches the clock run's markers
  LOG="$CH_LOGS/$id-$D-$(date +%H%M)-$EHASH.log"
  REPORT="$CH_STATE/reports/$id-$D-$EHASH.md"
  RAN="$RAN-$EHASH"; CLAIM="$CLAIM-$EHASH"
fi
POLL="${CHRONOS_POLL_S:-10}"   # watchdog poll interval (tests shorten it)
export CHRONOS_JOB="$id" CHRONOS_LOG="$LOG" CHRONOS_REPORT="$REPORT"

notify() { CHRONOS_EVENT="$1" ch_step "${CH_STEP_DEADLINE_S:-60}" notify "$DIR/chronos-notify" "$2" >/dev/null 2>&1 || true; }     # a notify command is a network call: bounded, from outside
release_claim() { rm -f "$CLAIM.owner.json"; rmdir "$CLAIM" 2>/dev/null; }     # the sibling owner file goes before the directory
# N1: a step of this script killed at its deadline (rc 137) is a launch stall, not a failed job: the day gets `stalled-` (terminal for the tick),
# headless is blocked, and the owner reruns by hand. No automatic retry.
stall_early() {
  echo "$(date '+%F %T') STALLED $id $1"
  [ -z "$EVENT" ] && touch "$CH_STATE/stalled-$id-$D"
  chx runlog stall --id "$id" --ds "$D" --run-id "${RUN_ID:-}" ${EVENT:+--hash "$EHASH"} >/dev/null 2>&1 || true
  [ -e "$CH_HOME/headless.blocked" ] || printf '{"reason":"launch_stalled","id":"%s","ds":"%s","source":"watchdog","tcc_suspected":false}\n' "$id" "$D" > "$CH_HOME/headless.blocked" 2>/dev/null
  [ "${MODE:-failure}" = "never" ] || notify job_stalled "Chronos: $id could not start: a step of the run timed out ($1), usually a macOS privacy prompt nobody can see. Background runs are paused until you retry them."
  release_claim; exit 1
}
fail_early() { echo "$(date '+%F %T') FAIL $id $1"; [ -z "$EVENT" ] && touch "$FAILED"; release_claim; exit 1; }

# a failed or stalled day is terminal for every automatic start; only Run now (which clears the marker before it spawns us) re-runs it
if [ -z "$EVENT" ] && { [ -e "$FAILED" ] || [ -e "$CH_STATE/stalled-$id-$D" ]; }; then echo "$(date '+%F %T') SKIP $id failed or stalled day"; release_claim; exit 0; fi
# idempotent: a relaunch must never redo a finished job
[ -f "$RAN" ] && { echo "$(date '+%F %T') SKIP $id already ran"; release_claim; exit 0; }
[ -d "$CH_WORKSPACE" ] || fail_early "workspace missing: $CH_WORKSPACE"

# v0.3.0: the run id (r_<ULID>; CHRONOS_RUN_ID when `chronos api start` already minted it) and, for a clock run, the claim's owner file
TRIGGER="${CHRONOS_TRIGGER:-clock}"; [ -n "$EVENT" ] && TRIGGER=event
RUN_ID="$(chx runlog open --id "$id" --ds "$D" --trigger-kind "$TRIGGER" --pid "$$" ${EVENT:+--hash "$EHASH"})" || RUN_ID=""
STATUS="$CH_STATE/run-$id-$D${EVENT:+-$EHASH}.json"
STARTED="$(date '+%Y-%m-%dT%H:%M:%S%z' | sed -E 's/(..)$/:\1/')"
write_status() {  # write_status <running|ok|failed|timed_out|launch_stalled>: atomic, refreshed on every watchdog poll
  printf '{"run_id":"%s","job":"%s","ds":"%s","status":"%s","pid":%s,"started_at":"%s","updated_at":"%s","bytes":%s}\n' \
    "$RUN_ID" "$id" "$D" "$1" "${pid:-0}" "$STARTED" "$(date '+%Y-%m-%dT%H:%M:%S%z' | sed -E 's/(..)$/:\1/')" "${BYTES:-0}" > "$STATUS.tmp.$$" 2>/dev/null \
    && mv "$STATUS.tmp.$$" "$STATUS" 2>/dev/null
  return 0
}
tree_cpu() {  # CPU centiseconds used by <pid> and everything below it (ps time is M:SS.ss, H:MM:SS or D-HH:MM:SS)
  ps -A -o pid=,ppid=,time= 2>/dev/null | awk -v root="$1" '
    function cs(t,   a, n, d, x, s, k) { d = 0; if (t ~ /-/) { split(t, x, "-"); d = x[1]; t = x[2] }; n = split(t, a, ":"); s = 0
      for (k = 1; k <= n; k++) s = s * 60 + a[k]; return int((s + d * 86400) * 100) }
    { par[$1] = $2; cpu[$1] = cs($3) }
    END { tot = 0; for (p in par) { q = p; g = 0; while (q != "" && q != 0 && g < 64) { if (q == root) { tot += cpu[p]; break }; q = par[q]; g++ } }; print tot }'
}
KIND="$(chx field "$id" kind)"
CMD=""
if [ "$KIND" = command ]; then
  [ -z "$EVENT" ] || fail_early "a command job cannot run on an event"
  CMD="$(chx field "$id" command)"
  [ -n "$CMD" ] || fail_early "command job has no command"
fi

if [ "$KIND" = command ]; then
  :
elif [ -n "$EVENT" ]; then
  [ -f "$EVFILE" ] || fail_early "event file unreadable: $EVFILE"
  PROMPT="$(chx prompt "$id" --event "$EVFILE" "$ETYPE")" || { [ $? -eq 137 ] && stall_early "event prompt"; fail_early "cannot build the event prompt"; }
else
  PROMPT="$(chx prompt "$id" "$D")" || { [ $? -eq 137 ] && stall_early "prompt"; fail_early "cannot build prompt"; }
fi
MODE="$(chx field "$id" notify)"
# per-job settings: CH_JOB_MODEL and, for events, CH_EV_ALLOWED / CH_EV_TOOLS / CH_EV_DENY / CH_EV_MCP.
# A job that cannot be read is a failure; nothing falls back to a wider tool list.
CH_JOB_MODEL=""; CH_RESTRICTED=0; CH_RSETTINGS=""
if [ "$KIND" != command ]; then
  JOBENV="$(chx jobenv "$id")" || { [ $? -eq 137 ] && stall_early "job settings"; fail_early "cannot read the job's settings"; }
  eval "$JOBENV"
fi

RESTRICTED=""
[ "$KIND" != command ] && [ -z "$EVENT" ] && [ "$CH_RESTRICTED" = "1" ] && RESTRICTED=1
if [ "$KIND" = command ]; then
  args=()
elif [ -n "$EVENT" ] || [ -n "$RESTRICTED" ]; then
  # --setting-sources= : read NO user/project/local settings file. A saved `Bash(curl:*)` (or a whole-MCP-server allow) in
  # ~/.claude/settings.json or the agent's settings.local.json would otherwise be MERGED on top of --allowedTools. The agent's
  # hooks and deny rules come back in through --settings (CH_RSETTINGS, built by chronos_run_settings).
  args=(-p --permission-mode=default --setting-sources= "--allowedTools=$CH_EV_ALLOWED" "--tools=$CH_EV_TOOLS")
  [ -n "$CH_EV_DENY" ] && args+=("--disallowedTools=$CH_EV_DENY")
  [ "$CH_EV_MCP" = "open" ] || args+=(--strict-mcp-config)
else
  args=(-p)
  [ ${#CH_CLAUDE_ARGS[@]} -gt 0 ] && args+=("${CH_CLAUDE_ARGS[@]}")
fi
if [ "$KIND" != command ]; then
  if [ -n "$EVENT" ] || [ -n "$RESTRICTED" ]; then
    [ -n "$CH_RSETTINGS" ] && args+=(--settings "$CH_RSETTINGS")
  else
    [ -n "$CH_SETTINGS" ] && args+=(--settings "$CH_SETTINGS")
  fi
  [ -n "$CH_JOB_MODEL" ] && args+=("--model=$CH_JOB_MODEL")
  args+=(--output-format=stream-json --verbose)
fi

RAW="$CH_LOGS/.$(basename "$LOG").raw"; ERRF="$CH_LOGS/.$(basename "$LOG").err"; T0=$(date +%s)
cd "$CH_WORKSPACE" || exit 1
echo "$(date '+%F %T') START $id${EVENT:+ event $ETYPE $EHASH} log=$LOG"
if [ "$KIND" = command ]; then
  /bin/bash -c "$CMD" > "$RAW" 2> "$ERRF" < /dev/null &
else
  "$CH_CLAUDE" "${args[@]}" "$PROMPT" > "$RAW" 2> "$ERRF" < /dev/null &
fi
pid=$!; waited=0; timed_out=""; stalled=""
# THE LAUNCH WATCHDOG (v0.3.0). This loop lives in the parent, outside the child that may be stuck in the kernel. Until the child has
# written its first byte, no output AND no CPU progress for CH_LAUNCH_TIMEOUT_S means it never launched (a privacy prompt nobody sees):
# kill it by PID, and the run is `stalled`, not `failed`. A command job is exempt (it may be quiet by design).
BYTES=0; CPU=0; IDLE=0; SAWOUT=""
LAUNCHWATCH=1; [ "$KIND" = command ] && LAUNCHWATCH=""
write_status running
while kill -0 "$pid" 2>/dev/null; do
  sleep "$POLL"; waited=$((waited + POLL))
  sz="$(wc -c < "$RAW" 2>/dev/null | tr -d ' ')"; sz="${sz:-0}"
  [ "$sz" -gt 0 ] && SAWOUT=1
  if [ -n "$LAUNCHWATCH" ] && [ -z "$SAWOUT" ]; then
    cpu="$(tree_cpu "$pid")"; cpu="${cpu:-0}"
    if [ "$cpu" != "$CPU" ]; then IDLE=0; else IDLE=$((IDLE + POLL)); fi
    CPU="$cpu"
  fi
  BYTES="$sz"
  write_status running
  if [ -n "$LAUNCHWATCH" ] && [ -z "$SAWOUT" ] && [ "$IDLE" -ge "$CH_LAUNCH_TIMEOUT_S" ]; then
    stalled=1; kids="$(ch_tree "$pid" | tr '\n' ' ')"
    kill -TERM $kids "$pid" 2>/dev/null; sleep 1; kill -KILL $kids "$pid" 2>/dev/null; break
  fi
  if [ "$waited" -ge "$CH_TIMEOUT_S" ]; then
    timed_out=1; pkill -TERM -P "$pid" 2>/dev/null; kill -TERM "$pid" 2>/dev/null; break
  fi
done
if [ -n "$stalled" ]; then rc=137; else wait "$pid" 2>/dev/null; rc=$?; fi     # (never `wait` for a child killed mid-stall: it may not die)

record() {  # $1 = schedule|event. Builds $LOG from the stream and logs usage; any failure leaves the raw stream as the log.
  cp "$RAW" "$LOG" 2>/dev/null
  chx record --run-id "$RUN_ID" --ds "$D" --trigger-kind "$TRIGGER" --claude-bin "$CH_CLAUDE" --launch-stalled "${stalled:-0}" \
    --raw "$RAW" --log "$LOG" --err "$ERRF" --id "$id" --kind "$1" --start "$T0" --end "$(date +%s)" \
    --rc "$rc" --timed-out "$([ -n "$timed_out" ] && echo 1 || echo 0)" --model-flag "$CH_JOB_MODEL" --ttype "$ETYPE" --thash "$EHASH" \
    --marker-path "$RAN" --command "$([ "$KIND" = command ] && echo 1 || echo 0)" --restricted "$([ -n "$RESTRICTED" ] && echo 1 || echo 0)" 2>>"$CH_LOGS/chronos.log"
}

if [ -n "$stalled" ]; then
  # a launch stall: the row says launch_stalled, the day is `stalled-` (never `failed-`, so it is terminal for the tick), headless is blocked
  # until Retry, and the claim is released. record, stall and the notice all run under the external deadline.
  record "$([ -n "$EVENT" ] && echo event || echo schedule)" >/dev/null || true
  chx runlog stall --id "$id" --ds "$D" --run-id "$RUN_ID" ${EVENT:+--hash "$EHASH"} 2>&1 || true
  write_status launch_stalled
  rm -f "$RAW" "$ERRF"
  echo "$(date '+%F %T') STALLED $id rc=$rc (no output and no CPU progress for ${CH_LAUNCH_TIMEOUT_S}s; headless runs are blocked)"
  [ "$MODE" = "never" ] || notify job_stalled "Chronos: $id could not start. Claude produced no output and no CPU progress for ${CH_LAUNCH_TIMEOUT_S}s (a launch stall, usually a macOS privacy prompt nobody can see). Background runs are paused until you retry them. Log: $LOG"
  release_claim
  exit 0
fi

if [ -n "$EVENT" ]; then
  # event run: success = a clean exit inside the watchdog with no error result. Chronos writes the done-marker and the
  # report from the final message; the job itself writes no markers (an event run must not suppress the clock run).
  OKREC="$(record event)" || OKREC=""
  rm -f "$RAW" "$ERRF"
  if [ "$rc" -eq 0 ] && [ -z "$timed_out" ] && [ "$OKREC" = "ok" ]; then
    echo "$(date '+%F %T') OK $id event $EHASH rc=$rc"
    touch "$RAN"; cp "$LOG" "$REPORT" 2>/dev/null
    if [ "$MODE" = "always" ]; then
      body=""; [ -f "$REPORT" ] && body="$(head -c 700 "$REPORT")"
      notify job_ok "Chronos: $id handled a $ETYPE event.${body:+ }$body"
    fi
  else
    echo "$(date '+%F %T') FAIL $id event $EHASH rc=$rc timed_out=${timed_out:-0}"
    [ "$MODE" = "never" ] || notify job_failed "Chronos: $id event run ($ETYPE $EHASH) did not finish (exit $rc${timed_out:+, killed by the watchdog}). Log: $LOG"
  fi
  [ -n "$RUN_ID" ] && [ -f "$REPORT" ] && cp "$REPORT" "$CH_STATE/reports/$id-$D-$RUN_ID.md" 2>/dev/null
  if [ -f "$RAN" ] && [ "$rc" -eq 0 ] && [ -z "$timed_out" ]; then write_status ok; elif [ -n "$timed_out" ]; then write_status timed_out; else write_status failed; fi
  release_claim
  exit 0
fi

# clock run. success = the job wrote its done-marker, or (unless require_marker) the process exited 0 on its own
if [ -n "$RESTRICTED" ]; then
  # restricted run: the job has no way to write a marker or a report. Success = a clean exit inside the watchdog with no
  # error result (require_marker cannot apply: nothing but Chronos may write the marker). Chronos writes the report.
  OKREC="$(record schedule)" || OKREC=""
  # (OKREC must be exactly "ok": a record step that crashed prints nothing, and "nothing" is not success)
  if [ ! -f "$RAN" ] && [ "$rc" -eq 0 ] && [ -z "$timed_out" ] && [ "$OKREC" = "ok" ]; then touch "$RAN"; cp "$LOG" "$REPORT" 2>/dev/null; fi
else
  if [ ! -f "$RAN" ] && { [ -z "$CH_REQUIRE_MARKER" ] || [ "$KIND" = command ]; } && [ "$rc" -eq 0 ] && [ -z "$timed_out" ]; then touch "$RAN"; fi
  record schedule >/dev/null || true
fi
rm -f "$RAW" "$ERRF"
# a command writes no report of its own: the tail of its output is the report (notices and notify=always show it)
if [ "$KIND" = command ] && [ -f "$RAN" ]; then tail -n 40 "$LOG" > "$REPORT" 2>/dev/null || true; fi

if [ -f "$RAN" ]; then
  echo "$(date '+%F %T') OK $id rc=$rc"
  chx notice "$id" "$D" "$LOG" "$REPORT" ok 2>&1 || true
  chx disable-once "$id" 2>&1 || true     # one-shot jobs switch themselves off
  if [ "$MODE" = "always" ]; then
    body=""; [ -f "$REPORT" ] && body="$(head -c 700 "$REPORT")"
    notify job_ok "Chronos: $id finished.${body:+ }$body"
  fi
else
  touch "$FAILED"
  echo "$(date '+%F %T') FAIL $id rc=$rc timed_out=${timed_out:-0}"
  chx notice "$id" "$D" "$LOG" "$REPORT" failed 2>&1 || true
  [ "$MODE" = "never" ] || notify job_failed "Chronos: $id did not finish (exit $rc${timed_out:+, killed by the watchdog}). Log: $LOG"
fi
[ -n "$RUN_ID" ] && [ -f "$REPORT" ] && cp "$REPORT" "$CH_STATE/reports/$id-$D-$RUN_ID.md" 2>/dev/null     # v0.3.0: one report per run; the per-day path stays a copy of the latest
if [ -f "$RAN" ]; then write_status ok; elif [ -n "$timed_out" ]; then write_status timed_out; else write_status failed; fi
release_claim
exit 0
