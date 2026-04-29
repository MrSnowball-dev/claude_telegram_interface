"""One persistent ``claude --print`` subprocess per active session.

The OS process is disposable; durable identity is the ``session_id`` passed via
``--session-id`` on first start and ``--resume`` thereafter. Idle sessions are
suspended (process killed) and respawned on the next user turn.

stdin/stdout are line-delimited stream-json. We parse each line, dispatch
semantic events to a callback, and treat ``result`` as the turn-end marker.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

_BUFFER_LIMIT = 8 << 20  # 8 MiB stdout pipe limit; defaults are 64 KiB and deadlock.
_TURN_WATCHDOG_SEC = 120.0  # No event for this long → SIGINT.
_TURN_HARD_KILL_SEC = 30.0  # Then SIGTERM after this many more seconds.


# ---- semantic events ------------------------------------------------------


@dataclass(slots=True)
class TurnStarted:
    session_id: str


@dataclass(slots=True)
class TextDelta:
    text: str


@dataclass(slots=True)
class ToolCall:
    tool: str
    summary: str


@dataclass(slots=True)
class Thinking:
    pass


@dataclass(slots=True)
class TurnEnded:
    final_text: str
    is_error: bool
    error: str | None


SemanticEvent = TurnStarted | TextDelta | ToolCall | Thinking | TurnEnded


def parse_event(raw: dict[str, Any], state: dict[str, Any]) -> SemanticEvent | None:
    """Map one stream-json record to a semantic event. ``state`` accumulates
    per-turn scratch (currently just tool-input partials keyed by content-block index)."""
    kind = raw.get("type")

    if kind == "system":
        if raw.get("subtype") == "init":
            sid = raw.get("session_id")
            if sid:
                return TurnStarted(sid)
        return None

    if kind == "result":
        if raw.get("is_error"):
            err = raw.get("api_error_status") or raw.get("result") or "unknown error"
            return TurnEnded("", True, str(err))
        return TurnEnded(str(raw.get("result", "")), False, None)

    if kind != "stream_event":
        return None

    event = raw.get("event") or {}
    et = event.get("type")

    if et == "content_block_start":
        block = event.get("content_block") or {}
        btype = block.get("type")
        idx = event.get("index")
        if btype == "tool_use":
            state.setdefault("tool_blocks", {})[idx] = {
                "name": block.get("name", "tool"), "input": "",
            }
            return ToolCall(tool=block.get("name", "tool"), summary="")
        if btype == "thinking":
            return Thinking()
        return None

    if et == "content_block_delta":
        delta = event.get("delta") or {}
        dtype = delta.get("type")
        if dtype == "text_delta":
            text = delta.get("text", "")
            if text:
                return TextDelta(text)
            return None
        if dtype == "input_json_delta":
            idx = event.get("index")
            tb = state.get("tool_blocks", {}).get(idx)
            if tb is not None:
                tb["input"] += delta.get("partial_json", "")
            return None
        return None

    if et == "content_block_stop":
        idx = event.get("index")
        tb = state.get("tool_blocks", {}).pop(idx, None)
        if tb is not None:
            return ToolCall(tool=tb["name"], summary=tb["input"])
        return None

    return None


# ---- session --------------------------------------------------------------


EventHandler = Callable[[SemanticEvent], Awaitable[None]]


class ClaudeSession:
    """Wraps one durable Claude session_id. Spawns/suspends the underlying process."""

    __slots__ = (
        "session_id", "user_id", "cwd", "perm_mode", "allowed_tools", "claude_bin",
        "_proc", "_reader_task", "_lock", "_known", "last_used",
    )

    def __init__(
        self,
        *,
        session_id: str,
        user_id: int,
        cwd: str,
        perm_mode: str,
        allowed_tools: tuple[str, ...],
        claude_bin: str,
    ) -> None:
        self.session_id = session_id
        self.user_id = user_id
        self.cwd = cwd
        self.perm_mode = perm_mode
        self.allowed_tools = allowed_tools
        self.claude_bin = claude_bin
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._known = False  # True once the CLI has acknowledged the session_id at least once.
        self.last_used = 0.0

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def _spawn(self) -> None:
        args = [
            self.claude_bin, "--print",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--permission-mode", self.perm_mode,
            "--add-dir", self.cwd,
        ]
        if self.allowed_tools:
            args += ["--allowedTools", " ".join(self.allowed_tools)]
        if self._known:
            args += ["--resume", self.session_id]
        else:
            args += ["--session-id", self.session_id]

        self._proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_BUFFER_LIMIT,
            start_new_session=True,
        )
        log.info("spawned claude pid=%s session=%s cwd=%s", self._proc.pid, self.session_id, self.cwd)

    async def ensure_running(self) -> None:
        if self.alive:
            return
        await self._spawn()

    async def suspend(self) -> None:
        async with self._lock:
            await self._terminate_locked()

    async def _terminate_locked(self) -> None:
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
            self._reader_task = None
        proc = self._proc
        if proc is None:
            return
        if proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        self._proc = None

    async def aclose(self) -> None:
        await self.suspend()

    async def run_turn(self, user_text: str, on_event: EventHandler) -> None:
        """Send a user message and stream events to ``on_event`` until ``TurnEnded``.

        Survives one mid-turn crash by respawning and replaying the user message once.
        """
        async with self._lock:
            try:
                await self._run_turn_once(user_text, on_event)
                return
            except _ProcessGone:
                log.warning("claude proc gone mid-turn; respawning and retrying")
                await self._terminate_locked()
                self._known = True  # We had a session_id; resume it next time.
                try:
                    await self._run_turn_once(user_text, on_event)
                except _ProcessGone as e:
                    await on_event(TurnEnded("", True, str(e) or "session crashed"))

    async def _run_turn_once(self, user_text: str, on_event: EventHandler) -> None:
        await self.ensure_running()
        proc = self._proc
        assert proc is not None and proc.stdin is not None and proc.stdout is not None

        line = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": user_text}]},
        }, ensure_ascii=False) + "\n"
        try:
            proc.stdin.write(line.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise _ProcessGone(str(e)) from e

        scratch: dict[str, Any] = {}
        last_event_at = asyncio.get_running_loop().time()
        sigint_sent = False

        while True:
            try:
                line_bytes = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=10.0,
                )
            except TimeoutError:
                now = asyncio.get_running_loop().time()
                idle = now - last_event_at
                if idle > _TURN_WATCHDOG_SEC + _TURN_HARD_KILL_SEC:
                    log.warning("turn hard timeout; killing")
                    if proc.returncode is None:
                        proc.kill()
                    raise _ProcessGone("watchdog hard timeout") from None
                if idle > _TURN_WATCHDOG_SEC and not sigint_sent:
                    log.warning("turn soft timeout; sending SIGINT")
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
                    sigint_sent = True
                continue

            if not line_bytes:
                rc = proc.returncode
                raise _ProcessGone(f"stdout closed (rc={rc})")

            last_event_at = asyncio.get_running_loop().time()
            try:
                raw = json.loads(line_bytes)
            except json.JSONDecodeError:
                log.debug("non-JSON line: %r", line_bytes[:200])
                continue

            evt = parse_event(raw, scratch)
            if evt is None:
                continue
            if isinstance(evt, TurnStarted):
                self._known = True
            await on_event(evt)
            if isinstance(evt, TurnEnded):
                self.last_used = last_event_at
                return


class _ProcessGone(RuntimeError):
    pass
