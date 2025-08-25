from __future__ import annotations
import logging
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
from .config import TELEGRAM_BOT_TOKEN, LOG_LEVEL, GOFILE_TOKENS, BOT_API_BASE_URL
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
    
    if BOT_API_BASE_URL:  # only if non-empty
        builder = builder.base_url(BOT_API_BASE_URL.rstrip("/") + "/")
    
    app = builder.build()
    
    app.bot_data["pool"] = pool
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stats", stats))

    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.PHOTO,
        handle_incoming_file
    ))

    log.info("Starting bot in polling mode…")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
