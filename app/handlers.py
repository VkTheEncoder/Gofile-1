from __future__ import annotations
import asyncio
import logging
import os
import tempfile
from typing import Optional, Tuple
from telegram.error import BadRequest
from .pyro_client import get_client
from pyrogram.errors import RPCError

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes
from .config import DOWNLOAD_DIR
from .account_pool import AccountPool
from .gofile_api import GofileClient

log = logging.getLogger(__name__)

async def _download_telegram_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    """Download incoming file to temp disk and return path."""
    msg = update.effective_message
    file = None
    filename = None

    if msg.document:
        file = await msg.document.get_file()
        filename = msg.document.file_name or "document.bin"
    elif msg.video:
        file = await msg.video.get_file()
        filename = msg.video.file_name or "video.mp4"
    elif msg.audio:
        file = await msg.audio.get_file()
        filename = msg.audio.file_name or "audio.bin"
    elif msg.photo:
        # largest size
        photo = msg.photo[-1]
        file = await photo.get_file()
        filename = f"photo_{photo.file_unique_id}.jpg"
    else:
        return None

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    dest = os.path.join(DOWNLOAD_DIR, filename)
    await file.download_to_drive(custom_path=dest)
    return dest

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send me a document/video/audio/photo and I’ll upload it to GoFile for you."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/start – intro\n/stats – show active account usage (best effort).\nSend a file to upload."
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pool: AccountPool = context.bot_data["pool"]
    # Peek the first account’s usage (best effort)
    idx, client = await pool.pick()
    async with client as c:
        acc_id = await c.get_account_id()
        info = await c.get_account_info(acc_id)
        used, limit = c._extract_usage(info)
    txt = [f"Active index candidate: {idx}"]
    if acc_id:
        txt.append(f"Account ID: {acc_id}")
    if used is not None and limit is not None and limit > 0:
        gb = 1024**3
        txt.append(f"Monthly traffic: {used/gb:.2f} / {limit/gb:.2f} GB")
    else:
        txt.append("Usage fields not provided by API (free accounts expose limited info)." )
    await update.message.reply_text("\n".join(txt))

async def _download_via_pyrogram(update, dest_dir: str) -> str | None:
    """
    Download the same incoming message's media via MTProto (Pyrogram).
    Works for files too big for Bot API (up to ~2GB).
    """
    import os
    os.makedirs(dest_dir, exist_ok=True)

    chat_id = update.effective_chat.id
    msg_id = update.effective_message.message_id

    client = await get_client()
    msg = await client.get_messages(chat_id, msg_id)
    return await msg.download(file_name=dest_dir)

async def handle_incoming_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pool: AccountPool = context.bot_data["pool"]
    chat = update.effective_chat

    await context.bot.send_chat_action(chat.id, ChatAction.UPLOAD_DOCUMENT)

    # 1) Try normal Bot API download
    path = None
    try:
        path = await _download_telegram_file(update, context)
    except BadRequest as e:
        if "File is too big" in str(e):
            # 2) Fallback to Pyrogram (MTProto)
            try:
                path = await _download_via_pyrogram(update, DOWNLOAD_DIR)
            except RPCError as e2:
                await update.message.reply_text(f"Download failed via MTProto: {e2}")
                return
        else:
            await update.message.reply_text(f"Download failed: {e}")
            return

    if not path:
        # Could be no media or another edge-case
        # Try MTProto as last resort anyway
        try:
            path = await _download_via_pyrogram(update, DOWNLOAD_DIR)
        except Exception as e:
            await update.message.reply_text("I couldn't find a file in your message.")
            return

    try:
        last_error = None
        for _ in range(len(pool.tokens)):
            idx, client = await pool.pick()
            log.info("Using token index %s for upload", idx)
            async with client as c:
                result = await c.upload_file(path)
            if result and isinstance(result, dict) and ("downloadPage" in result or "contentId" in result):
                dl = result.get("downloadPage") or result.get("downloadUrl") or result.get("page")
                content_id = result.get("contentId") or result.get("id")
                message = ["✅ Uploaded to GoFile!"]
                if dl:
                    message.append(f"Link: {dl}")
                if content_id:
                    message.append(f"Content ID: {content_id}")
                await update.message.reply_text("\n".join(message))
                break
            else:
                last_error = result
                await pool.mark_exhausted(idx)
        else:
            await update.message.reply_text("All GoFile accounts appear exhausted or failed to upload.")
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
