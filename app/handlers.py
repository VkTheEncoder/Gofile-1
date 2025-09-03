from __future__ import annotations
from telegram import LinkPreviewOptions
import asyncio
import logging
import os
import time
import mimetypes
import re
from pathlib import Path
from html import escape
from telegram.constants import ParseMode
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes
from pyrogram.errors import RPCError

from . import messages as M
from .pyro_client import get_client
from .netutils import smart_download, pick_filename_for_url, sanitize_filename
from .config import DOWNLOAD_DIR, BOT_API_BASE_URL, TELEGRAM_BOT_TOKEN
from .account_pool import AccountPool
from .gofile_api import GofileClient

log = logging.getLogger(__name__)

_URL_RE = re.compile(r'(https?://\S+)', re.I)

def _extract_urls(text: str | None) -> list[str]:
    if not text:
        return []
    return _URL_RE.findall(text.strip())

class _ThrottleEdit:
    def __init__(self, msg, interval: float = 1.0):
        self.msg = msg
        self.interval = interval
        self._last = 0.0

    async def edit(self, text: str):
        now = time.time()
        if now - self._last >= self.interval:
            self._last = now
            try:
                await self.msg.edit_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except Exception:
                pass

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
        progress = M.progress_block(
            pct=pct,
            current_mb=current / 1024 / 1024,
            total_mb=(total / 1024 / 1024) if total else None,
            speed_human=_fmt_speed(spd),
        )
        text = M.downloading_via_botapi(progress)
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
    tfile = None
    filename = None

    if msg.document:
        tfile = await msg.document.get_file()
        filename = msg.document.file_name or "document.bin"
    elif msg.video:
        tfile = await msg.video.get_file()
        filename = msg.video.file_name or "video.mp4"
    elif msg.audio:
        tfile = await msg.audio.get_file()
        filename = msg.audio.file_name or "audio.bin"
    elif msg.photo:
        photo = msg.photo[-1]
        tfile = await photo.get_file()
        filename = f"photo_{photo.file_unique_id}.jpg"
    else:
        return None

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    dest = os.path.join(DOWNLOAD_DIR, filename)

    base = (BOT_API_BASE_URL or "https://api.telegram.org").rstrip("/")
    file_url = f"{base}/file/bot{TELEGRAM_BOT_TOKEN}/{tfile.file_path}"

    await status.edit(M.url_downloading())
    await smart_download(file_url, dest)
    return dest

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(M.start())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(M.help_text())

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pool: AccountPool = context.bot_data["pool"]
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
    for attr in ("document", "video", "audio", "voice", "photo", "animation", "video_note"):
        media = getattr(m, attr, None)
        if not media:
            continue
        fn = getattr(media, "file_name", None)
        if fn:
            return fn
        if attr == "photo":
            return f"photo_{media.file_unique_id}.jpg"
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

    filename = _guess_filename_from_msg(m)
    filename = re.sub(r"[^\w.\-()+\[\]{} ]+", "_", filename).strip(" .")
    if not filename:
        filename = f"{m.id}.bin"

    full_path = str(Path(dest_dir) / filename)
    start = time.time()
    loop = asyncio.get_running_loop()
    return await m.download(
        file_name=full_path,
        progress=_pyro_progress_factory(status, start, loop),
        progress_args=()
    )

def _extract_gofile_result(result: dict) -> tuple[str | None, str | None]:
    """
    Be generous: pull link from any of the known keys; if only a code is present,
    construct https://gofile.io/d/<code>.
    """
    if not isinstance(result, dict):
        return None, None
    data = result.get("data", result)

    # Prefer explicit link fields
    link = (
        data.get("downloadPage") or data.get("downloadpage") or
        data.get("downloadUrl") or data.get("downloadURL") or
        data.get("page") or data.get("url") or data.get("link") or
        result.get("downloadPage")
    )

    # Known ID/code fields
    code = (
        data.get("code") or data.get("id") or data.get("fileId") or
        data.get("contentId") or data.get("cid") or result.get("code")
    )

    # If no link but we have a code, synthesize the public URL
    if not link and code:
        link = f"https://gofile.io/d/{code}"

    cid = (
        data.get("contentId") or data.get("contentID") or
        data.get("fileId") or data.get("id") or data.get("code") or data.get("cid") or
        result.get("contentId") or code
    )
    return link, cid

async def _process_http_url(url: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    sem = context.bot_data["sem"]
    async with sem:
        status_msg = await update.effective_message.reply_text(M.url_start(url))
        status = _ThrottleEdit(status_msg, interval=1.0)
        path = None
        try:
            await status.edit(M.url_downloading())
            fname = await pick_filename_for_url(url, default="download.bin")
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
            path = os.path.join(DOWNLOAD_DIR, fname)
            await smart_download(url, path)
        except Exception as e:
            await status.edit(M.error("URL download", f"{type(e).__name__}: {e}"))
            return

        pool: AccountPool = context.bot_data["pool"]
        try:
            await status.edit(M.upload_start())
            for _ in range(len(pool.tokens)):
                idx, client = await pool.pick()
                log.info("Using token index %s for upload (URL)", idx)
                try:
                    async with client as c:
                        result = await c.upload_file(path, progress_status=status)
                except Exception as e:
                    await status.edit(M.error("Upload", f"{type(e).__name__}: {e}"))
                    return

                dl, content_id = _extract_gofile_result(result)
                if dl:
                    filename = os.path.basename(path)
                    try:
                        size_mb = os.path.getsize(path) / (1024**2)
                    except Exception:
                        size_mb = 0.0
                    await status.edit(
                        M.upload_success(filename, size_mb, dl)
                        + (f"\n• <b>Content ID:</b> <code>{escape(str(content_id))}</code>" if content_id else "")
                    )
                    break
                else:
                    if isinstance(result, dict) and result.get("error"):
                        await status.edit(M.error("Upload", f"HTTP {result.get('httpStatus')} | {escape(str(result.get('raw'))[:600])}"))
                        return
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

        path = None
        try:
            path = await _download_telegram_file(update, context, status)
        except BadRequest as e:
            if "File is too big" in str(e):
                try:
                    path = await _download_via_pyrogram(update, DOWNLOAD_DIR, status)
                except RPCError as e2:
                    await status.edit(M.error("Download via MTProto", str(e2)))
                    return
            else:
                await status.edit(M.error("Download", str(e)))
                return

        if not path:
            try:
                path = await _download_via_pyrogram(update, DOWNLOAD_DIR, status)
            except Exception:
                await status.edit(M.no_file_found())
                return

        pool: AccountPool = context.bot_data["pool"]
        try:
            await status.edit(M.upload_start())
            for _ in range(len(pool.tokens)):
                idx, client = await pool.pick()
                log.info("Using token index %s for upload (TG)", idx)
                try:
                    async with client as c:
                        result = await c.upload_file(path, progress_status=status)
                except Exception as e:
                    await status.edit(M.error("Upload", f"{type(e).__name__}: {e}"))
                    return

                dl, content_id = _extract_gofile_result(result)
                if dl:
                    filename = os.path.basename(path)
                    try:
                        size_mb = os.path.getsize(path) / (1024**2)
                    except Exception:
                        size_mb = 0.0
                    await status.edit(
                        M.upload_success(filename, size_mb, dl)
                        + (f"\n• <b>Content ID:</b> <code>{escape(str(content_id))}</code>" if content_id else "")
                    )
                    break
                else:
                    if isinstance(result, dict) and result.get("error"):
                        await status.edit(M.error("Upload", f"HTTP {result.get('httpStatus')} | {escape(str(result.get('raw'))[:600])}"))
                        return
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
    msg = update.effective_message
    urls = _extract_urls((msg.text or "")) + _extract_urls((msg.caption or ""))

    if urls:
        for url in urls:
            asyncio.create_task(_process_http_url(url, update, context))
        await msg.reply_text(M.queue_ack(len(urls)))
        return

    asyncio.create_task(_process_telegram_media(update, context))
