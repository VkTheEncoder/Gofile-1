from __future__ import annotations
import asyncio
from typing import List, Optional, Tuple
from .gofile_api import GofileClient

class AccountPool:
    def __init__(self, tokens: List[str]):
        self.tokens = tokens[:]
        self._idx = 0
        self._lock = asyncio.Lock()
        self._exhausted = set()

    async def pick(self) -> Tuple[int, GofileClient]:
        """Return (index, client) for the next usable account; round-robin with exhaustion check."""
        async with self._lock:
            n = len(self.tokens)
            tried = 0
            while tried < n:
                idx = self._idx % n
                token = self.tokens[idx]
                client = GofileClient(token)
                # Check if quota exhausted (best-effort)
                async with client as c:
                    status = await c.is_quota_exhausted()
                if status is False or status is None:
                    # good to use (or unknown) → accept
                    chosen = idx
                    self._idx = (idx + 1) % n
                    return chosen, GofileClient(token)
                else:
                    # exhausted
                    self._exhausted.add(idx)
                    self._idx = (idx + 1) % n
                    tried += 1
            # If we reach here, all seem exhausted — return current index anyway
            idx = self._idx % n
            return idx, GofileClient(self.tokens[idx])

    async def mark_exhausted(self, idx: int):
        async with self._lock:
            self._exhausted.add(idx)

    def exhausted_indices(self):
        return sorted(self._exhausted)
