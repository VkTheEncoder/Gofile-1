# app/pyro_client.py
from __future__ import annotations
import asyncio, os
from pyrogram import Client

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

_client: Client | None = None
_lock = asyncio.Lock()

async def get_client() -> Client:
    """Return a singleton Pyrogram client (bot), using in-memory session (no files)."""
    global _client
    async with _lock:
        if _client is None:
            if not API_ID or not API_HASH or not BOT_TOKEN:
                raise RuntimeError("API_ID / API_HASH / TELEGRAM_BOT_TOKEN must be set in the environment")
            _client = Client(
                name="bot_session",
                api_id=API_ID,
                api_hash=API_HASH,
                bot_token=BOT_TOKEN,
                in_memory=True,        # ✅ avoid filesystem/SQLite
            )
            await _client.start()
        return _client
