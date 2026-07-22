#!/bin/sh
set -eu

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ "${TELEGRAM_BOT_TOKEN:-}" = "your_bot_token_here" ]; then
  echo "TELEGRAM_BOT_TOKEN missing. Set it in Coolify Environment Variables."
  echo "Container will stay alive so Coolify does not enter a restart loop."
  tail -f /dev/null
fi

python run_bot.py
status=$?

echo "Telegram bot stopped with exit code ${status}."
echo "Check the lines above. Common causes: invalid token, same bot running elsewhere, or missing env variables."
echo "Container will stay alive so Coolify does not enter a restart loop."
tail -f /dev/null
