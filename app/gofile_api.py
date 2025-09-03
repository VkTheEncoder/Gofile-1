from __future__ import annotations

import os, time, asyncio, inspect, json
from typing import Any, Dict, Optional, Tuple

import aiohttp
from aiohttp import MultipartWriter, payload

API_BASE   = "https://api.gofile.io"
UPLOAD_URL = "https://upload.gofile.io/uploadfile"  # global endpoint, no /getServer

# ---------- async file iterator ----------

async def _iter_file(path: str, chunk_size: int = 4 * 1024 * 1024, on_chunk=None):
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
            timeout = aiohttp.ClientTimeout(total=3600)
            self.session = aiohttp.ClientSession(timeout=timeout)
            self._owned_session = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._owned_session and self.session:
            await self.session.close()

    # ----- account info -----

    def _auth_headers(self, as_guest: bool = False) -> Dict[str, str]:
        # Bearer token when present; no header for guest uploads
        if as_guest or not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    async def get_account_id(self) -> Optional[str]:
        url = f"{API_BASE}/accounts/getid"
        async with self.session.get(url, headers=self._auth_headers()) as resp:
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
        async with self.session.get(url, headers=self._auth_headers()) as resp:
            if resp.status != 200:
                return {}
            return await resp.json(content_type=None)

    @staticmethod
    def _extract_usage(info: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
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

    # ----- upload helpers -----

    @staticmethod
    def _normalize_response(resp_status: int, raw_text: str, fallback_name: str) -> Dict[str, Any]:
        """Return a predictable dict with downloadPage/contentId/fileName/error/httpStatus/raw."""
        try:
            j = json.loads(raw_text)
        except Exception:
            j = {"status": "unknown", "raw": raw_text}

        data = j.get("data") or j

        # Pull everything we can
        code = data.get("code") or data.get("id") or data.get("fileId") or data.get("contentId")
        link = (
            data.get("downloadPage") or data.get("downloadpage") or
            data.get("downloadUrl") or data.get("downloadURL") or
            data.get("page") or data.get("url") or data.get("link")
        )
        # If only a code exists, build the public page link
        if not link and code:
            link = f"https://gofile.io/d/{code}"

        fname = data.get("fileName") or data.get("filename") or fallback_name

        normalized = {
            "status": (j.get("status") or data.get("status") or ("ok" if resp_status == 200 else "error")).lower(),
            "downloadPage": link,
            "contentId": (data.get("contentId") or data.get("fileId") or code),
            "fileName": fname,
            "raw": j,
        }
        if resp_status != 200 or normalized["status"] not in ("ok", "success"):
            normalized["error"] = True
            normalized["httpStatus"] = resp_status
        return normalized

    async def _upload_once(
        self, file_path: str, folder_id: Optional[str], progress_status, as_guest: bool
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if folder_id and not as_guest:
            params["folderId"] = folder_id  # folderId needs auth

        file_size = os.path.getsize(file_path)
        disp_name = os.path.basename(file_path)  # ← keep EXACT same name the user has

        # live progress (cap at 99.9% until server responds)
        last = {"t": time.time(), "sent": 0}
        def on_chunk(n: int):
            last["sent"] += n
            if not progress_status:
                return
            now = time.time()
            if now - last["t"] >= 1:
                try:
                    pct = (last["sent"] / file_size * 100) if file_size else 0.0
                    pct = min(pct, 99.9)
                    asyncio.create_task(progress_status.edit(f"⬆️ Uploading… {pct:.1f}%"))
                except Exception:
                    pass
                last["t"] = now

        mp = MultipartWriter("form-data")
        mp.append(
            payload.AsyncIterablePayload(_iter_file(file_path, 4 * 1024 * 1024, on_chunk)),
            {"Content-Disposition": f'form-data; name="file"; filename="{disp_name}"'},
        )

        async with self.session.post(
            UPLOAD_URL, data=mp, params=params, headers=self._auth_headers(as_guest=as_guest)
        ) as resp:
            if progress_status:
                try:
                    await progress_status.edit("⬆️ Uploading… 100% (processing…)")
                except Exception:
                    pass
            text = await resp.text()
            return self._normalize_response(resp.status, text, disp_name)

    async def upload_file(
        self, file_path: str, folder_id: Optional[str] = None, progress_status=None
    ) -> Dict[str, Any]:
        # Try with Bearer token first
        first = await self._upload_once(file_path, folder_id, progress_status, as_guest=False)
        # If auth fails, retry once as guest
        if first.get("error") and first.get("httpStatus") in (401, 403):
            return await self._upload_once(file_path, None, progress_status, as_guest=True)
        return first
