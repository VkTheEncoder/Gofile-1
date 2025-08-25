# ---- stage: fetch telegram-bot-api binary ----
FROM debian:stable-slim AS botapi
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl xz-utils && rm -rf /var/lib/apt/lists/*
# grab latest release (x86_64); adjust URL for different arch if needed
RUN curl -L -o /tmp/telegram-bot-api.tar.xz \
      https://github.com/tdlib/telegram-bot-api/releases/latest/download/telegram-bot-api-linux-x86_64.tar.xz \
 && mkdir -p /opt/tg-bot-api \
 && tar -xf /tmp/telegram-bot-api.tar.xz -C /opt/tg-bot-api \
 && chmod +x /opt/tg-bot-api/telegram-bot-api

# ---- stage: app runtime ----
FROM python:3.11-slim

# system deps
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# copy python deps first (better layer caching)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# copy app
COPY . .

# copy telegram-bot-api binary from stage
COPY --from=botapi /opt/tg-bot-api/telegram-bot-api /usr/local/bin/telegram-bot-api
RUN chmod +x /usr/local/bin/telegram-bot-api

# default envs (override at runtime)
ENV TELEGRAM_API_ID=27999679 \
    TELEGRAM_API_HASH=f553398ca957b9c92bcb672b05557038 \
    BOT_API_BASE_URL=http://127.0.0.1:8081 \
    PORT=8081

# add startup script
COPY docker-start.sh /usr/local/bin/docker-start.sh
RUN chmod +x /usr/local/bin/docker-start.sh

EXPOSE 8081
CMD ["/usr/local/bin/docker-start.sh"]
