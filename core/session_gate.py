from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator


class SessionGateRegistry:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    @asynccontextmanager
    async def hold(self, session_id: str) -> AsyncIterator[None]:
        async with self.lock_for(session_id):
            yield
