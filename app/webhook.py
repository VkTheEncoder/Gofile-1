from __future__ import annotations
import logging
import os
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
from .config import TELEGRAM_BOT_TOKEN, LOG_LEVEL, GOFILE_TOKENS, PORT, WEBHOOK_URL, BOT_API_BASE_URL
from .account_pool import AccountPool
from .handlers import start, help_cmd, stats, handle_incoming_file

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
log = logging.getLogger(__name__)

def main():
    pool = AccountPool(GOFILE_TOKENS)
    builder = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN)
    
    if BOT_API_BASE_URL:
        builder = builder.base_url(BOT_API_BASE_URL)
    app = builder.build()
    app.bot_data["pool"] = pool

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stats", stats))

    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.PHOTO,
        handle_incoming_file
    ))

    # Set webhook
    if not WEBHOOK_URL:
        raise RuntimeError("WEBHOOK_URL is required for webhook mode")

    log.info("Setting webhook to %s", WEBHOOK_URL)
    app.run_webhook(listen="0.0.0.0", port=PORT, url_path="webhook", webhook_url=f"{WEBHOOK_URL}")

if __name__ == "__main__":
    main()
