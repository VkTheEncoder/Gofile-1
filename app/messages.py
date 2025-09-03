# app/messages.py
from __future__ import annotations
from html import escape

def start() -> str:
    return (
        "<b>GoFile Uploader</b>\n"
        "Send a <i>document / video / audio / photo</i> or paste a <i>direct URL</i>.\n"
        "I’ll download it and upload to <b>GoFile</b>, then reply with the share link."
    )

def help_text() -> str:
    return (
        "<b>Commands</b>\n"
        "• <code>/start</code> – introduction\n"
        "• <code>/stats</code> – show current account usage (best effort)\n\n"
        "<b>How it works</b>\n"
        "Send a file or a URL → I download → I upload to GoFile → I return a share link."
    )

def queue_ack(n: int) -> str:
    return f"⌛ Queued <b>{n}</b> URL(s) for processing…"

def url_start(url: str) -> str:
    return f"🔗 <b>URL received</b>\n<code>{escape(url)}</code>"

def downloading_via_botapi(progress: str | None = None) -> str:
    base = "⬇️ <b>Downloading</b> <i>(Bot API)</i>"
    return f"{base}\n{progress}" if progress else base

def downloading_via_mtproto(progress: str | None = None) -> str:
    base = "⬇️ <b>Downloading</b> <i>(MTProto)</i>"
    return f"{base}\n{progress}" if progress else base

def url_downloading() -> str:
    return "⬇️ <b>Downloading from URL…</b>"

def upload_start() -> str:
    return "⬆️ <b>Uploading to GoFile…</b>"

def upload_success(filename: str, size_mb: float, link: str) -> str:
    return (
        "✅ <b>Uploaded to GoFile</b>\n"
        f"• <b>File:</b> <code>{escape(filename)}</code>\n"
        f"• <b>Size:</b> {size_mb:.2f} MB\n"
        f"• <b>Link:</b> <a href=\"{escape(link)}\">{escape(link)}</a>"
    )

def error(stage: str, detail: str) -> str:
    return f"❌ <b>{escape(stage)} failed</b>\n<code>{escape(detail)}</code>"

def all_exhausted() -> str:
    return "❌ <b>No available GoFile accounts</b>\nAll accounts look exhausted or blocked. Try again later."

def no_file_found() -> str:
    return "❌ <b>No file found</b>\nPlease send a media file or a direct URL."

def stats_header(idx: int, account_id: str | None, used_gb: float | None, limit_gb: float | None) -> str:
    lines = [f"🧮 <b>Account candidate index:</b> {idx}"]
    if account_id:
        lines.append(f"🆔 <b>Account ID:</b> <code>{escape(account_id)}</code>")
    if used_gb is not None and limit_gb is not None and limit_gb > 0:
        lines.append(f"📊 <b>Monthly traffic:</b> {used_gb:.2f} / {limit_gb:.2f} GB")
    else:
        lines.append("ℹ️ <i>Usage info is limited for free accounts.</i>")
    return "\n".join(lines)

def progress_block(pct: float, current_mb: float, total_mb: float | None, speed_human: str) -> str:
    # Render a compact progress block you can append to “Downloading …”
    total_part = f"{total_mb:.2f}" if total_mb is not None else "?"
    # 12-char bar, same as your current logic
    filled = max(0, min(12, int((pct/100) * 12))) if total_mb else int(pct/100*12)
    bar = "█"*filled + "░"*(12-filled)
    return (
        f"[{bar}] {pct:.1f}%\n"
        f"{current_mb:.2f}/{total_part} MB\n"
        f"Speed: {speed_human}"
    )
