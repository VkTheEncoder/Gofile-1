# app/netutils.py

import asyncio
import os
import re
import math
import time
import random
from urllib.parse import urlparse, unquote, parse_qs

import httpx

# ---------- Tunables ----------
CONNECT_TIMEOUT = 60.0
READ_TIMEOUT    = 900.0    # long read → full movies won’t truncate at ~2–3 min
WRITE_TIMEOUT   = 300.0
POOL_TIMEOUT    = 900.0

MAX_RETRIES     = 5
CHUNK_SIZE      = 1024 * 1024  # 1 MiB per chunk

# ---------- Small utils your handlers import ----------

def sanitize_filename(name: str) -> str:
    """Make a safe filesystem name."""
    name = unquote(name).strip()
    # remove path separators and control chars
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1F]", "_", name)
    # collapse spaces/underscores
    name = re.sub(r"[\s_]{2,}", " ", name).strip()
    return name or "file.bin"

def pick_filename_for_url(url: str, fallback: str = "download.bin") -> str:
    """Heuristically pick a filename from URL path or common query keys."""
    p = urlparse(url)
    q = parse_qs(p.query)
    for key in ("filename", "file", "name", "download", "dl"):
        if key in q and q[key]:
            return sanitize_filename(q[key][0])
    path = unquote(p.path.rstrip("/"))
    if path:
        base = os.path.basename(path)
        if base and "." in base:
            return sanitize_filename(base)
    return sanitize_filename(fallback)

# ---------- Internals ----------

def _rng_delay(attempt: int) -> float:
    """Exponential backoff with jitter."""
    base = min(30.0, 1.5 ** attempt)
    return base + random.uniform(0, 0.75)

async def _probe_headers(client: httpx.AsyncClient, url: str) -> tuple[int, bool]:
    """
    Return (size, ranged). size = -1 if unknown.
    """
    size = -1
    ranged = False
    try:
        r = await client.head(url, follow_redirects=True)
        # Some origins block HEAD; fall back if needed.
        if r.status_code < 400:
            cl = r.headers.get("Content-Length")
            if cl and cl.isdigit():
                size = int(cl)
            ar = r.headers.get("Accept-Ranges", "") or r.headers.get("accept-ranges", "")
            ranged = "bytes" in ar.lower()
            return size, ranged
    except Exception:
        pass

    # Fallback: GET a single byte with Range to test support & obtain length
    try:
        r = await client.get(url, headers={"Range": "bytes=0-0"}, follow_redirects=True)
        if r.status_code in (200, 206, 416):
            cl = r.headers.get("Content-Length")
            if cl and cl.isdigit():
                # For 206 we only got 1 byte; size header is often 1.
                # Prefer Content-Range if present: bytes 0-0/123456
                cr = r.headers.get("Content-Range", "")
                if "/" in cr:
                    try:
                        size = int(cr.split("/")[-1])
                    except Exception:
                        size = int(cl)
                else:
                    size = int(cl)
            cr = r.headers.get("Content-Range", "")
            ranged = "bytes" in cr.lower() or r.status_code == 206
    except Exception:
        pass
    return size, ranged

# ---------- Downloader ----------

async def smart_download(url: str, out_path: str, *args, progress=None, chunk_size: int = CHUNK_SIZE, **kwargs) -> str:
    """
    Robust single-file downloader with resume and long timeouts.

    Parameters
    ----------
    url : str
    out_path : str
    progress : Optional[callable(total:int|None, downloaded:int)]; called periodically.
               (If callers pass a 3rd positional arg as a callback, we’ll accept that too.)

    Returns
    -------
    str : the out_path
    """
    # Back-compat: if someone passed a positional callback as 3rd arg
    if progress is None and args:
        maybe_cb = args[0]
        if callable(maybe_cb):
            progress = maybe_cb

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    limits  = httpx.Limits(max_connections=8, max_keepalive_connections=8)
    timeout = httpx.Timeout(
        connect=CONNECT_TIMEOUT,
        read=READ_TIMEOUT,
        write=WRITE_TIMEOUT,
        pool=POOL_TIMEOUT,
    )

    async with httpx.AsyncClient(http2=True, timeout=timeout, limits=limits) as client:
        total_size, ranged = await _probe_headers(client, url)

        # Early exit: if file already complete
        if total_size > 0 and os.path.exists(out_path) and os.path.getsize(out_path) >= total_size:
            if callable(progress):
                await _maybe_await(progress, total_size, total_size)
            return out_path

        attempt = 0
        while True:
            try:
                # Figure out how much we have and attempt to resume
                downloaded = 0
                headers = {}
                mode = "r+b" if os.path.exists(out_path) else "w+b"
                with open(out_path, mode) as fp:
                    if os.path.exists(out_path):
                        downloaded = fp.seek(0, os.SEEK_END)

                    if downloaded > 0 and ranged:
                        headers["Range"] = f"bytes={downloaded}-"

                    async with client.stream("GET", url, headers=headers, follow_redirects=True) as r:
                        if r.status_code not in (200, 206):
                            r.raise_for_status()

                        # If we resumed but server ignored Range, we must rewrite from 0.
                        if downloaded and r.status_code == 200:
                            fp.seek(0)
                            fp.truncate(0)
                            downloaded = 0

                        if callable(progress):
                            await _maybe_await(progress, total_size if total_size > 0 else None, downloaded)

                        async for chunk in r.aiter_bytes(chunk_size=chunk_size):
                            if not chunk:
                                continue
                            fp.seek(downloaded)
                            fp.write(chunk)
                            downloaded += len(chunk)
                            if callable(progress):
                                await _maybe_await(progress, total_size if total_size > 0 else None, downloaded)

                # Verify completeness if we know size; otherwise accept as done
                if total_size > 0 and downloaded < total_size:
                    # server closed early — loop and resume
                    attempt += 1
                    if attempt > MAX_RETRIES:
                        raise RuntimeError(f"download stalled after {attempt} attempts; got {downloaded}/{total_size} bytes")
                    await asyncio.sleep(_rng_delay(attempt))
                    continue

                return out_path

            except Exception as e:
                attempt += 1
                if attempt > MAX_RETRIES:
                    raise
                await asyncio.sleep(_rng_delay(attempt))

# helper: allow both sync/async progress callbacks
async def _maybe_await(fn, total, downloaded):
    try:
        ret = fn(total, downloaded)
        if asyncio.iscoroutine(ret):
            await ret
    except Exception:
        # progress is best-effort; ignore failures
        pass
