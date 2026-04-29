"""Drive ``claude setup-token`` from inside a PTY so the OAuth code-paste flow
is reachable over Telegram.

We use ``setup-token`` rather than ``auth login`` because the latter has no
code-paste prompt — it relies on a localhost browser callback that won't reach
us when the user is on a different device.

The CLI emits a heavily ANSI-decorated TUI; we strip escapes before pattern
matching. Confirmation polls ``claude auth status`` (authoritative JSON)
rather than scraping prompt text."""

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

_OSC_RE = re.compile(rb"\x1b\][^\x07]*\x07")
# Cursor-right `\e[NC` / cursor-forward `\e[NA-D` are visual whitespace; the
# CLI uses them to space out banner words. Replace with N spaces.
_CURSOR_RIGHT_RE = re.compile(rb"\x1b\[(\d*)C")
# All other CSI / SS2 / SS3 / charset selectors: drop entirely.
_OTHER_CSI_RE = re.compile(rb"\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]|\x1b[\x40-\x5f]")

_URL_START_RE = re.compile(rb"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
_URL_CONT_RE = re.compile(rb"[\r\n]+([A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+)")
_PROMPT_RE = re.compile(rb"Paste code here", re.IGNORECASE)
_TOKEN_ANCHOR_RE = re.compile(rb"Store this token", re.IGNORECASE)
# Token is base64url-ish; runs of >= 16 chars, possibly wrapped across lines.
_TOKEN_CHUNK_RE = re.compile(rb"[A-Za-z0-9_\-]{16,}")


def _extract_url(buf: bytes) -> str | None:
    """Find an OAuth URL, reassembling it across terminal line wraps. The CLI
    wraps at column width with bare ``\\r\\n``; nothing else (after our
    ANSI cleanup) sits between fragments. We greedily consume those fragments."""
    m = _URL_START_RE.search(buf)
    if not m:
        return None
    pieces: list[bytes] = [m.group(0)]
    pos = m.end()
    while True:
        cm = _URL_CONT_RE.match(buf, pos)
        if cm is None:
            break
        pieces.append(cm.group(1))
        pos = cm.end()
    return b"".join(pieces).decode(errors="replace").rstrip(".,)\"' ")


def _extract_token(cleaned: bytes) -> str | None:
    """``setup-token`` prints the OAuth token (possibly wrapped across two lines)
    immediately before the line ``Store this token securely…``. We anchor on that
    string and concatenate the token-shaped chunks that sit just before it."""
    anchor = _TOKEN_ANCHOR_RE.search(cleaned)
    if anchor is None:
        return None
    region = cleaned[: anchor.start()]
    chunks = _TOKEN_CHUNK_RE.findall(region[-400:])
    if not chunks:
        return None
    # Common case: 1-2 trailing chunks that together form the token. Take a
    # generous suffix and prune anything obviously-not-token (short prose words
    # are filtered out by the {16,} length minimum already).
    token = b"".join(chunks[-2:]).decode()
    return token if len(token) >= 32 else None

URL_TIMEOUT = 10.0
PROMPT_TIMEOUT = 10.0
CODE_TIMEOUT = 600.0  # User-reply window stays generous.
CONFIRM_TIMEOUT = 10.0
POLL_INTERVAL = 0.5
_OAUTH_ERROR_RE = re.compile(rb"OAuth error|status code \d+|Press Enter to retry", re.IGNORECASE)


class LoginError(RuntimeError):
    pass


async def auth_status(
    claude_bin: str, *, home: str | None = None, token: str | None = None,
) -> dict[str, object]:
    env = {"PATH": os.environ.get("PATH", "")}
    if home is not None:
        env["HOME"] = home
    if token is not None:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    proc = await asyncio.create_subprocess_exec(
        claude_bin, "auth", "status",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    out, _ = await proc.communicate()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"loggedIn": False}


def _clean(buf: bytes) -> bytes:
    """Strip ANSI: drop OSC + most CSI; translate cursor-right to literal spaces
    so the CLI's banner ("Paste\\e[1Ccode\\e[1Chere") doesn't lose word breaks."""
    out = _OSC_RE.sub(b"", buf)

    def _cursor_to_spaces(m: re.Match[bytes]) -> bytes:
        n_raw = m.group(1)
        n = int(n_raw) if n_raw else 1
        return b" " * min(n, 16)

    out = _CURSOR_RIGHT_RE.sub(_cursor_to_spaces, out)
    return _OTHER_CSI_RE.sub(b"", out)


class PtyReader:
    """Cumulative buffer fed from a master PTY fd. ``wait_for`` advances a cursor
    so successive calls don't re-match earlier text. Patterns are matched against
    the ANSI-stripped view."""

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
                    cleaned = _clean(bytes(self._buf))
                    m = pattern.search(cleaned, self._cursor)
                    if m:
                        self._cursor = m.end()
                        return m
                    if self._closed:
                        raise LoginError("pty closed before match")
                    await self._cond.wait()

        return await asyncio.wait_for(_waiter(), timeout=timeout)

    def advance_to_end(self) -> None:
        self._cursor = len(_clean(bytes(self._buf)))

    def cleaned(self) -> bytes:
        return _clean(bytes(self._buf))

    def close(self) -> None:
        with contextlib.suppress(ValueError, RuntimeError):
            self._loop.remove_reader(self._fd)


async def run_login(
    *,
    claude_bin: str,
    home: str,
    send_url: Callable[[str], Awaitable[None]],
    await_code: Callable[[float], Awaitable[str]],
    on_code_received: Callable[[], Awaitable[None]] | None = None,
) -> str:
    """Run a single ``setup-token`` pass. Sends the OAuth URL via ``send_url``,
    awaits the user-pasted code via ``await_code``, writes it into the PTY,
    then captures the printed OAuth token and returns it. The caller is
    responsible for storing the token and passing it to subsequent ``claude``
    spawns via ``CLAUDE_CODE_OAUTH_TOKEN``."""

    master, slave = pty.openpty()
    try:
        proc = await asyncio.create_subprocess_exec(
            claude_bin, "setup-token",
            stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True,
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": home,
                "TERM": "xterm-256color",
            },
        )
    finally:
        os.close(slave)

    reader = PtyReader(master)
    try:
        try:
            await reader.wait_for(_URL_START_RE, timeout=URL_TIMEOUT)
        except TimeoutError as e:
            tail = reader.cleaned()[-500:].decode(errors="replace")
            raise LoginError(f"no OAuth URL printed; tail: {tail!r}") from e

        # Give the rest of the wrapped URL a moment to land on stdout, then
        # re-extract from the cleaned buffer so we get the whole thing.
        await asyncio.sleep(0.3)
        url = _extract_url(reader.cleaned())
        if url is None:
            raise LoginError("URL match disappeared between waits")
        await send_url(url)

        # Confirm we see the paste prompt (sanity check) before asking the user.
        with contextlib.suppress(asyncio.TimeoutError, LoginError):
            await reader.wait_for(_PROMPT_RE, timeout=PROMPT_TIMEOUT)

        try:
            code = await await_code(CODE_TIMEOUT)
        except TimeoutError as e:
            raise LoginError("timed out waiting for code") from e

        if on_code_received is not None:
            await on_code_received()

        reader.advance_to_end()
        try:
            # Two writes: paste the code, then send \r as a separate keystroke.
            # The CLI's Ink input runs paste-detection: when many chars arrive
            # in one read(), \r in the same chunk is treated as a paste-internal
            # newline rather than submit, especially once the input wraps onto
            # a second visual line. Splitting the writes guarantees \r is seen
            # as a fresh keypress.
            os.write(master, code.strip().encode())
            await asyncio.sleep(0.15)
            os.write(master, b"\r")
        except OSError as e:
            raise LoginError(f"could not write code to pty: {e}") from e

        deadline = asyncio.get_running_loop().time() + CONFIRM_TIMEOUT
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            cleaned = reader.cleaned()
            token = _extract_token(cleaned)
            if token is not None:
                return token
            if _OAUTH_ERROR_RE.search(cleaned):
                tail = cleaned[-300:].decode(errors="replace").strip()
                raise LoginError(f"code rejected: {tail}")
            if proc.returncode is not None and token is None:
                # Re-check: sometimes the token lands at the very end and the
                # process has already exited.
                token = _extract_token(reader.cleaned())
                if token is not None:
                    return token
                tail = cleaned[-300:].decode(errors="replace").strip()
                raise LoginError(
                    f"claude exited without token (rc={proc.returncode}); tail: {tail!r}",
                )
            if asyncio.get_running_loop().time() >= deadline:
                tail = cleaned[-300:].decode(errors="replace").strip()
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
