from __future__ import annotations

import logging
from telegram import Update
from telegram.constants import MessageEntityType
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
from telegram.ext import Defaults
from .config import TELEGRAM_BOT_TOKEN, LOG_LEVEL, GOFILE_TOKENS, BOT_API_BASE_URL, MAX_CONCURRENT_TRANSFERS
from .account_pool import AccountPool
from .handlers import start, help_cmd, stats, handle_incoming_file
import asyncio
from telegram import LinkPreviewOptions

# --- logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
log = logging.getLogger(__name__)


# --- global error handler ---
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Unhandled error while processing update: %s", update)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Something went wrong, but I’m still here!")
        except Exception:
            pass


def main():
    # 1) Build application
    pool = AccountPool(GOFILE_TOKENS)

    defaults = Defaults(
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),  # NEW
    )
    
    builder = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).defaults(defaults)
    app = builder.build()
    if BOT_API_BASE_URL:
        # Ensure trailing slash for PTB custom Bot API base URL
        builder = builder.base_url(BOT_API_BASE_URL.rstrip("/") + "/")

    app = builder.build()
    app.bot_data["pool"] = pool  # used by handlers

    app.bot_data["sem"] = asyncio.Semaphore(MAX_CONCURRENT_TRANSFERS)

    # 2) URL messages (text or captions) -> same handler
    url_filter = (
        (filters.TEXT & filters.Entity(MessageEntityType.URL)) |
        (filters.TEXT & filters.Regex(r"https?://")) |
        (filters.CAPTION & filters.CaptionEntity(MessageEntityType.URL)) |
        (filters.CAPTION & filters.CaptionRegex(r"https?://"))
    )
    app.add_handler(MessageHandler(url_filter, handle_incoming_file))

    # 3) Files (document/video/audio/photo) -> same handler
    file_filter = (
        filters.Document.ALL |
        filters.VIDEO |
        filters.AUDIO |
        filters.PHOTO
    )
    app.add_handler(MessageHandler(file_filter, handle_incoming_file))

    # 4) Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stats", stats))

    # 5) Errors
    app.add_error_handler(on_error)

    log.info("Starting bot in polling mode…")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
