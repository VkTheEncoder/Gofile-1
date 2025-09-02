# app/http_downloader.py
from __future__ import annotations

import asyncio
import os
import re
import tempfile
from typing import Optional, Dict, Any
from urllib.parse import urlparse, unquote

import aiohttp

_FILENAME_RE = re.compile(r'filename\*=UTF-8\'\'([^;]+)|filename="([^"]+)"|filename=([^;]+)', re.I)

def _guess_filename_from_headers(headers: aiohttp.typedefs.LooseHeaders, url: str, default_ext: str = "") -> str:
    cd = headers.get("Content-Disposition") or headers.get("content-disposition")
    if cd:
        m = _Filename_R = _FILENAME_RE.search(cd)
        if m:
            fname = next(g for g in m.groups() if g)  # first non-None capture
            return unquote(fname).strip().strip('"')
    # fallback to URL path
    path = urlparse(url).path
    base = os.path.basename(path)
    if base:
        return unquote(base)
    return f"download{default_ext}"

async def http_download(
    url: str,
    dest_dir: Optional[str] = None,
    status=None,  # Telegram message editor (optional)
    session: Optional[aiohttp.ClientSession] = None,
    chunk_size: int = 4 * 1024 * 1024,  # 4MB chunks for speed
    headers: Optional[Dict[str, str]] = None,
    follow_redirects: bool = True,
) -> str:
    """
    Downloads a file via HTTP(S) to a temp path and returns the file path.
    Shows progress via `status.edit(...)` if provided.
    """
    close_session = False
    if session is None:
        timeout = aiohttp.ClientTimeout(total=None, sock_read=600, sock_connect=30)
        session = aiohttp.ClientSession(timeout=timeout)
        close_session = True

    try:
        req_headers = {
            "User-Agent": "Mozilla/5.0 (compatible; GoFileBot/1.0; +https://example)",
            "Accept": "*/*",
        }
        if headers:
            req_headers.update(headers)

        async with session.get(url, headers=req_headers, allow_redirects=follow_redirects) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length", "0")) or None
            filename = _guess_filename_from_headers(resp.headers, url)
            suffix = os.path.splitext(filename)[1] if "." in filename else ""
            td = dest_dir or tempfile.gettempdir()
            os.makedirs(td, exist_ok=True)

            # pre-create a temp path using the header filename if present
            tmp_path = os.path.join(td, filename) if filename else tempfile.mkstemp(prefix="dl_", suffix=suffix)[1]
            # ensure uniqueness
            i = 1
            base, ext = os.path.splitext(tmp_path)
            while os.path.exists(tmp_path):
                tmp_path = f"{base}({i}){ext}"
                i += 1

            downloaded = 0
            t0 = asyncio.get_event_loop().time()
            with open(tmp_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(chunk_size):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    if status and (downloaded % (chunk_size * 2) == 0):  # throttle UI updates
                        try:
                            dt = max(0.001, asyncio.get_event_loop().time() - t0)
                            spd = downloaded / dt  # bytes/sec
                            if total:
                                pct = downloaded * 100.0 / total
                                await status.edit(
                                    f"⬇️ Downloading… {pct:.1f}%\n"
                                    f"{downloaded/1024/1024:.2f} / {total/1024/1024:.2f} MB\n"
                                    f"Speed: {spd/1024/1024:.2f} MB/s"
                                )
                            else:
                                await status.edit(
                                    f"⬇️ Downloading…\n"
                                    f"{downloaded/1024/1024:.2f} MB\n"
                                    f"Speed: {spd/1024/1024:.2f} MB/s"
                                )
                        except Exception:
                            pass

        # final status (optional)
        if status:
            try:
                if total:
                    await status.edit(f"✅ Downloaded {filename} ({total/1024/1024:.2f} MB). Uploading next…")
                else:
                    await status.edit(f"✅ Downloaded {filename}. Uploading next…")
            except Exception:
                pass

        return tmp_path
    finally:
        if close_session:
            await session.close()
