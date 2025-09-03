from __future__ import annotations

import os, time, asyncio, inspect, re, json
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote

import aiohttp
from aiohttp import MultipartWriter, payload

API_BASE    = "https://api.gofile.io"
UPLOAD_URL  = "https://upload.gofile.io/uploadfile"  # global upload endpoint (no getServer)

# ---------- filename normalization ----------

_INVALID = re.compile(r'[\\/:*?"<>|\x00-\x1f]')  # Windows-reserved + control chars

def _sanitize_filename(name: str, max_len: int = 180) -> str:
    name = unquote(name or "").strip().strip(". ")
    name = _INVALID.sub("_", name) or "download.bin"
    if len(name) > max_len:
        if "." in name:
            stem, ext = name.rsplit(".", 1)
            keep = max(1, max_len - len(ext) - 1)
            name = (stem[:keep] or "file") + "." + ext
        else:
            name = name[:max_len]
    return name

# ---------- async file iterator ----------

async def _iter_file(path: str, chunk_size: int = 1024 * 1024, on_chunk=None):
    """Chunked async reader with optional progress callback (sync or async)."""
    loop = asyncio.get_event_loop()

    def _read(f, n):
        return f.read(n)

    with open(path, "rb") as f:
        while True:
            chunk = await loop.run_in_executor(None, _read, f, chunk_size)
            if not chunk:
                break
            if on_chunk:
                if inspect.iscoroutinefunction(on_chunk):
                    await on_chunk(len(chunk))
                else:
                    try:
                        on_chunk(len(chunk))
                    except Exception:
                        pass
            yield chunk

# ---------- client ----------

class GofileClient:
    def __init__(self, token: str, session: Optional[aiohttp.ClientSession] = None):
        self.token = token
        self.session = session
        self._owned_session = False

    async def __aenter__(self):
        if self.session is None:
            timeout = aiohttp.ClientTimeout(total=3600)  # 1 hour for big uploads
            self.session = aiohttp.ClientSession(timeout=timeout)
            self._owned_session = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._owned_session and self.session:
            await self.session.close()

    def _headers(self) -> Dict[str, str]:
        # GoFile expects the token as-is (no "Bearer ")
        return {"Authorization": self.token}

    async def get_account_id(self) -> Optional[str]:
        url = f"{API_BASE}/accounts/getid"
        async with self.session.get(url, headers=self._headers()) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            return data.get("data") or data.get("accountId") or data.get("id")

    async def get_account_info(self, account_id: Optional[str] = None) -> Dict[str, Any]:
        if not account_id:
            account_id = await self.get_account_id()
            if not account_id:
                return {}
        url = f"{API_BASE}/accounts/{account_id}"
        async with self.session.get(url, headers=self._headers()) as resp:
            if resp.status != 200:
                return {}
            return await resp.json(content_type=None)

    @staticmethod
    def _extract_usage(info: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
        """Return (used_bytes, limit_bytes) if present, else (None, None)."""
        data = info.get("data", info)

        traffic = data.get("traffic") or data.get("monthlyTraffic") or data.get("bandwidth")
        if isinstance(traffic, dict):
            used  = traffic.get("used") or traffic.get("current") or traffic.get("value")
            limit = traffic.get("limit") or traffic.get("max") or traffic.get("quota")
            if isinstance(used, (int, float)) and isinstance(limit, (int, float)):
                return int(used), int(limit)

        used  = data.get("trafficUsed") or data.get("monthlyTrafficUsed")
        limit = data.get("trafficLimit") or data.get("monthlyTrafficLimit")
        if isinstance(used, (int, float)) and isinstance(limit, (int, float)):
            return int(used), int(limit)

        return None, None

    async def is_quota_exhausted(self, threshold: float = 0.995) -> Optional[bool]:
        info = await self.get_account_info()
        if not info:
            return None
        used, limit = self._extract_usage(info)
        if used is None or limit is None or limit == 0:
            return None
        return (used / limit) >= threshold

    async def upload_file(
        self,
        file_path: str,
        folder_id: Optional[str] = None,
        progress_status=None
    ) -> Dict[str, Any]:
        """
        Upload via the global endpoint; no /getServer step.
        """
        params: Dict[str, Any] = {}
        if folder_id:
            params["folderId"] = folder_id

        file_size = os.path.getsize(file_path)
        disp_name = _sanitize_filename(os.path.basename(file_path))

        # progress state — clamp ≤99.9% during stream
        last = {"t": time.time(), "sent": 0}
        def on_chunk(n: int):
            last["sent"] += n
            if not progress_status:
                return
            now = time.time()
            if now - last["t"] >= 1:
                try:
                    pct = (last["sent"] / file_size * 100) if file_size else 0.0
                    pct = min(pct, 99.9)  # don't show 100% until server replies
                    asyncio.create_task(progress_status.edit(f"⬆️ Uploading… {pct:.1f}%"))
                except Exception:
                    pass
                last["t"] = now

        # multipart with clean display name (no disk rename)
        mp = MultipartWriter("form-data")
        mp.append(
            payload.AsyncIterablePayload(_iter_file(file_path, 1024 * 1024, on_chunk)),
            {"Content-Disposition": f'form-data; name="file"; filename="{disp_name}"'},
        )

        async with self.session.post(UPLOAD_URL, data=mp, params=params, headers=self._headers()) as resp:
            # final “100% (processing…)”
            if progress_status:
                try:
                    await progress_status.edit("⬆️ Uploading… 100% (processing…)")
                except Exception:
                    pass

            raw_text = await resp.text()
            try:
                j = json.loads(raw_text)
            except Exception:
                j = {"status": "unknown", "raw": raw_text}

            data = j.get("data") or j
            normalized = {
                "status": j.get("status") or data.get("status") or ("ok" if resp.status == 200 else "error"),
                "downloadPage": data.get("downloadPage") or data.get("downloadUrl") or data.get("page"),
                "contentId": data.get("contentId") or data.get("fileId") or data.get("id") or data.get("code"),
                "fileName": data.get("fileName") or disp_name,
                "raw": j,
            }

            if resp.status != 200 or normalized["status"] not in ("ok", "OK", "success"):
                normalized["error"] = True
                normalized["httpStatus"] = resp.status

            return normalized
