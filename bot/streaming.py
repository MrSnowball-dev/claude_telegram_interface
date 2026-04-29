"""Throttled draft updater. One DraftStreamer per active turn.

Calls ``send_draft(text)`` no faster than ``min_interval`` seconds, skipping updates that
are too small. ``finalize(full)`` cancels pending work and commits the message via
``commit(full)``; the draft animation simply stops at its last frame.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable

from .render import draft_view


class DraftStreamer:
    __slots__ = (
        "send_draft", "commit", "min_interval", "_buf",
        "_last_sent", "_last_sent_at", "_task", "_sealed",
    )

    def __init__(
        self,
        send_draft: Callable[[str], Awaitable[None]],
        commit: Callable[[str], Awaitable[None]],
        *,
        min_interval: float = 0.25,
    ) -> None:
        self.send_draft = send_draft
        self.commit = commit
        self.min_interval = min_interval
        self._buf = ""
        self._last_sent = ""
        self._last_sent_at = 0.0
        self._task: asyncio.Task[None] | None = None
        self._sealed = False

    def append(self, delta: str) -> None:
        if self._sealed or not delta:
            return
        self._buf += delta
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        try:
            while not self._sealed and self._buf != self._last_sent:
                wait = self.min_interval - (time.monotonic() - self._last_sent_at)
                if wait > 0:
                    await asyncio.sleep(wait)
                if self._sealed:
                    return
                # Skip tiny updates to avoid one-char spam.
                grew = len(self._buf) - len(self._last_sent)
                elapsed = time.monotonic() - self._last_sent_at
                if grew < 8 and elapsed < 1.0 and self._buf != self._last_sent:
                    await asyncio.sleep(self.min_interval)
                    continue
                snap = self._buf
                view = draft_view(snap)
                try:
                    await self.send_draft(view)
                except Exception:
                    # Drop this draft tick; let the next iteration retry.
                    await asyncio.sleep(self.min_interval)
                    continue
                self._last_sent = snap
                self._last_sent_at = time.monotonic()
        except asyncio.CancelledError:
            pass

    async def finalize(self, full_text: str) -> None:
        self._sealed = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        if full_text:
            await self.commit(full_text)
