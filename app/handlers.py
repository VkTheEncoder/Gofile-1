from __future__ import annotations
import asyncio
import logging
import os
import tempfile
from typing import Optional, Tuple
from telegram.error import BadRequest
from .pyro_client import get_client
from pyrogram.errors import RPCError
import time

from app.http_downloader import http_download
import re
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes
from .config import DOWNLOAD_DIR
from .account_pool import AccountPool
from .gofile_api import GofileClient

log = logging.getLogger(__name__)
_URL_RE = re.compile(r'(https?://\S+)', re.I)
def _extract_urls(text: str | None) -> list[str]:
    if not text:
        return []
    return _URL_RE.findall(text.strip())

class _ThrottleEdit:
    """Edit a Telegram message at most once per `interval` seconds."""
    def __init__(self, msg, interval=1.0):
        self.msg = msg
        self.interval = interval
        self._last = 0.0

    async def edit(self, text: str):
        now = time.time()
        if now - self._last >= self.interval:
            self._last = now
            try:
                await self.msg.edit_text(text)
            except Exception:
                pass

def _bar(pct: float, width: int = 12) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(pct / (100/width))
    return "█"*filled + "░"*(width-filled)

def _fmt_speed(bytes_per_sec: float) -> str:
    if bytes_per_sec >= 1024**2:
        return f"{bytes_per_sec/1024**2:.2f} MB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec/1024:.2f} KB/s"
    return f"{bytes_per_sec:.0f} B/s"

def _ptb_progress_factory(status, start_time, loop):
    def _cb(current: int, total: int, *args):
        pct = (current/total*100) if total else 0.0
        elapsed = max(0.001, time.time() - start_time)
        spd = current / elapsed
        text = (
            "⬇️ Downloading (Bot API)\n"
            f"[{_bar(pct)}] {pct:.1f}%\n"
            f"{current/1024/1024:.2f}/{(total or 0)/1024/1024:.2f} MB\n"
            f"Speed: {_fmt_speed(spd)}"
        )
        # schedule the edit on the main loop (thread-safe)
        loop.call_soon_threadsafe(asyncio.create_task, status.edit(text))
    return _cb


def _pyro_progress_factory(status, start_time, loop):
    def _cb(current: int, total: int):
        pct = (current/total*100) if total else 0.0
        elapsed = max(0.001, time.time() - start_time)
        spd = current / elapsed
        text = (
            "⬇️ Downloading (MTProto)\n"
            f"[{_bar(pct)}] {pct:.1f}%\n"
            f"{current/1024/1024:.2f}/{(total or 0)/1024/1024:.2f} MB\n"
            f"Speed: {_fmt_speed(spd)}"
        )
        # schedule the edit on the main loop (thread-safe)
        loop.call_soon_threadsafe(asyncio.create_task, status.edit(text))
    return _cb


async def _download_telegram_file(update, context, status) -> str | None:
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
        photo = msg.photo[-1]
        file = await photo.get_file()
        filename = f"photo_{photo.file_unique_id}.jpg"
    else:
        return None

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    dest = os.path.join(DOWNLOAD_DIR, filename)
    start = time.time()
    loop = asyncio.get_running_loop()
    await file.download_to_drive(
        custom_path=dest,
        progress=_ptb_progress_factory(status, start, loop),
        progress_args=()
    )
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

async def _download_via_pyrogram(update, dest_dir: str, status: _ThrottleEdit) -> str | None:
    os.makedirs(dest_dir, exist_ok=True)
    chat_id = update.effective_chat.id
    msg_id = update.effective_message.message_id

    client = await get_client()
    m = await client.get_messages(chat_id, msg_id)

    start = time.time()
    loop = asyncio.get_running_loop()
    return await m.download(
        file_name=dest_dir,
        progress=_pyro_progress_factory(status, start, loop),
        progress_args=()
    )


async def _process_http_url(url: str, update, context):
    # one concurrent slot for this whole download+upload
    sem = context.bot_data["sem"]
    async with sem:
        status_msg = await update.effective_message.reply_text(f"Starting URL:\n{url}")
        status = _ThrottleEdit(status_msg, interval=1.0)
        try:
            await status.edit("⬇️ Downloading from URL…")
            path = await http_download(url, dest_dir=DOWNLOAD_DIR, status=status)
        except Exception as e:
            await status.edit(f"❌ URL download failed: {type(e).__name__}: {e}")
            return

        pool: AccountPool = context.bot_data["pool"]
        try:
            await status.edit("⬆️ Uploading to GoFile…")
            for _ in range(len(pool.tokens)):
                idx, client = await pool.pick()
                log.info("Using token index %s for upload (URL)", idx)
                async with client as c:
                    result = await c.upload_file(path, progress_status=status)
                if result and isinstance(result, dict) and ("downloadPage" in result or "contentId" in result):
                    dl = result.get("downloadPage") or result.get("downloadUrl") or result.get("page")
                    content_id = result.get("contentId") or result.get("id")
                    text = "✅ Uploaded to GoFile!\n"
                    if dl: text += f"Link: {dl}\n"
                    if content_id: text += f"Content ID: {content_id}"
                    await status.edit(text)
                    break
                else:
                    await pool.mark_exhausted(idx)
            else:
                await status.edit("❌ All GoFile accounts appear exhausted or failed to upload.")
        finally:
            try: os.remove(path)
            except Exception: pass


async def _process_telegram_media(update, context):
    sem = context.bot_data["sem"]
    async with sem:
        status_msg = await update.effective_message.reply_text("Starting…")
        status = _ThrottleEdit(status_msg, interval=1.0)

        # Try Bot API first
        try:
            path = await _download_telegram_file(update, context, status)
        except BadRequest as e:
            if "File is too big" in str(e):
                # MTProto fallback
                try:
                    path = await _download_via_pyrogram(update, DOWNLOAD_DIR, status)
                except RPCError as e2:
                    await status.edit(f"❌ Download failed via MTProto: {e2}")
                    return
            else:
                await status.edit(f"❌ Download failed: {e}")
                return

        if not path:
            # last resort try MTProto
            try:
                path = await _download_via_pyrogram(update, DOWNLOAD_DIR, status)
            except Exception:
                await status.edit("❌ I couldn't find a file in your message.")
                return

        pool: AccountPool = context.bot_data["pool"]
        try:
            await status.edit("⬆️ Uploading to GoFile…")
            for _ in range(len(pool.tokens)):
                idx, client = await pool.pick()
                log.info("Using token index %s for upload (TG)", idx)
                async with client as c:
                    result = await c.upload_file(path, progress_status=status)
                if result and isinstance(result, dict) and ("downloadPage" in result or "contentId" in result):
                    dl = result.get("downloadPage") or result.get("downloadUrl") or result.get("page")
                    content_id = result.get("contentId") or result.get("id")
                    text = "✅ Uploaded to GoFile!\n"
                    if dl: text += f"Link: {dl}\n"
                    if content_id: text += f"Content ID: {content_id}"
                    await status.edit(text)
                    break
                else:
                    await pool.mark_exhausted(idx)
            else:
                await status.edit("❌ All GoFile accounts appear exhausted or failed to upload.")
        finally:
            try: os.remove(path)
            except Exception: pass


async def handle_incoming_file(update, context):
    # Gather ALL urls from text + caption
    msg = update.effective_message
    urls = _extract_urls((msg.text or "")) + _extract_urls((msg.caption or ""))

    if urls:
        # Spawn one task per URL (parallel; limited by semaphore)
        for url in urls:
            asyncio.create_task(_process_http_url(url, update, context))
        # Acknowledge and return immediately (don’t block the handler)
        await msg.reply_text(f"Queued {len(urls)} URL(s) for processing…")
        return

    # No URLs: treat the incoming message as Telegram media
    # Spawn as a task so handler returns immediately
    asyncio.create_task(_process_telegram_media(update, context))
