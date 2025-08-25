import os
from dotenv import load_dotenv

load_dotenv()

BOT_API_BASE_URL = os.getenv("BOT_API_BASE_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GOFILE_TOKENS = [t.strip() for t in os.getenv("GOFILE_TOKENS", "").split(",") if t.strip()]

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads").strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Webhook
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "false").lower() in {"1", "true", "yes"}
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
PORT = int(os.getenv("PORT", "8080"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

if not GOFILE_TOKENS:
    raise RuntimeError("Provide at least one GoFile token via GOFILE_TOKENS separated by commas")
