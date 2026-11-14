#!/bin/bash
# Chronos installer (macOS; Linux without a background timer yet).
#
#   ./install.sh [--workspace DIR] [--port N] [--no-ui] [--no-load] [--copy | --no-copy] [--with-examples] [--force-config] [--quiet]
#
# Creates the folders, writes ~/.config/chronos/config.json from the template (never overwrites an
# existing one unless --force-config), generates the UI token (mode 0600), renders both launchd plists
# with your paths and loads them with `launchctl bootstrap gui/$(id -u)`.
#
#   --workspace DIR   directory headless jobs run in (default ~/chronos-workspace, created if missing)
#   --port N          web UI port (default 4747)
#   --no-ui           install the scheduler only
#   --no-load         write everything but do not touch launchctl (useful for review)
#   --copy            copy the runtime to ~/.local/share/chronos and run from there. This is the DEFAULT
#                     when this checkout lives under ~/Desktop, ~/Documents or ~/Downloads, because
#                     macOS privacy (TCC) can stop launchd jobs reading those folders. --no-copy overrides.
#   --with-examples   install the example jobs (disabled) when you have no jobs.json yet
#   --force-config    overwrite an existing config.json
#   --quiet           print only warnings, errors and ONE summary line. For another installer that calls this one
#                     (Talos does); it adds its own next steps.
#
# On Linux there is no launchd: the runtime is copied to ~/.local/share/chronos, the config, jobs and UI token are made as on a
# Mac, and nothing is loaded. Jobs that run in a Talos thread work (Talos asks Chronos what is due every minute); jobs that run by
# themselves in the background need a timer that Chronos does not install on Linux yet, so they do not run there.
#
# Environment: CHRONOS_CONFIG (config path), CHRONOS_LAUNCHAGENTS_DIR (plist folder, for tests).
set -eu

SRC="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE="$HOME/chronos-workspace"; PORT=4747; UI=1; LOAD=1; COPY=auto; EXAMPLES=0; FORCE=0; QUIET=0
while [ $# -gt 0 ]; do
  case "$1" in
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --no-ui) UI=0; shift ;;
    --no-load) LOAD=0; shift ;;
    --copy) COPY=yes; shift ;;
    --no-copy) COPY=no; shift ;;
    --with-examples) EXAMPLES=1; shift ;;
    --force-config) FORCE=1; shift ;;
    --quiet) QUIET=1; shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { [ "$QUIET" = 1 ] || printf '%s\n' "$*"; }     # informational output; --quiet silences it (warnings and errors never go through say)

case "$(uname -s)" in
  Darwin) MACOS=1 ;;
  Linux) MACOS=0 ;;
  *) echo "Chronos runs on macOS, and on Linux without a background timer yet." >&2; exit 1 ;;
esac
case "$PORT" in ''|*[!0-9]*) echo "--port must be a number" >&2; exit 2 ;; esac

if [ "$MACOS" = 1 ]; then PY=/usr/bin/python3; [ -x "$PY" ] || PY="$(command -v python3 || true)"
else PY="$(command -v python3 || true)"; fi   # Linux: the python3 on PATH (launchd is what needed /usr/bin/python3)
if [ -z "$PY" ]; then
  if [ "$MACOS" = 1 ]; then echo "python3 not found (install the Xcode command line tools: xcode-select --install)" >&2
  else echo "python3 not found (install it with your package manager, for example: sudo apt install python3)" >&2; fi
  exit 1
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || { echo "Python 3.9 or newer is required." >&2; exit 1; }
command -v claude >/dev/null 2>&1 || echo >&2 "warning: the 'claude' CLI is not on PATH. Install Claude Code and run 'claude' once to log in, or set claude_bin in the config."

# ---- where the runtime lives
case "$SRC" in "$HOME/Desktop"/*|"$HOME/Documents"/*|"$HOME/Downloads"/*) PROTECTED=1 ;; *) PROTECTED=0 ;; esac
# Linux: always run from ~/.local/share/chronos, where Talos looks for Chronos (and a pulled checkout never changes a running copy).
# Read before `auto` is resolved below, so only a --no-copy the person really passed is answered.
if [ "$MACOS" = 0 ]; then
  [ "$COPY" = no ] && echo "note: --no-copy does nothing on Linux: the runtime always runs from ~/.local/share/chronos." >&2
  [ "$LOAD" = 0 ] && echo "note: --no-load does nothing on Linux: nothing is loaded there anyway." >&2
  COPY=yes
fi
if [ "$COPY" = auto ]; then COPY=no; [ "$PROTECTED" = 1 ] && COPY=yes; fi
# the checkout already sits where the copy would go (cloned straight into ~/.local/share/chronos): run it in place, never copy
# a folder onto itself (the copy loop would delete the checkout's own files first)
if [ "$COPY" = yes ] && [ "$(cd "$HOME/.local/share/chronos" 2>/dev/null && pwd -P)" = "$(cd "$SRC" && pwd -P)" ]; then
  COPY=no
  say "note: this checkout is already in ~/.local/share/chronos, so it runs from here; nothing was copied."
fi
INSTALL_DIR="$SRC"
if [ "$COPY" = yes ]; then
  INSTALL_DIR="$HOME/.local/share/chronos"
  [ "$PROTECTED" = 1 ] && [ "$MACOS" = 1 ] && say "note: this checkout is in a macOS-protected folder, so the runtime is copied to $INSTALL_DIR (see README, TCC)."
  mkdir -p "$INSTALL_DIR"
  for d in bin lib ui hooks examples; do rm -rf "$INSTALL_DIR/$d"; cp -R "$SRC/$d" "$INSTALL_DIR/$d"; done
  find "$INSTALL_DIR" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  say "re-run ./install.sh after you pull updates to refresh the copy."
fi

CONFIG="${CHRONOS_CONFIG:-$HOME/.config/chronos/config.json}"
CONFIG_DIR="$(dirname "$CONFIG")"
HOME_DIR="$HOME/.chronos"
AGENTS="${CHRONOS_LAUNCHAGENTS_DIR:-$HOME/Library/LaunchAgents}"
umask 077
mkdir -p "$CONFIG_DIR" "$HOME_DIR/state" "$HOME_DIR/logs" "$WORKSPACE"
[ "$MACOS" = 1 ] && mkdir -p "$AGENTS"
chmod 700 "$HOME_DIR" "$HOME_DIR/state" "$HOME_DIR/logs"

# ---- config
if [ ! -f "$CONFIG" ] || [ "$FORCE" = 1 ]; then
  CFG_SRC="$INSTALL_DIR/examples/config.json" WS="$WORKSPACE" PT="$PORT" OUT="$CONFIG" "$PY" - <<'PYEOF'
import json, os
c = json.load(open(os.environ["CFG_SRC"]))
c["workspace"] = os.environ["WS"]
c["ui_port"] = int(os.environ["PT"])
json.dump(c, open(os.environ["OUT"], "w"), indent=2); open(os.environ["OUT"], "a").write("\n")
PYEOF
  chmod 600 "$CONFIG"
  say "wrote $CONFIG"
else
  say "kept existing $CONFIG"
fi

# ---- jobs
JOBS_FILE="$CONFIG_DIR/jobs.json"; JOBS_DIR="$CONFIG_DIR/jobs"
mkdir -p "$JOBS_DIR"
if [ ! -f "$JOBS_FILE" ]; then
  if [ "$EXAMPLES" = 1 ]; then
    cp "$INSTALL_DIR/examples/jobs.json" "$JOBS_FILE"
    for j in "$INSTALL_DIR"/examples/jobs/*/; do [ -d "$JOBS_DIR/$(basename "$j")" ] || cp -R "${j%/}" "$JOBS_DIR/"; done
    say "installed the example jobs (all disabled; enable one in the UI after reading its prompt)"
  else
    echo "[]" > "$JOBS_FILE"
    say "no jobs yet: created an empty jobs.json. Add one in the web UI, or re-run with --with-examples for a few disabled samples."
  fi
fi

# ---- UI token (0600)
TOKEN_FILE="$HOME_DIR/ui-token"
if [ ! -s "$TOKEN_FILE" ]; then
  "$PY" -c 'import secrets; print(secrets.token_urlsafe(32))' > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  say "generated $TOKEN_FILE"
fi

# ---- Linux: no launchd, so nothing to render or load (the background timer is not on Linux yet)
if [ "$MACOS" = 0 ]; then
  CHVER="$("$PY" "$INSTALL_DIR/bin/chronos" --version 2>/dev/null || echo chronos)"
  LINE="Jobs that run in a Talos thread work now. Jobs that run by themselves in the background need a timer that isn't on Linux yet."
  if [ "$QUIET" = 1 ]; then
    echo "$CHVER installed on Linux: $LINE Config $CONFIG"
    exit 0
  fi
  echo
  echo "Chronos is installed (Linux)."
  echo "  $LINE"
  echo "  Config:     $CONFIG"
  echo "  Jobs:       $JOBS_FILE  (prompts in $JOBS_DIR/<id>/prompt.md)"
  echo "  Runtime:    $INSTALL_DIR  (re-run ./install.sh after you pull updates)"
  [ "$UI" = 1 ] && echo "  UI:         not started as a service on Linux; run it by hand: CHRONOS_CONFIG=$CONFIG python3 $INSTALL_DIR/ui/server.py   (then open http://127.0.0.1:$PORT/)"
  echo "  Next:       $INSTALL_DIR/bin/chronos list"
  exit 0
fi

# ---- plists
render() { # template -> destination
  sed -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" -e "s|__HOME_DIR__|$HOME_DIR|g" -e "s|__CONFIG__|$CONFIG|g" -e "s|__PYTHON__|$PY|g" "$1" > "$2"
  plutil -lint "$2" >/dev/null
}
render "$SRC/launchd/io.github.chronos.tick.plist.tmpl" "$AGENTS/io.github.chronos.tick.plist"
[ "$UI" = 1 ] && render "$SRC/launchd/io.github.chronos.ui.plist.tmpl" "$AGENTS/io.github.chronos.ui.plist"
chmod 644 "$AGENTS"/io.github.chronos.*.plist
say "wrote plists in $AGENTS"

# ---- load
if [ "$LOAD" = 1 ]; then
  UIDN="$(id -u)"
  for label in io.github.chronos.tick $([ "$UI" = 1 ] && echo io.github.chronos.ui); do
    launchctl bootout "gui/$UIDN/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$UIDN" "$AGENTS/$label.plist"
    say "loaded $label"
  done
else
  say "--no-load: nothing loaded."
fi

CHVER="$("$PY" "$SRC/bin/chronos" --version 2>/dev/null || echo chronos)"
if [ "$LOAD" = 1 ]; then SCHED_NOTE="scheduler loaded"; UI_NOTE="UI http://127.0.0.1:$PORT/"
else SCHED_NOTE="scheduler NOT loaded (--no-load)"; UI_NOTE="UI NOT running (--no-load)"; fi
[ "$UI" = 1 ] || UI_NOTE="no UI (--no-ui)"
if [ "$QUIET" = 1 ]; then
  echo "$CHVER installed: $SCHED_NOTE; $UI_NOTE; config $CONFIG"
  exit 0
fi
echo
echo "Chronos is installed."
if [ "$LOAD" = 1 ]; then
  [ "$UI" = 1 ] && echo "  UI:         http://127.0.0.1:$PORT/"
else
  echo "  Nothing is running yet (--no-load). Start it by hand:"
  echo "    Scheduler (every 5 minutes, what launchd does):  launchctl bootstrap gui/\$(id -u) $AGENTS/io.github.chronos.tick.plist"
  echo "                              or one tick right now:  CHRONOS_CONFIG=$CONFIG bash $INSTALL_DIR/bin/chronos-tick.sh"
  [ "$UI" = 1 ] && echo "    UI:  launchctl bootstrap gui/\$(id -u) $AGENTS/io.github.chronos.ui.plist"
  [ "$UI" = 1 ] && echo "         or in a terminal:  CHRONOS_CONFIG=$CONFIG python3 $INSTALL_DIR/ui/server.py   (then open http://127.0.0.1:$PORT/)"
fi
echo "  Config:     $CONFIG"
echo "  Jobs:       $JOBS_FILE  (prompts in $JOBS_DIR/<id>/prompt.md)"
echo "  Workspace:  $WORKSPACE   (jobs run here; change it in the config)"
echo "  Next: set \"notify\" in the config if you want messages (see examples/notify/), then try:"
echo "        $INSTALL_DIR/bin/chronos list"
