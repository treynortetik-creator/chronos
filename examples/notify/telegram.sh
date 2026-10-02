#!/bin/bash
# Chronos notify example: Telegram Bot API over plain curl.
#   usage (set by Chronos):  telegram.sh "message"
# Needs two environment variables. launchd does not read your shell profile, so put them in a file that
# this script sources, e.g. ~/.config/chronos/notify.env (chmod 600), containing:
#     TELEGRAM_BOT_TOKEN=<token from @BotFather>
#     TELEGRAM_CHAT_ID=<your chat id>
# curl never starts a getUpdates poller, so this cannot cause the HTTP 409 conflict that a headless
# Claude with the Telegram plugin enabled would (see README).
set -u
ENV_FILE="${CHRONOS_NOTIFY_ENV:-$HOME/.config/chronos/notify.env}"
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
: "${TELEGRAM_BOT_TOKEN:?set TELEGRAM_BOT_TOKEN}" "${TELEGRAM_CHAT_ID:?set TELEGRAM_CHAT_ID}"
msg="${1:-}"; [ -n "$msg" ] || exit 0
# Telegram caps a message at 4096 characters
curl -sS --max-time 20 \
  --data-urlencode "chat_id=$TELEGRAM_CHAT_ID" \
  --data-urlencode "text=${msg:0:4000}" \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" >/dev/null
