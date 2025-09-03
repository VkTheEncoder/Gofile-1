# app/netutils.py
import asyncio
import httpx
import math
import os
import random
import re
from typing import Tuple, Optional
from urllib.parse import urlparse, unquote
from .config import MAX_HTTP_DOWNLOAD_MB, MAX_HTTP_DOWNLOAD_SECONDS

# Tunables via env (safe defaults)
PART_SIZE = int(os.getenv("RANGE_PART_SIZE_MB", "8")) * 1024 * 1024  # 8 MB
MAX_PARTS  = int(os.getenv("RANGE_MAX_PARTS", "6"))
MAX_RETRIES = int(os.getenv("RANGE_MAX_RETRIES", "6"))

# ---------- filename helpers ----------

_INVALID = re.compile(r'[\\/:*?"<>|\x00-\x1f]')   # Windows + control chars
def sanitize_filename(name: str, max_len: int = 180) -> str:
    name = name.strip().strip(". ")
    name = _INVALID.sub("_", name)
    if not name:
        name = "download.bin"
    # keep an extension if present
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            keep = max_len - (len(ext) + 1)
            name = (stem[:keep] or "file") + "." + ext
        else:
            name = name[:max_len]
    return name

_CD_FILENAME_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?("?)([^";]+)\1', re.IGNORECASE)

async def pick_filename_for_url(url: str, default: str = "download.bin") -> str:
    """
    Best-effort filename:
      1) Content-Disposition filename/filename*
      2) URL basename (percent-decoded)
      3) default
    """
    limits  = httpx.Limits(max_connections=5, max_keepalive_connections=5)
    timeout = httpx.Timeout(connect=20.0, read=20.0, write=20.0, pool=20.0)
    try:
        async with httpx.AsyncClient(http2=True, timeout=timeout, limits=limits) as client:
            r = await client.head(url, follow_redirects=True)
            # some servers don't allow HEAD—fallback to GET headers only
            if r.status_code >= 400:
                r = await client.get(url, follow_redirects=True, headers={"Range": "bytes=0-0"})
            cd = r.headers.get("Content-Disposition", "")
            if cd:
                m = _CD_FILENAME_RE.search(cd)
                if m:
                    name = unquote(m.group(2))
                    return sanitize_filename(name)
    except Exception:
        pass

    # fallback to URL
    parsed = urlparse(url)
    base = os.path.basename(parsed.path) or default
    base = unquote(base)
    return sanitize_filename(base or default)

# ---------- downloader internals ----------

def _rng_delay(i: int) -> float:
    return min(60.0, (2 ** i) + random.uniform(0, 1))

async def _head(url: str, client: httpx.AsyncClient) -> Tuple[int, bool]:
    r = await client.head(url, follow_redirects=True)
    r.raise_for_status()
    size = int(r.headers.get("Content-Length", "-1"))
    accept_ranges = "bytes" in r.headers.get("Accept-Ranges", "").lower()
    return size, accept_ranges

async def _fetch_range(url: str, start: int, end: int, client: httpx.AsyncClient, fp, sem: asyncio.Semaphore):
    headers = {"Range": f"bytes={start}-{end}"}
    attempt = 0
    while True:
        try:
            async with sem:
                async with client.stream("GET", url, headers=headers, follow_redirects=True) as r:
                    if r.status_code not in (200, 206):
                        r.raise_for_status()
                    pos = start
                    async for chunk in r.aiter_bytes():
                        if not chunk:
                            continue
                        fp.seek(pos)
                        fp.write(chunk)
                        pos += len(chunk)
            return
        except Exception:
            attempt += 1
            if attempt > MAX_RETRIES:
                raise
            await asyncio.sleep(_rng_delay(attempt))

async def smart_download(url: str, out_path: str):
    """
    Robust downloader:
      - HTTP/2
      - Retry on read/EOF (fixes 'ContentLengthError: not enough data...')
      - Resume when server supports ranges
      - Parallel range parts for throughput
    """
    limits  = httpx.Limits(max_connections=20, max_keepalive_connections=20)
    timeout = httpx.Timeout(connect=60.0, read=900.0, write=300.0, pool=900.0)

    async with httpx.AsyncClient(http2=True, timeout=timeout, limits=limits) as client:
        size, ranged = await _head(url, client)

                attempt = 0
        while True:
            try:
                async with client.stream("GET", url, headers=headers, follow_redirects=True) as r:
                    if r.status_code not in (200, 206):
                        r.raise_for_status()
                    pos = downloaded
                    async for chunk in r.aiter_bytes():
                        if not chunk:
                            continue
                        fp.seek(pos)
                        fp.write(chunk)
                        pos += len(chunk)

                # ✅ After a successful pass, confirm we’re complete if size is known.
                if size > 0:
                    final = pos
                    if final < size:
                        # not done yet → resume from where we stopped
                        downloaded = final
                        headers = {"Range": f"bytes={downloaded}-"}
                        continue

                # done
                return

            except Exception:
                attempt += 1
                if attempt > MAX_RETRIES:
                    raise
                # ensure we resume from current file end on retry
                try:
                    downloaded = fp.seek(0, os.SEEK_END)
                    headers = {"Range": f"bytes={downloaded}-"}
                except Exception:
                    headers = {}
                await asyncio.sleep(_rng_delay(attempt))

        else:
            parts = max(1, min(MAX_PARTS, math.ceil(size / PART_SIZE)))
            with open(out_path, "w+b") as fp:
                fp.truncate(size)  # preallocate contiguous file
                sem = asyncio.Semaphore(parts)
                tasks = []
                for i in range(parts):
                    start = i * PART_SIZE
                    end = min(size - 1, (i + 1) * PART_SIZE - 1)
                    tasks.append(_fetch_range(url, start, end, client, fp, sem))
                await asyncio.gather(*tasks)
