"""Drive ``claude auth login`` from inside a PTY so the OAuth flow is reachable
over Telegram. Native asyncio + stdlib ``pty`` (no pexpect dependency).

Confirmation is done by polling ``claude auth status`` rather than regex-matching
the PTY output, which is fragile (URLs and prompts can themselves contain words
like "error" / "fail")."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import pty
import re
import signal
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

_URL_RE = re.compile(rb"https?://[^\s\x1b\)]+")
_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]")

URL_TIMEOUT = 30.0
CODE_TIMEOUT = 600.0
CONFIRM_TIMEOUT = 45.0
POLL_INTERVAL = 1.0


class LoginError(RuntimeError):
    pass


async def auth_status(claude_bin: str) -> dict[str, object]:
    proc = await asyncio.create_subprocess_exec(
        claude_bin, "auth", "status",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"loggedIn": False}


class PtyReader:
    """Cumulative buffer fed from a master PTY fd. ``wait_for`` advances a cursor
    so successive calls don't re-match earlier text."""

    def __init__(self, master_fd: int) -> None:
        self._fd = master_fd
        self._buf = bytearray()
        self._cursor = 0
        self._cond = asyncio.Condition()
        self._closed = False
        loop = asyncio.get_running_loop()
        loop.add_reader(self._fd, self._on_readable)
        self._loop = loop

    def _on_readable(self) -> None:
        try:
            data = os.read(self._fd, 4096)
        except OSError:
            data = b""
        if not data:
            self._closed = True
            self._loop.remove_reader(self._fd)
        else:
            self._buf.extend(data)
        asyncio.create_task(self._notify())

    async def _notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    async def wait_for(self, pattern: re.Pattern[bytes], timeout: float) -> re.Match[bytes]:
        async def _waiter() -> re.Match[bytes]:
            async with self._cond:
                while True:
                    m = pattern.search(self._buf, self._cursor)
                    if m:
                        self._cursor = m.end()
                        return m
                    if self._closed:
                        raise LoginError("pty closed")
                    await self._cond.wait()

        return await asyncio.wait_for(_waiter(), timeout=timeout)

    def advance_to_end(self) -> None:
        """Drop everything written so far from future searches (call before sending input)."""
        self._cursor = len(self._buf)

    def snapshot(self) -> bytes:
        return bytes(self._buf)

    def close(self) -> None:
        with contextlib.suppress(ValueError, RuntimeError):
            self._loop.remove_reader(self._fd)


async def run_login(
    *,
    claude_bin: str,
    send_url: Callable[[str], Awaitable[None]],
    await_code: Callable[[float], Awaitable[str]],
) -> None:
    """Run a single OAuth login pass. Caller is responsible for retry by re-invoking.

    Sends the OAuth URL via ``send_url``, awaits the code via ``await_code``, writes
    it to the PTY, then polls ``claude auth status`` for confirmation. Raises
    :class:`LoginError` on any failure.
    """

    pre = await auth_status(claude_bin)
    if pre.get("loggedIn"):
        return  # Caller should short-circuit before invoking; tolerate either way.

    master, slave = pty.openpty()
    try:
        proc = await asyncio.create_subprocess_exec(
            claude_bin, "auth", "login",
            stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True,
            env={**os.environ, "TERM": "dumb", "NO_COLOR": "1", "BROWSER": "true"},
        )
    finally:
        os.close(slave)

    reader = PtyReader(master)
    try:
        try:
            m = await reader.wait_for(_URL_RE, timeout=URL_TIMEOUT)
        except TimeoutError as e:
            tail = reader.snapshot()[-400:].decode("utf-8", errors="replace")
            raise LoginError(f"no OAuth URL printed; tail: {tail!r}") from e

        url = _ANSI_RE.sub(b"", m.group(0)).decode("utf-8", errors="replace").rstrip(".,)")
        await send_url(url)

        try:
            code = await await_code(CODE_TIMEOUT)
        except TimeoutError as e:
            raise LoginError("timed out waiting for code") from e

        # Drop everything we've seen so far so the post-code wait doesn't get
        # confused by the URL prompt's earlier text.
        reader.advance_to_end()
        try:
            os.write(master, (code.strip() + "\n").encode())
        except OSError as e:
            raise LoginError(f"could not write code to pty: {e}") from e

        deadline = asyncio.get_running_loop().time() + CONFIRM_TIMEOUT
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            status = await auth_status(claude_bin)
            if status.get("loggedIn"):
                return
            if proc.returncode is not None:
                tail = reader.snapshot()[-400:].decode("utf-8", errors="replace")
                raise LoginError(f"claude exited without auth (rc={proc.returncode}); tail: {tail!r}")
            if asyncio.get_running_loop().time() >= deadline:
                tail = reader.snapshot()[-400:].decode("utf-8", errors="replace")
                raise LoginError(f"auth confirmation timed out; tail: {tail!r}")
    finally:
        reader.close()
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
        with contextlib.suppress(OSError):
            os.close(master)
