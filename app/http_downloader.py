# app/http_downloader.py
from __future__ import annotations
import asyncio
import os
import re
import time
from pathlib import Path
from typing import Optional
import aiohttp
from yarl import URL

# Chunk size for streaming
_CHUNK = 1 << 14  # 16 KiB
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0.0.0 Safari/537.36")

def _guess_filename_from_headers(url: str, headers: aiohttp.typedefs.LooseHeaders) -> str:
    # Try Content-Disposition
    cd = headers.get("Content-Disposition") or headers.get("content-disposition")
    if cd:
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
        if m:
            return m.group(1).strip().strip('"')
    # Fallback to path
    name = os.path.basename(URL(url).path) or "download.bin"
    return name

async def http_download(
    url: str,
    dest_dir: str,
    status=None,  # your _ThrottleEdit
    max_retries: int = 3,
    connect_timeout: int = 20,
    read_timeout: int = 60,
) -> str:
    """
    Download URL to dest_dir, with:
      - Browser-like headers (UA/Accept/Accept-Language)
      - Redirects allowed
      - Resume on early EOF / partial responses (Range)
      - Progress updates via `status.edit(...)` if provided
    Returns: full file path
    Raises: last exception if it cannot complete within retry budget
    """
    os.makedirs(dest_dir, exist_ok=True)
    # Basic Referer: same origin
    try:
        referer = str(URL(url).with_path("/"))
    except Exception:
        referer = None

    timeout = aiohttp.ClientTimeout(
        total=None, connect=connect_timeout, sock_read=read_timeout
    )

    # We might not know size yet
    total_size: Optional[int] = None
    file_name: Optional[str] = None

    # Temp path to allow resume
    tmp_path = Path(dest_dir) / f".part-{int(time.time()*1000)}"
    bytes_done = 0
    attempt = 0
    last_err: Exception | None = None

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while attempt < max_retries:
            attempt += 1
            headers = {
                "User-Agent": _UA,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
            }
            if referer:
                headers["Referer"] = referer
            if bytes_done:
                headers["Range"] = f"bytes={bytes_done}-"

            try:
                async with session.get(url, headers=headers, allow_redirects=True) as resp:
                    # 206 for Range, 200 for normal
                    if resp.status not in (200, 206):
                        # Some CDNs respond 302 to a signed URL; aiohttp follows by default
                        resp.raise_for_status()

                    # First time populate filename and size
                    if file_name is None:
                        file_name = _guess_filename_from_headers(url, resp.headers)
                    # Infer total size
                    clen = resp.headers.get("Content-Length")
                    accept_ranges = (resp.headers.get("Accept-Ranges") or "").lower()
                    if clen and clen.isdigit():
                        # If resuming, the reported length is the remainder
                        remainder = int(clen)
                        total_size = (bytes_done + remainder) if bytes_done else remainder
                    # Open file for append on resume
                    mode = "ab" if bytes_done else "wb"
                    with open(tmp_path, mode) as f:
                        downloaded_this_attempt = 0
                        start = time.time()

                        async for chunk in resp.content.iter_chunked(_CHUNK):
                            if not chunk:
                                continue
                            f.write(chunk)
                            bytes_done += len(chunk)
                            downloaded_this_attempt += len(chunk)

                            # Progress display (optional)
                            if status:
                                # show progress with your standardized block (unknown total if None)
                                try:
                                    from . import messages as M  # local import to avoid cycles
                                except Exception:
                                    M = None
                                if M and total_size:
                                    pct = bytes_done / total_size * 100
                                    # fabricate a tiny speed
                                    elapsed = max(0.001, time.time() - start)
                                    spd = downloaded_this_attempt / elapsed
                                    progress = M.progress_block(
                                        pct=pct,
                                        current_mb=bytes_done / 1024 / 1024,
                                        total_mb=total_size / 1024 / 1024,
                                        speed_human=(f"{spd/1024/1024:.2f} MB/s" if spd >= 1024*1024
                                                     else f"{spd/1024:.2f} KB/s" if spd >= 1024
                                                     else f"{spd:.0f} B/s"),
                                    )
                                    # Use a neutral header here (URL path sets a separate header)
                                    await status.edit("⬇️ <b>Downloading from URL…</b>\n" + progress)

                    # If we got here, this attempt finished reading the body without errors
                    # Validate completion if we know the size
                    if total_size is None or bytes_done >= total_size:
                        # Finalize filename & move
                        final_path = Path(dest_dir) / (file_name or "download.bin")
                        # If final_path exists from old runs, overwrite
                        try:
                            if final_path.exists():
                                final_path.unlink()
                        except Exception:
                            pass
                        tmp_path.replace(final_path)
                        return str(final_path)

                    # If we read less than total_size, loop to resume
                    # Only if server advertises ranges or we simply try again
                    continue

            except (aiohttp.ClientPayloadError, aiohttp.ContentTypeError, aiohttp.ServerDisconnectedError) as e:
                # Early close or mismatch—try to resume if possible
                last_err = e
                await asyncio.sleep(1.0)
                continue
            except (aiohttp.ClientConnectorError, aiohttp.ClientOSError, asyncio.TimeoutError) as e:
                last_err = e
                await asyncio.sleep(1.0)
                continue
            except Exception as e:
                last_err = e
                break
        # Exhausted retries
        # Cleanup partial if present
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        if last_err:
            raise last_err
        raise RuntimeError("Download failed for unknown reason")
