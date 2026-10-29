#!/bin/bash
# A stand-in for `launchctl`, used only by the tests (Chronos reads $CHRONOS_LAUNCHCTL). It never touches launchd.
#   submit -l LABEL [-o OUT] [-e ERR] -- cmd args...   runs the command detached, like a one-off launchd job
#   remove LABEL                                       kills that job by the PID it recorded
# Every call is appended to $FAKE_LAUNCHCTL_DIR/calls.log.
D="${FAKE_LAUNCHCTL_DIR:?FAKE_LAUNCHCTL_DIR must be set}"
mkdir -p "$D"
echo "$*" >> "$D/calls.log"
case "${1:-}" in
  submit)
    shift; label=""; out=/dev/null; err=/dev/null
    while [ $# -gt 0 ]; do
      case "$1" in
        -l) label="$2"; shift 2 ;;
        -o) out="$2"; shift 2 ;;
        -e) err="$2"; shift 2 ;;
        --) shift; break ;;
        *) shift ;;
      esac
    done
    "$@" > "$out" 2> "$err" < /dev/null &
    echo $! > "$D/$label.pid"
    ;;
  remove)
    [ -f "$D/$2.pid" ] && kill "$(cat "$D/$2.pid")" 2>/dev/null
    ;;
esac
exit 0
