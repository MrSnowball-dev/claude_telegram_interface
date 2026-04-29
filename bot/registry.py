"""In-memory registry of active ClaudeSession objects + idle sweeper."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from .claude_session import ClaudeSession

log = logging.getLogger(__name__)


class SessionRegistry:
    def __init__(self, idle_suspend_sec: int) -> None:
        self.idle_suspend_sec = idle_suspend_sec
        self._by_id: dict[str, ClaudeSession] = {}
        self._lock = asyncio.Lock()
        self._sweeper: asyncio.Task[None] | None = None

    def get(self, session_id: str) -> ClaudeSession | None:
        return self._by_id.get(session_id)

    async def put(self, session: ClaudeSession) -> None:
        async with self._lock:
            self._by_id[session.session_id] = session

    async def drop(self, session_id: str) -> None:
        async with self._lock:
            session = self._by_id.pop(session_id, None)
        if session is not None:
            await session.aclose()

    def start(self) -> None:
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(self._sweep())

    async def stop(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sweeper
            self._sweeper = None
        async with self._lock:
            sessions = list(self._by_id.values())
            self._by_id.clear()
        for s in sessions:
            await s.aclose()

    async def _sweep(self) -> None:
        try:
            while True:
                await asyncio.sleep(30)
                cutoff = time.monotonic() - self.idle_suspend_sec
                victims: list[ClaudeSession] = []
                for s in list(self._by_id.values()):
                    if s.alive and s.last_used and s.last_used < cutoff:
                        victims.append(s)
                for s in victims:
                    log.info("idle-suspending session %s", s.session_id)
                    try:
                        await s.suspend()
                    except Exception as e:
                        log.warning("suspend failed for %s: %s", s.session_id, e)
        except asyncio.CancelledError:
            pass
