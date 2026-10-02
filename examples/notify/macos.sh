#!/bin/bash
# Chronos notify example: a macOS notification banner via osascript.
#   usage (set by Chronos):  macos.sh "message"
msg="${1:-}"; [ -n "$msg" ] || exit 0
title="Chronos${CHRONOS_JOB:+: $CHRONOS_JOB}"
# pass the text as an argument, never splice it into the AppleScript source
osascript -e 'on run argv' -e 'display notification (item 1 of argv) with title (item 2 of argv)' -e 'end run' -- "${msg:0:300}" "$title"
