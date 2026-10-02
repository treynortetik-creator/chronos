#!/bin/bash
# Chronos uninstaller. Unloads and removes the two launchd agents. Leaves your data alone unless --purge.
#   ./uninstall.sh [--purge] [--yes]
#   --purge  also delete ~/.chronos (state, logs, history, UI token), ~/.config/chronos (config, jobs)
#            and the copied runtime in ~/.local/share/chronos. Asks first unless --yes.
# Only touches the labels io.github.chronos.tick and io.github.chronos.ui.
set -u
PURGE=0; YES=0
for a in "$@"; do
  case "$a" in --purge) PURGE=1 ;; --yes) YES=1 ;; -h|--help) sed -n '2,7p' "$0"; exit 0 ;; *) echo "unknown option: $a" >&2; exit 2 ;; esac
done
AGENTS="${CHRONOS_LAUNCHAGENTS_DIR:-$HOME/Library/LaunchAgents}"
UIDN="$(id -u)"
for label in io.github.chronos.tick io.github.chronos.ui; do
  if [ -z "${CHRONOS_NO_LAUNCHCTL:-}" ]; then launchctl bootout "gui/$UIDN/$label" 2>/dev/null && echo "unloaded $label"; fi
  [ -f "$AGENTS/$label.plist" ] && rm -f "$AGENTS/$label.plist" && echo "removed $AGENTS/$label.plist"
done
if [ "$PURGE" = 1 ]; then
  CONFIG="${CHRONOS_CONFIG:-$HOME/.config/chronos/config.json}"
  CONFIG_DIR="$(dirname "$CONFIG")"
  # Only delete the config folder when it is a folder of its own named "chronos". A CHRONOS_CONFIG that sits
  # directly in $HOME (or anywhere shared) must never turn --purge into "delete that whole folder".
  if [ "$(basename "$CONFIG_DIR")" = chronos ]; then TARGETS=("$HOME/.chronos" "$CONFIG_DIR" "$HOME/.local/share/chronos")
  else TARGETS=("$HOME/.chronos" "$HOME/.local/share/chronos"); echo "note: $CONFIG_DIR is not a chronos folder, so it is kept; delete your config and jobs there by hand."; fi
  echo "--purge will delete: ${TARGETS[*]}"
  if [ "$YES" != 1 ]; then read -r -p "Type 'purge' to continue: " ans; [ "$ans" = purge ] || { echo "aborted; data kept."; exit 1; }; fi
  for t in "${TARGETS[@]}"; do rm -rf "$t"; done
  echo "purged."
else
  echo "Data kept (~/.chronos, ~/.config/chronos). Use --purge to delete it."
fi
