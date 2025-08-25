#!/usr/bin/env bash
set -euo pipefail

# sanity checks
if [[ "${TELEGRAM_API_ID:-0}" == "0" || -z "${TELEGRAM_API_HASH:-}" ]]; then
  echo "ERROR: TELEGRAM_API_ID / TELEGRAM_API_HASH must be set (from https://my.telegram.org)."
  exit 1
fi

# Start local Bot API server (background)
echo "Starting telegram-bot-api on port ${PORT:-8081} ..."
telegram-bot-api \
  --api-id="${TELEGRAM_API_ID}" \
  --api-hash="${TELEGRAM_API_HASH}" \
  --http-port="${PORT:-8081}" \
  --local \
  --max-webhook-connections=40 \
  --log=3 &

# wait until port is ready
echo "Waiting for Bot API server to become ready..."
for i in {1..30}; do
  if curl -fsS "http://127.0.0.1:${PORT:-8081}/" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# warn if still not reachable (but continue — it may come up a bit later)
if ! curl -fsS "http://127.0.0.1:${PORT:-8081}/" >/dev/null 2>&1; then
  echo "WARNING: Bot API at 127.0.0.1:${PORT:-8081} not reachable yet. Continuing…"
fi

# ensure base URL is clean (no /bot or token)
export BOT_API_BASE_URL="${BOT_API_BASE_URL%%/bot*}"
echo "Using BOT_API_BASE_URL=${BOT_API_BASE_URL}"

# run the python bot (foreground)
echo "Starting Python bot…"
exec python -m app.bot
