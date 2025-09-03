from __future__ import annotations

import asyncio
import logging
import os
import time
import mimetypes
import re
from pathlib import Path
from html import escape

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes
from pyrogram.errors import RPCError

from . import messages as M
from .pyro_client import get_client
from app.http_downloader import http_download
from .config import DOWNLOAD_DIR
from .account_pool import AccountPool
from .gofile_api import GofileClient  # kept for type/context clarity

log = logging.getLogger(__name__)

_URL_RE = re.compile(r'(https?://\S+)', re.I)


def _extract_urls(text: str | None) -> list[str]:
    if not text:
        return []
    return _URL_RE.findall(text.strip())


class _ThrottleEdit:
    """Edit a Telegram message at most once per `interval` seconds."""
    def __init__(self, msg, interval: float = 1.0):
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
                # Silently ignore edit races (message not modified / deleted etc.)
                pass


def _bar(pct: float, width: int = 12) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(pct / (100 / width))
    return "█" * filled + "░" * (width - filled)


def _fmt_speed(bytes_per_sec: float) -> str:
    if bytes_per_sec >= 1024**2:
        return f"{bytes_per_sec/1024**2:.2f} MB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec/1024:.2f} KB/s"
    return f"{bytes_per_sec:.0f} B/s"


def _ptb_progress_factory(status: _ThrottleEdit, start_time: float, loop):
    def _cb(current: int, total: int, *args):
        pct = (current / total * 100) if total else 0.0
        elapsed = max(0.001, time.time() - start_time)
        spd = current / elapsed
        # Build our standardized progress block
        progress = M.progress_block(
            pct=pct,
            current_mb=current / 1024 / 1024,
            total_mb=(total / 1024 / 1024) if total else None,
            speed_human=_fmt_speed(spd),
        )
        text = M.downloading_via_botapi(progress)
        # schedule the edit on the main loop (thread-safe)
        loop.call_soon_threadsafe(asyncio.create_task, status.edit(text))
    return _cb


def _pyro_progress_factory(status: _ThrottleEdit, start_time: float, loop):
    def _cb(current: int, total: int):
        pct = (current / total * 100) if total else 0.0
        elapsed = max(0.001, time.time() - start_time)
        spd = current / elapsed
        progress = M.progress_block(
            pct=pct,
            current_mb=current / 1024 / 1024,
            total_mb=(total / 1024 / 1024) if total else None,
            speed_human=_fmt_speed(spd),
        )
        text = M.downloading_via_mtproto(progress)
        loop.call_soon_threadsafe(asyncio.create_task, status.edit(text))
    return _cb


async def _download_telegram_file(update: Update, context: ContextTypes.DEFAULT_TYPE, status: _ThrottleEdit) -> str | None:
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
    await update.message.reply_text(M.start())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(M.help_text())


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pool: AccountPool = context.bot_data["pool"]
    # Peek the first account’s usage (best effort)
    idx, client = await pool.pick()
    async with client as c:
        acc_id = await c.get_account_id()
        info = await c.get_account_info(acc_id)
        used, limit = c._extract_usage(info)
    gb = 1024**3
    used_gb = (used / gb) if (used is not None) else None
    limit_gb = (limit / gb) if (limit is not None) else None
    await update.message.reply_text(M.stats_header(idx, acc_id, used_gb, limit_gb))


def _guess_filename_from_msg(m) -> str:
    # Try to reuse Telegram-provided filenames where possible
    for attr in ("document", "video", "audio", "voice", "photo", "animation", "video_note"):
        media = getattr(m, attr, None)
        if not media:
            continue
        # document/video/audio often carry a file_name
        fn = getattr(media, "file_name", None)
        if fn:
            return fn

        # photo has no name; give it a jpeg with unique id
        if attr == "photo":
            return f"photo_{media.file_unique_id}.jpg"

    # Fallback: use caption hint or message id with extension guess
    ext = ""
    mt = getattr(m, "mime_type", None) or getattr(getattr(m, "document", None), "mime_type", None)
    if mt:
        ext = mimetypes.guess_extension(mt) or ""
    return f"{m.id}{ext or '.bin'}"


async def _download_via_pyrogram(update: Update, dest_dir: str, status: _ThrottleEdit) -> str | None:
    os.makedirs(dest_dir, exist_ok=True)

    chat_id = update.effective_chat.id
    msg_id = update.effective_message.message_id

    client = await get_client()
    m = await client.get_messages(chat_id, msg_id)

    # ✅ Build a full file path (folder + filename)
    filename = _guess_filename_from_msg(m)
    # sanitize a bit
    filename = re.sub(r"[^\w.\-()+\[\]{} ]+", "_", filename).strip(" .")
    if not filename:
        filename = f"{m.id}.bin"

    full_path = str(Path(dest_dir) / filename)

    start = time.time()
    loop = asyncio.get_running_loop()
    return await m.download(
        file_name=full_path,  # <— pass a full path, not just the folder
        progress=_pyro_progress_factory(status, start, loop),
        progress_args=()
    )


async def _process_http_url(url: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    # one concurrent slot for this whole download+upload
    sem = context.bot_data["sem"]
    async with sem:
        status_msg = await update.effective_message.reply_text(M.url_start(url))
        status = _ThrottleEdit(status_msg, interval=1.0)
        path = None
        try:
            await status.edit(M.url_downloading())
            path = await http_download(url, dest_dir=DOWNLOAD_DIR, status=status)
        except Exception as e:
            await status.edit(M.error("URL download", f"{type(e).__name__}: {e}"))
            return

        pool: AccountPool = context.bot_data["pool"]
        try:
            await status.edit(M.upload_start())
            for _ in range(len(pool.tokens)):
                idx, client = await pool.pick()
                log.info("Using token index %s for upload (URL)", idx)
                async with client as c:
                    result = await c.upload_file(path, progress_status=status)
                if result and isinstance(result, dict) and ("downloadPage" in result or "contentId" in result):
                    dl = result.get("downloadPage") or result.get("downloadUrl") or result.get("page")
                    content_id = result.get("contentId") or result.get("id")

                    # Format success message uniformly
                    filename = os.path.basename(path)
                    try:
                        size_mb = os.path.getsize(path) / (1024**2)
                    except Exception:
                        size_mb = 0.0
                    link = dl or ""
                    await status.edit(M.upload_success(filename, size_mb, link) + (f"\n• <b>Content ID:</b> <code>{escape(str(content_id))}</code>" if content_id else ""))
                    break
                else:
                    await pool.mark_exhausted(idx)
            else:
                await status.edit(M.all_exhausted())
        finally:
            try:
                if path:
                    os.remove(path)
            except Exception:
                pass


async def _process_telegram_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sem = context.bot_data["sem"]
    async with sem:
        status_msg = await update.effective_message.reply_text("⏳ <b>Starting…</b>")
        status = _ThrottleEdit(status_msg, interval=1.0)

        # Try Bot API first
        path = None
        try:
            path = await _download_telegram_file(update, context, status)
        except BadRequest as e:
            if "File is too big" in str(e):
                # MTProto fallback
                try:
                    path = await _download_via_pyrogram(update, DOWNLOAD_DIR, status)
                except RPCError as e2:
                    await status.edit(M.error("Download via MTProto", str(e2)))
                    return
            else:
                await status.edit(M.error("Download", str(e)))
                return

        if not path:
            # last resort try MTProto
            try:
                path = await _download_via_pyrogram(update, DOWNLOAD_DIR, status)
            except Exception as e:
                await status.edit(M.no_file_found())
                return

        pool: AccountPool = context.bot_data["pool"]
        try:
            await status.edit(M.upload_start())
            for _ in range(len(pool.tokens)):
                idx, client = await pool.pick()
                log.info("Using token index %s for upload (TG)", idx)
                async with client as c:
                    result = await c.upload_file(path, progress_status=status)
                if result and isinstance(result, dict) and ("downloadPage" in result or "contentId" in result):
                    dl = result.get("downloadPage") or result.get("downloadUrl") or result.get("page")
                    content_id = result.get("contentId") or result.get("id")

                    filename = os.path.basename(path)
                    try:
                        size_mb = os.path.getsize(path) / (1024**2)
                    except Exception:
                        size_mb = 0.0
                    link = dl or ""
                    await status.edit(M.upload_success(filename, size_mb, link) + (f"\n• <b>Content ID:</b> <code>{escape(str(content_id))}</code>" if content_id else ""))
                    break
                else:
                    await pool.mark_exhausted(idx)
            else:
                await status.edit(M.all_exhausted())
        finally:
            try:
                if path:
                    os.remove(path)
            except Exception:
                pass


async def handle_incoming_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Gather ALL urls from text + caption
    msg = update.effective_message
    urls = _extract_urls((msg.text or "")) + _extract_urls((msg.caption or ""))

    if urls:
        # Spawn one task per URL (parallel; limited by semaphore)
        for url in urls:
            asyncio.create_task(_process_http_url(url, update, context))
        # Acknowledge and return immediately (don’t block the handler)
        await msg.reply_text(M.queue_ack(len(urls)))
        return

    # No URLs: treat the incoming message as Telegram media
    # Spawn as a task so handler returns immediately
    asyncio.create_task(_process_telegram_media(update, context))
