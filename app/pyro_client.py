# app/pyro_client.py
from __future__ import annotations
import asyncio
from pyrogram import Client
import os

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# single, shared Pyrogram client (bot mode)
_client: Client | None = None
_lock = asyncio.Lock()

async def get_client() -> Client:
    global _client
    async with _lock:
        if _client is None:
            # name of the session dir; stays in working folder
            _client = Client(
                name="bot_session",
                api_id=API_ID,
                api_hash=API_HASH,
                bot_token=BOT_TOKEN,
                workdir="./pyro_session"
            )
            await _client.start()
        return _client
