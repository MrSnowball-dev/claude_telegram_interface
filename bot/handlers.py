"""Telegram event routing. /start /help /new /cd /login /perm /lang and plain text → session."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass

from telethon import TelegramClient, events

from .bot_api import BotAPI, BotAPIError
from .claude_session import (
    ClaudeSession,
    SemanticEvent,
    TextDelta,
    Thinking,
    ToolCall,
    TurnEnded,
    TurnStarted,
)
from .config import Settings
from .i18n import available_languages, pick_lang, t
from .login_pty import LoginError, auth_status, run_login
from .paths import user_home as user_home_path
from .permissions import normalize_perm_mode
from .registry import SessionRegistry
from .render import chunk_message, tool_summary
from .state import State, User
from .streaming import DraftStreamer

log = logging.getLogger(__name__)

_AUTH_CACHE_TTL = 30.0  # seconds


@dataclass(slots=True)
class _PendingLogin:
    queue: asyncio.Queue[str]
    task: asyncio.Task[None]


class Handlers:
    def __init__(
        self,
        *,
        client: TelegramClient,
        settings: Settings,
        state: State,
        registry: SessionRegistry,
        bot_api: BotAPI,
    ) -> None:
        self.client = client
        self.settings = settings
        self.state = state
        self.registry = registry
        self.bot_api = bot_api
        self._pending_logins: dict[int, _PendingLogin] = {}
        self._auth_cache: dict[int, tuple[float, bool]] = {}  # user_id → (timestamp, logged_in)

    # ---- registration -----------------------------------------------------

    def register(self) -> None:
        self.client.add_event_handler(self._on_message, events.NewMessage(incoming=True))

    # ---- main dispatch ----------------------------------------------------

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        sender = await event.get_sender()
        if sender is None or sender.bot:
            return
        user_id = int(sender.id)
        chat_id = int(event.chat_id)

        if user_id not in self.settings.allowed_user_ids:
            await self._send(chat_id, t("not_allowed", "en"))
            return

        user = await self.state.get_or_create_user(
            user_id, pick_lang(getattr(sender, "lang_code", None)), self.settings.default_cwd,
        )
        user = await self._ensure_system_thread(user, chat_id)
        text = (event.raw_text or "").strip()

        if text.startswith("/"):
            await self._handle_command(event, user, text)
            return

        # Pending login takes precedence: a plain-text reply IS the verification code.
        pending = self._pending_logins.get(user_id)
        if pending is not None:
            await pending.queue.put(text)
            return

        if not await self._is_logged_in(user):
            await self._send(chat_id, t("auth_required", user.lang), thread_id=user.system_thread_id)
            return

        await self._route_to_session(event, user, text)

    # ---- commands ---------------------------------------------------------

    async def _handle_command(self, event: events.NewMessage.Event, user: User, text: str) -> None:
        cmd, _, rest = text.partition(" ")
        cmd = cmd.split("@", 1)[0].lower()
        rest = rest.strip()

        # Open commands — no auth required.
        if cmd in ("/start", "/help"):
            await self._send(
                int(event.chat_id),
                t("welcome" if cmd == "/start" else "help", user.lang),
                thread_id=user.system_thread_id,
            )
            if cmd == "/start" and not await self._is_logged_in(user):
                await self._send(
                    int(event.chat_id),
                    t("auth_required", user.lang),
                    thread_id=user.system_thread_id,
                )
            return
        if cmd == "/lang":
            await self._cmd_lang(event, user, rest)
            return
        if cmd == "/login":
            await self._cmd_login(event, user)
            return
        if cmd == "/logout":
            await self._cmd_logout(event, user)
            return

        # Gated commands.
        if not await self._is_logged_in(user):
            await self._send(
                int(event.chat_id), t("auth_required", user.lang),
                thread_id=user.system_thread_id,
            )
            return

        if cmd == "/new":
            await self._cmd_new(event, user)
        elif cmd == "/cd":
            await self._cmd_cd(event, user, rest)
        elif cmd == "/perm":
            await self._cmd_perm(event, user, rest)
        else:
            await self._send(
                int(event.chat_id), t("help", user.lang), thread_id=user.system_thread_id,
            )

    async def _cmd_cd(self, event, user: User, rest: str) -> None:
        if not rest:
            await self._send(
                int(event.chat_id), t("cd_missing", user.lang), thread_id=user.system_thread_id,
            )
            return
        path = os.path.realpath(rest)
        if not os.path.isdir(path) or not os.access(path, os.R_OK):
            await self._send(
                int(event.chat_id),
                t("cd_bad", user.lang, path=path),
                thread_id=user.system_thread_id,
            )
            return
        await self.state.set_user_cwd(user.user_id, path)
        await self._send(
            int(event.chat_id),
            t("cd_ok", user.lang, path=path),
            thread_id=user.system_thread_id,
        )

    async def _cmd_perm(self, event, user: User, rest: str) -> None:
        mode = normalize_perm_mode(rest)
        if mode is None:
            await self._send(
                int(event.chat_id), t("perm_bad", user.lang),
                thread_id=user.system_thread_id,
            )
            return
        await self.state.set_user_perm(user.user_id, mode)
        await self._send(
            int(event.chat_id),
            t("perm_set", user.lang, mode=mode),
            thread_id=user.system_thread_id,
        )

    async def _cmd_lang(self, event, user: User, rest: str) -> None:
        langs = available_languages()
        code = rest.lower().split("-", 1)[0]
        if code not in langs:
            await self._send(
                int(event.chat_id),
                t("lang_bad", user.lang, available=", ".join(sorted(langs))),
                thread_id=user.system_thread_id,
            )
            return
        await self.state.set_user_lang(user.user_id, code)
        await self._send(
            int(event.chat_id),
            t("lang_set", code, name=langs[code]),
            thread_id=user.system_thread_id,
        )

    async def _cmd_new(self, event, user: User) -> None:
        # Refresh user (cwd / token may have changed since dispatch).
        user = await self.state.get_or_create_user(user.user_id, user.lang, self.settings.default_cwd)
        chat_id = int(event.chat_id)
        try:
            topic = await self.bot_api.create_forum_topic(
                chat_id=chat_id,
                name=f"claude · {user.cwd.split('/')[-1] or 'session'}",
            )
            thread_id: int | None = int(topic["message_thread_id"])
        except BotAPIError as e:
            log.warning("createForumTopic failed (%s); falling back to no topic", e)
            thread_id = None

        session_id = str(uuid.uuid4())
        sess = await self.state.new_session(
            session_id=session_id,
            user_id=user.user_id,
            thread_id=thread_id,
            cwd=user.cwd,
            perm_mode=user.perm_mode,
        )
        await self.state.set_active_session(user.user_id, session_id)
        await self.registry.put(ClaudeSession(
            session_id=session_id,
            user_id=user.user_id,
            cwd=sess.cwd,
            perm_mode=sess.perm_mode,
            allowed_tools=self.settings.allowed_tools,
            claude_bin=self.settings.claude_bin,
            home=user_home_path(self.settings.data_dir, user.user_id),
            oauth_token=user.oauth_token,
        ))
        await self._send(
            chat_id, t("session_new", user.lang, cwd=sess.cwd), thread_id=thread_id,
        )

    async def _cmd_login(self, event, user: User) -> None:
        chat_id = int(event.chat_id)
        if user.user_id in self._pending_logins:
            await self._send(
                chat_id, t("login_already", user.lang), thread_id=user.system_thread_id,
            )
            return

        if await self._is_logged_in(user, force=True):
            status = await auth_status(
                self.settings.claude_bin,
                home=user_home_path(self.settings.data_dir, user.user_id),
                token=user.oauth_token,
            )
            email = str(status.get("email", ""))
            await self._send(
                chat_id,
                t("login_already_authed", user.lang, email=email),
                thread_id=user.system_thread_id,
            )
            return

        await self._send(
            chat_id, t("login_start", user.lang), thread_id=user.system_thread_id,
        )
        queue: asyncio.Queue[str] = asyncio.Queue()
        sys_thread = user.system_thread_id
        home = user_home_path(self.settings.data_dir, user.user_id)

        async def send_url(url: str) -> None:
            await self._send(
                chat_id, t("login_url", user.lang, url=url), thread_id=sys_thread,
            )

        async def await_code(timeout: float) -> str:
            return await asyncio.wait_for(queue.get(), timeout=timeout)

        async def on_code_received() -> None:
            await self._send(
                chat_id, t("login_checking", user.lang), thread_id=sys_thread,
            )

        async def runner() -> None:
            try:
                token = await run_login(
                    claude_bin=self.settings.claude_bin,
                    home=home,
                    send_url=send_url,
                    await_code=await_code,
                    on_code_received=on_code_received,
                )
                await self.state.set_user_token(user.user_id, token)
                self._auth_cache[user.user_id] = (time.monotonic(), True)
                # Update any registry entries for this user with the new token.
                for sess in list(self.registry.iter_user(user.user_id)):
                    sess.oauth_token = token
                await self._send(chat_id, t("login_done", user.lang), thread_id=sys_thread)
            except LoginError as e:
                log.warning("login failed: %s", e)
                await self._send(
                    chat_id, t("login_failed", user.lang, detail=str(e)),
                    thread_id=sys_thread,
                )
            except Exception as e:
                log.exception("login flow crashed")
                await self._send(
                    chat_id, t("login_failed", user.lang, detail=repr(e)),
                    thread_id=sys_thread,
                )
            finally:
                self._pending_logins.pop(user.user_id, None)

        task = asyncio.create_task(runner())
        self._pending_logins[user.user_id] = _PendingLogin(queue=queue, task=task)

    async def _cmd_logout(self, event, user: User) -> None:
        chat_id = int(event.chat_id)
        if not await self._is_logged_in(user, force=True):
            await self._send(
                chat_id, t("logout_already", user.lang),
                thread_id=user.system_thread_id,
            )
            return

        # Forget the per-user token; subsequent claude spawns won't authenticate.
        # Also kill any live sessions for this user so they don't keep using the
        # old token from memory.
        await self.state.set_user_token(user.user_id, None)
        for sess in list(self.registry.iter_user(user.user_id)):
            sess.oauth_token = None
            await self.registry.drop(sess.session_id)
        self._auth_cache[user.user_id] = (time.monotonic(), False)
        await self._send(
            chat_id, t("logout_done", user.lang),
            thread_id=user.system_thread_id,
        )

    # ---- auth gate --------------------------------------------------------

    async def _is_logged_in(self, user: User, *, force: bool = False) -> bool:
        now = time.monotonic()
        cached = self._auth_cache.get(user.user_id)
        if not force and cached is not None:
            ts, ok = cached
            if now - ts < _AUTH_CACHE_TTL:
                return ok
        status = await auth_status(
            self.settings.claude_bin,
            home=user_home_path(self.settings.data_dir, user.user_id),
            token=user.oauth_token,
        )
        ok = bool(status.get("loggedIn"))
        self._auth_cache[user.user_id] = (now, ok)
        return ok

    # ---- system thread ----------------------------------------------------

    async def _ensure_system_thread(self, user: User, chat_id: int) -> User:
        if user.system_thread_id is not None:
            return user
        try:
            topic = await self.bot_api.create_forum_topic(
                chat_id=chat_id, name=t("system_topic_name", user.lang),
                icon_color=0x6FB9F0,
            )
        except BotAPIError as e:
            log.warning("createForumTopic for system thread failed: %s", e)
            return user
        thread_id = int(topic["message_thread_id"])
        await self.state.set_system_thread(user.user_id, thread_id)
        user.system_thread_id = thread_id
        return user

    # ---- session routing --------------------------------------------------

    async def _route_to_session(self, event, user: User, text: str) -> None:
        chat_id = int(event.chat_id)
        thread_id = _extract_thread_id(event)

        # Messages in the system topic are not session-bound; nudge the user.
        if thread_id is not None and thread_id == user.system_thread_id:
            await self._send(
                chat_id, t("session_none", user.lang), thread_id=user.system_thread_id,
            )
            return

        session_row = None
        if thread_id is not None:
            session_row = await self.state.get_session_by_thread(thread_id)
        if session_row is None and user.active_session_id is not None:
            session_row = await self.state.get_session(user.active_session_id)

        if session_row is None:
            await self._send(
                chat_id, t("session_none", user.lang), thread_id=user.system_thread_id,
            )
            return

        sess = self.registry.get(session_row.session_id)
        if sess is None:
            sess = ClaudeSession(
                session_id=session_row.session_id,
                user_id=user.user_id,
                cwd=session_row.cwd,
                perm_mode=session_row.perm_mode,
                allowed_tools=self.settings.allowed_tools,
                claude_bin=self.settings.claude_bin,
                home=user_home_path(self.settings.data_dir, user.user_id),
                oauth_token=user.oauth_token,
            )
            sess._known = True  # noqa: SLF001  -- existing session_id; resume on first spawn
            await self.registry.put(sess)
        else:
            # Token may have rotated since spawn (re-login); refresh in-memory.
            sess.oauth_token = user.oauth_token

        await self.state.touch(session_row.session_id)
        target_thread = session_row.thread_id
        draft_id = (
            await self.state.next_draft_id(target_thread) if target_thread is not None else 1
        )

        await self._run_streamed_turn(
            session=sess,
            user_text=text,
            chat_id=chat_id,
            thread_id=target_thread,
            draft_id=draft_id,
            lang=user.lang,
        )

    async def _run_streamed_turn(
        self,
        *,
        session: ClaudeSession,
        user_text: str,
        chat_id: int,
        thread_id: int | None,
        draft_id: int,
        lang: str,
    ) -> None:
        accumulated: list[str] = []
        result_text: list[str] = [""]

        async def send_draft(text: str) -> None:
            if not text:
                return
            await self.bot_api.send_message_draft(
                chat_id=chat_id, draft_id=draft_id, text=text,
                message_thread_id=thread_id,
            )

        async def commit(text: str) -> None:
            for chunk in chunk_message(text):
                await self.bot_api.send_message(
                    chat_id=chat_id, text=chunk, message_thread_id=thread_id,
                )

        streamer = DraftStreamer(send_draft, commit)

        async def on_event(evt: SemanticEvent) -> None:
            if isinstance(evt, TurnStarted):
                return
            if isinstance(evt, TextDelta):
                accumulated.append(evt.text)
                streamer.append(evt.text)
                return
            if isinstance(evt, ToolCall):
                line = "\n" + t("tool_call", lang, tool=evt.tool, summary=tool_summary(evt.tool, evt.summary)) + "\n"
                accumulated.append(line)
                streamer.append(line)
                return
            if isinstance(evt, Thinking):
                line = "\n" + t("thinking", lang) + "\n"
                accumulated.append(line)
                streamer.append(line)
                return
            if isinstance(evt, TurnEnded):
                if evt.is_error:
                    err = t("err_generic", lang, detail=evt.error or "?")
                    await streamer.finalize(err)
                else:
                    result_text[0] = evt.final_text or "".join(accumulated)
                    await streamer.finalize(result_text[0])

        try:
            await session.run_turn(user_text, on_event)
        except Exception as e:
            log.exception("turn crashed")
            await streamer.finalize(t("err_generic", lang, detail=repr(e)))

    # ---- send helper ------------------------------------------------------

    async def _send(self, chat_id: int, text: str, *, thread_id: int | None = None) -> None:
        try:
            await self.bot_api.send_message(
                chat_id=chat_id, text=text, message_thread_id=thread_id,
            )
        except BotAPIError as e:
            log.warning("send failed: %s", e)


def _extract_thread_id(event) -> int | None:
    """Pull the forum-topic id from an incoming Telethon NewMessage event, if any."""
    rt = getattr(event.message, "reply_to", None)
    if rt is None:
        return None
    if getattr(rt, "forum_topic", False):
        return int(getattr(rt, "reply_to_top_id", None) or getattr(rt, "reply_to_msg_id", 0)) or None
    top = getattr(rt, "reply_to_top_id", None)
    return int(top) if top else None
