#!/bin/bash
# A stand-in for the `claude` CLI, used only by the tests. Records how it was called, then behaves
# according to $FAKE_MODE: ok (default) | fail | hang | nomarker | iserror | alldenied | somedenied
# With FAKE_STREAM=1 it prints what `claude -p --output-format=stream-json --verbose` prints (system init,
# assistant text, a rate_limit_event, a result with usage and cost) instead of plain text.
LOGF="${FAKE_LOG:-/dev/null}"
{ echo "ARGS: $*"; echo "CHRONOS_RUN=${CHRONOS_RUN:-}"; echo "CWD=$(pwd)"; echo "CHRONOS_JOB=${CHRONOS_JOB:-}"; } >> "$LOGF"
prompt="${@: -1}"
ran="$(printf '%s\n' "$prompt" | sed -n 's/^  2\. Create the done-marker: touch //p')"
report="$(printf '%s\n' "$prompt" | sed -n 's/^  1\. Write a short markdown report of what you did to //p')"

say() {  # say <text> [is_error true|false]
  if [ -n "${FAKE_STREAM:-}" ]; then
    echo '{"type":"system","subtype":"init","model":"claude-fake-1"}'
    echo '{"type":"assistant","message":{"content":[{"type":"text","text":"working"}]}}'
    echo '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","unifiedWindows":{"seven_day":{"utilization":0.42,"resetsAt":1790000000},"five_hour":{"utilization":0.1,"resetsAt":1789990000}}}}'
    echo '{"type":"result","subtype":"success","is_error":'"${2:-false}"',"result":"'"$1"'","total_cost_usd":0.1234,"num_turns":3,"usage":{"input_tokens":100,"output_tokens":50,"cache_creation_input_tokens":10,"cache_read_input_tokens":1000},"modelUsage":{"claude-fake-1":{}},"permission_denials":[{"tool_name":"Bash"}],"session_id":"abc"}'
  else
    echo "$1"
  fi
}

denied_stream() {  # denied_stream <tool_uses> <denials>: a stream whose tool calls were (partly) refused by the permission system
  echo '{"type":"system","subtype":"init","model":"claude-fake-1"}'
  local i
  for i in $(seq 1 "$1"); do echo '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t'$i'","name":"Bash","input":{"command":"x"}}]}}'; done
  local d="" j
  for j in $(seq 1 "$2"); do d="$d{\"tool_name\":\"Bash\"},"; done
  echo '{"type":"result","subtype":"success","is_error":false,"result":"I could not run anything","total_cost_usd":0.01,"num_turns":3,"usage":{"input_tokens":1,"output_tokens":1},"permission_denials":['"${d%,}"'],"session_id":"abc"}'
}

case "${FAKE_MODE:-ok}" in
  alldenied) denied_stream 2 2; exit 0 ;;
  somedenied) denied_stream 3 1; exit 0 ;;
  hang) exec sleep 120 ;;
  fail) echo "simulated failure"; exit 1 ;;
  iserror) say "the model reported an error" true; exit 0 ;;
  nomarker) say "did the thing but forgot the marker"; [ -n "$report" ] && echo "report" > "$report"; exit 0 ;;
  *) [ -n "$report" ] && { mkdir -p "$(dirname "$report")"; echo "# Report: all good" > "$report"; }
     [ -n "$ran" ] && touch "$ran"; say "done"; exit 0 ;;
esac
