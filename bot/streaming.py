"""Throttled draft updater. One DraftStreamer per active turn.

Calls ``send_draft(text)`` no faster than ``min_interval`` seconds. A heartbeat
task re-sends the most recent text every ``heartbeat_interval`` seconds during
quiet periods (thinking, tool execution) so Telegram doesn't silently retire
the draft animation after its ~25 s TTL — without that, long pauses would
make the draft disappear and the next update would replay the whole text
from scratch.

``finalize(full_text)`` cancels all pending work and commits the canonical
text via ``commit(full_text)``."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable

from .render import draft_view


class DraftStreamer:
    __slots__ = (
        "send_draft", "commit", "min_interval", "heartbeat_interval",
        "_buf", "_last_sent", "_last_sent_at",
        "_flush_task", "_heartbeat_task", "_sealed",
    )

    def __init__(
        self,
        send_draft: Callable[[str], Awaitable[None]],
        commit: Callable[[str], Awaitable[None]],
        *,
        min_interval: float = 0.35,
        heartbeat_interval: float = 12.0,
    ) -> None:
        self.send_draft = send_draft
        self.commit = commit
        self.min_interval = min_interval
        self.heartbeat_interval = heartbeat_interval
        self._buf = ""
        self._last_sent = ""
        self._last_sent_at = 0.0
        self._flush_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._sealed = False

    def append(self, delta: str) -> None:
        if self._sealed or not delta:
            return
        self._buf += delta
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _flush_loop(self) -> None:
        try:
            while not self._sealed and self._buf != self._last_sent:
                wait = self.min_interval - (time.monotonic() - self._last_sent_at)
                if wait > 0:
                    await asyncio.sleep(wait)
                if self._sealed:
                    return
                # Skip tiny diffs to avoid 1-char update spam.
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
                    await asyncio.sleep(self.min_interval)
                    continue
                self._last_sent = snap
                self._last_sent_at = time.monotonic()
        except asyncio.CancelledError:
            pass

    async def _heartbeat_loop(self) -> None:
        """Refresh the draft on quiet intervals so its 25-second silent expiry
        never fires. Re-sending the same text under the same draft_id is a no-op
        for the user (no visible animation) but resets Telegram's TTL."""
        try:
            while not self._sealed:
                await asyncio.sleep(self.heartbeat_interval)
                if self._sealed or not self._last_sent:
                    continue
                # Only heartbeat if no real update happened during this window.
                if time.monotonic() - self._last_sent_at < self.heartbeat_interval:
                    continue
                with contextlib.suppress(Exception):
                    await self.send_draft(draft_view(self._last_sent))
                    self._last_sent_at = time.monotonic()
        except asyncio.CancelledError:
            pass

    async def finalize(self, full_text: str) -> None:
        self._sealed = True
        for task in (self._flush_task, self._heartbeat_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._flush_task = None
        self._heartbeat_task = None
        if full_text:
            await self.commit(full_text)
