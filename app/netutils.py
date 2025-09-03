# app/netutils.py
import asyncio, os, math, random, httpx
from typing import Tuple

PART_SIZE = int(os.getenv("RANGE_PART_SIZE_MB", "8")) * 1024 * 1024  # default 8MB
MAX_PARTS = int(os.getenv("RANGE_MAX_PARTS", "6"))                   # 4–8 is a good range
MAX_RETRIES = int(os.getenv("RANGE_MAX_RETRIES", "6"))

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
                        if not chunk:  # keep streaming
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
    Robust downloader: HTTP/2, retries, resume, and parallel ranges when possible.
    Fixes intermittent 'ContentLengthError / payload not completed' by re-requesting the missing range.
    """
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=20)
    timeout = httpx.Timeout(connect=60.0, read=120.0, write=60.0, pool=60.0)
    async with httpx.AsyncClient(http2=True, timeout=timeout, limits=limits) as client:
        size, ranged = await _head(url, client)

        # Fallback: no size or no range support → single stream with resume
        if size <= 0 or not ranged:
            mode = "r+b" if os.path.exists(out_path) else "w+b"
            with open(out_path, mode) as fp:
                downloaded = fp.seek(0, os.SEEK_END)
                headers = {}
                if downloaded:
                    headers["Range"] = f"bytes={downloaded}-"
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
                        return
                    except Exception:
                        attempt += 1
                        if attempt > MAX_RETRIES:
                            raise
                        await asyncio.sleep(_rng_delay(attempt))
        else:
            # Parallel ranged download
            parts = min(MAX_PARTS, max(1, math.ceil(size / PART_SIZE)))
            with open(out_path, "w+b") as fp:
                fp.truncate(size)  # preallocate for faster contiguous writes
                sem = asyncio.Semaphore(parts)
                tasks = []
                for i in range(parts):
                    start = i * PART_SIZE
                    end = min(size - 1, (i + 1) * PART_SIZE - 1)
                    tasks.append(_fetch_range(url, start, end, client, fp, sem))
                await asyncio.gather(*tasks)
