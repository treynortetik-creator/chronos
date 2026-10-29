# chronos-deadline.sh: `ch_step`, a deadline enforced from OUTSIDE the process. Sourced by chronos-tick.sh.
#
# Why not signal.alarm inside Python: an in-process handler runs only after a blocked syscall returns, and the stall seen on
# macOS is a thread stuck in the kernel in open(), waiting on a privacy (TCC) check nobody sees. kill -9 from a separate
# process cannot be blocked. There is no `timeout` binary on stock macOS, and none is needed.
#
#   ch_step <seconds> <label> <command> [args...]
#
# Runs the command as a child (its stdout and stderr pass straight through, so $(...) capture works), and a watchdog in the
# background sends it kill -9 after <seconds>, logging "DEADLINE <label>" to $CH_LOGS/chronos.log. Returns the command's exit
# status (137 when it was killed). The watchdog is stopped as soon as the command finishes, so none outlives its step.
# ch_tree <pid>: every descendant of <pid>, deepest first (so a killed parent cannot re-parent a child out of reach). Kill by PID only.
ch_tree() {
  local k
  for k in $(pgrep -P "$1" 2>/dev/null); do ch_tree "$k"; echo "$k"; done
}

ch_step() {
  local secs="$1" label="$2" c w
  shift 2
  "$@" &
  c=$!
  (
    sleep "$secs" >/dev/null 2>&1 &
    local sp=$!
    trap 'kill "$sp" 2>/dev/null; exit 0' TERM
    wait "$sp"
    kids="$(ch_tree "$c" | tr '\n' ' ')"
    if kill -9 $kids "$c" 2>/dev/null; then
      echo "$(date '+%F %T') DEADLINE $label (no answer in ${secs}s; killed)" >> "${CH_LOGS:-/dev/null}/chronos.log" 2>/dev/null
    fi
  ) >/dev/null 2>&1 &
  w=$!
  wait "$c"
  local rc=$?
  kill "$w" 2>/dev/null
  wait "$w" 2>/dev/null
  return $rc
}
