from __future__ import annotations
import os, time
from aiohttp import MultipartWriter, payload
import asyncio
import os
from typing import Any, Dict, Optional, Tuple

API_BASE = "https://api.gofile.io"
UPLOAD_URL = "https://upload.gofile.io/uploadfile"

async def _iter_file(path, chunk_size, on_chunk):
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            await on_chunk(len(chunk))
            yield chunk

class GofileClient:
    def __init__(self, token: str, session: Optional[aiohttp.ClientSession] = None):
        self.token = token
        self.session = session
        self._owned_session = False

    async def __aenter__(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()
            self._owned_session = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._owned_session and self.session:
            await self.session.close()

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def get_account_id(self) -> Optional[str]:
        url = f"{API_BASE}/accounts/getid"
        async with self.session.get(url, headers=self._headers()) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            # defensive parsing (API is beta)
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
            return await resp.json()

    @staticmethod
    def _extract_usage(info: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
        """Return (used_bytes, limit_bytes) if present, else (None, None)."""
        # Try multiple likely shapes since API is beta.
        # Common possibilities:
        # { data: { traffic: { used: 123, limit: 100*GB } } }
        data = info.get("data", info)
        candidates = []
        # 1) nested traffic
        traffic = data.get("traffic") or data.get("monthlyTraffic") or data.get("bandwidth")
        if isinstance(traffic, dict):
            used = traffic.get("used") or traffic.get("current") or traffic.get("value")
            limit = traffic.get("limit") or traffic.get("max") or traffic.get("quota")
            if isinstance(used, (int, float)) and isinstance(limit, (int, float)):
                return int(used), int(limit)

        # 2) flat
        used = data.get("trafficUsed") or data.get("monthlyTrafficUsed")
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

async def upload_file(self, file_path: str, folder_id: Optional[str] = None, progress_status=None) -> Dict[str, Any]:
    params = {}
    if folder_id:
        params["folderId"] = folder_id

    size = os.path.getsize(file_path)
    uploaded = 0
    start = time.time()

    async def on_chunk(n):
        nonlocal uploaded
        uploaded += n
        if progress_status:
            # throttle via the _ThrottleEdit already applied in handlers.py
            pct = (uploaded/size*100) if size else 0.0
            elapsed = max(0.001, time.time() - start)
            spd = uploaded / elapsed
            bar = "█"*int(pct/10) + "░"*(10-int(pct/10))
            text = (
                "⬆️ Uploading to GoFile…\n"
                f"[{bar}] {pct:.1f}%\n"
                f"{uploaded/1024/1024:.2f}/{size/1024/1024:.2f} MB\n"
                f"Speed: { (spd/1024/1024):.2f } MB/s"
            )
            try:
                await progress_status.edit(text)
            except Exception:
                pass

    # Build a multipart with a streaming payload
    mp = MultipartWriter("form-data")
    # file field
    part = mp.append(_iter_file(file_path, 1024*1024, on_chunk))  # 1MB chunks
    part.set_content_disposition("form-data", name="file", filename=os.path.basename(file_path))

    async with self.session.post(UPLOAD_URL, data=mp, params=params, headers=self._headers()) as resp:
        j = await resp.json(content_type=None)
        if resp.status != 200:
            return {"error": True, "status": resp.status, "response": j}
        if isinstance(j, dict):
            if "data" in j:
                return j["data"]
            return j
        return {"raw": j}
