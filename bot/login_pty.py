"""Drive ``claude auth login`` from inside a PTY so the OAuth flow is reachable
over Telegram. Native asyncio + stdlib ``pty`` (no pexpect dependency)."""

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

_URL_RE = re.compile(rb"https?://[^\s\x1b]+")
_SUCCESS_RE = re.compile(rb"(?i)(success|logged\s+in|authenticated)")
_ERROR_RE = re.compile(rb"(?i)(invalid|incorrect|error|failed)")
_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]")

URL_TIMEOUT = 30.0
CODE_TIMEOUT = 600.0
CONFIRM_TIMEOUT = 60.0


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
    """Cumulative buffer fed from a master PTY fd; coroutines wait for regex matches."""

    def __init__(self, master_fd: int) -> None:
        self._fd = master_fd
        self._buf = bytearray()
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
        # Notify under loop; create_task ensures we're inside a coroutine context.
        asyncio.create_task(self._notify())

    async def _notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    async def wait_for(self, pattern: re.Pattern[bytes], timeout: float) -> re.Match[bytes]:
        async def _waiter() -> re.Match[bytes]:
            async with self._cond:
                while True:
                    m = pattern.search(self._buf)
                    if m:
                        return m
                    if self._closed:
                        raise LoginError("pty closed")
                    await self._cond.wait()

        return await asyncio.wait_for(_waiter(), timeout=timeout)

    def close(self) -> None:
        with contextlib.suppress(ValueError, RuntimeError):
            self._loop.remove_reader(self._fd)


async def run_login(
    *,
    claude_bin: str,
    send_url: Callable[[str], Awaitable[None]],
    await_code: Callable[[float], Awaitable[str]],
    notify_invalid: Callable[[int], Awaitable[None]],
    max_attempts: int = 3,
) -> None:
    """Run an interactive login. Calls ``send_url`` once with the OAuth URL,
    then loops up to ``max_attempts`` codes via ``await_code`` until the CLI
    confirms success. Raises :class:`LoginError` on any unrecoverable failure."""

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
            raise LoginError("timed out waiting for OAuth URL") from e

        url = _ANSI_RE.sub(b"", m.group(0)).decode("utf-8", errors="replace").rstrip(".,)")
        await send_url(url)

        for attempt in range(1, max_attempts + 1):
            try:
                code = await await_code(CODE_TIMEOUT)
            except TimeoutError as e:
                raise LoginError("timed out waiting for code") from e

            os.write(master, (code.strip() + "\n").encode())

            done, pending = await asyncio.wait(
                {
                    asyncio.create_task(reader.wait_for(_SUCCESS_RE, timeout=CONFIRM_TIMEOUT)),
                    asyncio.create_task(reader.wait_for(_ERROR_RE, timeout=CONFIRM_TIMEOUT)),
                },
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()

            won = next(iter(done))
            try:
                match = won.result()
            except (TimeoutError, LoginError) as e:
                raise LoginError(f"no confirmation after code: {e}") from e

            if _SUCCESS_RE.search(match.group(0)):
                return
            await notify_invalid(max_attempts - attempt)

        raise LoginError("invalid code; out of attempts")
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
