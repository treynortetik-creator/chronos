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
ENVOUT="$("$PYBIN" "$DIR/chronos" env)" || exit 1      # (not `eval "$(...)" || ...`: eval of an empty string succeeds)
eval "$ENVOUT"
[ -n "$CH_PATH_EXTRA" ] && export PATH="$CH_PATH_EXTRA:$PATH"
export CHRONOS_RUN=1            # tells the optional SessionStart hook to stay silent inside this run

id="${1:-}"; D="${2:-$(date +%Y-%m-%d)}"
EVFILE="${3:-}"; EHASH="${4:-}"; ETYPE="${5:-}"; EVENT=""
[ -n "$EHASH" ] && EVENT=1
case "$id" in ""|*[!a-z0-9-]*) echo "bad job id"; exit 1 ;; esac
case "$EHASH" in *[!a-f0-9]*) echo "bad event hash"; exit 1 ;; esac
case "$ETYPE" in *[!a-z]*) echo "bad event type"; exit 1 ;; esac
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

notify() { CHRONOS_EVENT="$1" "$DIR/chronos-notify" "$2" >/dev/null 2>&1 || true; }
fail_early() { echo "$(date '+%F %T') FAIL $id $1"; [ -z "$EVENT" ] && touch "$FAILED"; rmdir "$CLAIM" 2>/dev/null; exit 1; }

# idempotent: a relaunch must never redo a finished job
[ -f "$RAN" ] && { echo "$(date '+%F %T') SKIP $id already ran"; rmdir "$CLAIM" 2>/dev/null; exit 0; }
[ -d "$CH_WORKSPACE" ] || fail_early "workspace missing: $CH_WORKSPACE"

KIND="$("$PYBIN" "$DIR/chronos" field "$id" kind)"
CMD=""
if [ "$KIND" = command ]; then
  [ -z "$EVENT" ] || fail_early "a command job cannot run on an event"
  CMD="$("$PYBIN" "$DIR/chronos" field "$id" command)"
  [ -n "$CMD" ] || fail_early "command job has no command"
fi

if [ "$KIND" = command ]; then
  :
elif [ -n "$EVENT" ]; then
  [ -f "$EVFILE" ] || fail_early "event file unreadable: $EVFILE"
  PROMPT="$("$PYBIN" "$DIR/chronos" prompt "$id" --event "$EVFILE" "$ETYPE")" || fail_early "cannot build the event prompt"
else
  PROMPT="$("$PYBIN" "$DIR/chronos" prompt "$id" "$D")" || fail_early "cannot build prompt"
fi
MODE="$("$PYBIN" "$DIR/chronos" field "$id" notify)"
# per-job settings: CH_JOB_MODEL and, for events, CH_EV_ALLOWED / CH_EV_TOOLS / CH_EV_DENY / CH_EV_MCP.
# A job that cannot be read is a failure; nothing falls back to a wider tool list.
CH_JOB_MODEL=""; CH_RESTRICTED=0; CH_RSETTINGS=""
if [ "$KIND" != command ]; then
  JOBENV="$("$PYBIN" "$DIR/chronos" jobenv "$id")" || fail_early "cannot read the job's settings"
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
pid=$!; waited=0; timed_out=""
while kill -0 "$pid" 2>/dev/null; do
  sleep "$POLL"; waited=$((waited + POLL))
  if [ "$waited" -ge "$CH_TIMEOUT_S" ]; then
    timed_out=1; pkill -TERM -P "$pid" 2>/dev/null; kill -TERM "$pid" 2>/dev/null; break
  fi
done
wait "$pid" 2>/dev/null; rc=$?

record() {  # $1 = schedule|event. Builds $LOG from the stream and logs usage; any failure leaves the raw stream as the log.
  cp "$RAW" "$LOG" 2>/dev/null
  "$PYBIN" "$DIR/chronos" record --raw "$RAW" --log "$LOG" --err "$ERRF" --id "$id" --kind "$1" --start "$T0" --end "$(date +%s)" \
    --rc "$rc" --timed-out "$([ -n "$timed_out" ] && echo 1 || echo 0)" --model-flag "$CH_JOB_MODEL" --ttype "$ETYPE" --thash "$EHASH" \
    --marker-path "$RAN" --command "$([ "$KIND" = command ] && echo 1 || echo 0)" --restricted "$([ -n "$RESTRICTED" ] && echo 1 || echo 0)" 2>>"$CH_LOGS/chronos.log"
}

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
  rmdir "$CLAIM" 2>/dev/null
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
  "$PYBIN" "$DIR/chronos" notice "$id" "$D" "$LOG" "$REPORT" ok 2>&1 || true
  "$PYBIN" "$DIR/chronos" disable-once "$id" 2>&1 || true     # one-shot jobs switch themselves off
  if [ "$MODE" = "always" ]; then
    body=""; [ -f "$REPORT" ] && body="$(head -c 700 "$REPORT")"
    notify job_ok "Chronos: $id finished.${body:+ }$body"
  fi
else
  touch "$FAILED"
  echo "$(date '+%F %T') FAIL $id rc=$rc timed_out=${timed_out:-0}"
  "$PYBIN" "$DIR/chronos" notice "$id" "$D" "$LOG" "$REPORT" failed 2>&1 || true
  [ "$MODE" = "never" ] || notify job_failed "Chronos: $id did not finish (exit $rc${timed_out:+, killed by the watchdog}). Log: $LOG"
fi
rmdir "$CLAIM" 2>/dev/null
exit 0
