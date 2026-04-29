"""Telegram event routing. /start /help /new /cd /login /perm /lang and plain text → session."""

from __future__ import annotations

import asyncio
import logging
import os
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
from .permissions import normalize_perm_mode
from .registry import SessionRegistry
from .render import chunk_message, tool_summary
from .state import State
from .streaming import DraftStreamer

log = logging.getLogger(__name__)


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

    # ---- registration -----------------------------------------------------

    def register(self) -> None:
        self.client.add_event_handler(self._on_message, events.NewMessage(incoming=True))

    # ---- main dispatch ----------------------------------------------------

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        sender = await event.get_sender()
        if sender is None or sender.bot:
            return
        user_id = int(sender.id)
        if user_id not in self.settings.allowed_user_ids:
            await self._reply(event, "not_allowed", "en")
            return

        user = await self.state.get_or_create_user(
            user_id, pick_lang(getattr(sender, "lang_code", None)), self.settings.default_cwd,
        )
        text = (event.raw_text or "").strip()

        if text.startswith("/"):
            await self._handle_command(event, user, text)
            return

        # Pending login takes precedence over routing to a session.
        pending = self._pending_logins.get(user_id)
        if pending is not None:
            await pending.queue.put(text)
            return

        await self._route_to_session(event, user, text)

    # ---- commands ---------------------------------------------------------

    async def _handle_command(self, event: events.NewMessage.Event, user, text: str) -> None:
        cmd, _, rest = text.partition(" ")
        cmd = cmd.split("@", 1)[0].lower()
        rest = rest.strip()

        if cmd == "/start" or cmd == "/help":
            await self._send(event.chat_id, t("welcome" if cmd == "/start" else "help", user.lang))
        elif cmd == "/new":
            await self._cmd_new(event, user)
        elif cmd == "/cd":
            await self._cmd_cd(event, user, rest)
        elif cmd == "/login":
            await self._cmd_login(event, user)
        elif cmd == "/perm":
            await self._cmd_perm(event, user, rest)
        elif cmd == "/lang":
            await self._cmd_lang(event, user, rest)
        else:
            await self._send(event.chat_id, t("help", user.lang))

    async def _cmd_cd(self, event, user, rest: str) -> None:
        if not rest:
            await self._send(event.chat_id, t("cd_missing", user.lang))
            return
        path = os.path.realpath(rest)
        if not os.path.isdir(path) or not os.access(path, os.R_OK):
            await self._send(event.chat_id, t("cd_bad", user.lang, path=path))
            return
        await self.state.set_user_cwd(user.user_id, path)
        await self._send(event.chat_id, t("cd_ok", user.lang, path=path))

    async def _cmd_perm(self, event, user, rest: str) -> None:
        mode = normalize_perm_mode(rest)
        if mode is None:
            await self._send(event.chat_id, t("perm_bad", user.lang))
            return
        await self.state.set_user_perm(user.user_id, mode)
        await self._send(event.chat_id, t("perm_set", user.lang, mode=mode))

    async def _cmd_lang(self, event, user, rest: str) -> None:
        langs = available_languages()
        code = rest.lower().split("-", 1)[0]
        if code not in langs:
            await self._send(
                event.chat_id,
                t("lang_bad", user.lang, available=", ".join(sorted(langs))),
            )
            return
        await self.state.set_user_lang(user.user_id, code)
        await self._send(event.chat_id, t("lang_set", code, name=langs[code]))

    async def _cmd_new(self, event, user) -> None:
        # Refresh user (cwd may have changed).
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
        ))
        await self._send(
            chat_id, t("session_new", user.lang, cwd=sess.cwd), thread_id=thread_id,
        )

    async def _cmd_login(self, event, user) -> None:
        chat_id = int(event.chat_id)
        if user.user_id in self._pending_logins:
            await self._send(chat_id, t("login_already", user.lang))
            return

        await self._send(chat_id, t("login_start", user.lang))
        queue: asyncio.Queue[str] = asyncio.Queue()

        async def send_url(url: str) -> None:
            await self._send(chat_id, t("login_url", user.lang, url=url))

        async def await_code(timeout: float) -> str:
            return await asyncio.wait_for(queue.get(), timeout=timeout)

        async def notify_invalid(left: int) -> None:
            await self._send(chat_id, t("login_bad", user.lang, left=left))

        async def runner() -> None:
            try:
                await run_login(
                    claude_bin=self.settings.claude_bin,
                    send_url=send_url,
                    await_code=await_code,
                    notify_invalid=notify_invalid,
                )
                await self._send(chat_id, t("login_done", user.lang))
            except LoginError as e:
                await self._send(chat_id, t("login_failed", user.lang, detail=str(e)))
            except Exception as e:
                log.exception("login flow crashed")
                await self._send(chat_id, t("login_failed", user.lang, detail=repr(e)))
            finally:
                self._pending_logins.pop(user.user_id, None)

        task = asyncio.create_task(runner())
        self._pending_logins[user.user_id] = _PendingLogin(queue=queue, task=task)

    # ---- session routing --------------------------------------------------

    async def _route_to_session(self, event, user, text: str) -> None:
        chat_id = int(event.chat_id)
        thread_id = _extract_thread_id(event)

        session_row = None
        if thread_id is not None:
            session_row = await self.state.get_session_by_thread(thread_id)
        if session_row is None and user.active_session_id is not None:
            session_row = await self.state.get_session(user.active_session_id)

        if session_row is None:
            await self._send(chat_id, t("session_none", user.lang), thread_id=thread_id)
            return

        # Confirm auth before spawning.
        status = await auth_status(self.settings.claude_bin)
        if not status.get("loggedIn"):
            await self._send(chat_id, t("auth_required", user.lang), thread_id=thread_id)
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
            )
            # Existing session_id from DB → resume on first spawn.
            sess._known = True  # noqa: SLF001
            await self.registry.put(sess)

        await self.state.touch(session_row.session_id)
        draft_id = await self.state.next_draft_id(thread_id) if thread_id is not None else 1

        await self._run_streamed_turn(
            session=sess,
            user_text=text,
            chat_id=chat_id,
            thread_id=session_row.thread_id,
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

    # ---- send helpers -----------------------------------------------------

    async def _send(self, chat_id: int, text: str, *, thread_id: int | None = None) -> None:
        try:
            await self.bot_api.send_message(
                chat_id=chat_id, text=text, message_thread_id=thread_id,
            )
        except BotAPIError as e:
            log.warning("send failed: %s", e)

    async def _reply(self, event, key: str, lang: str, **fmt: object) -> None:
        await self._send(int(event.chat_id), t(key, lang, **fmt))


def _extract_thread_id(event) -> int | None:
    """Pull the forum-topic id from an incoming Telethon NewMessage event, if any."""
    rt = getattr(event.message, "reply_to", None)
    if rt is None:
        return None
    if getattr(rt, "forum_topic", False):
        # Topic posts: top_id is the topic-creation message id (== Bot-API message_thread_id).
        return int(getattr(rt, "reply_to_top_id", None) or getattr(rt, "reply_to_msg_id", 0)) or None
    top = getattr(rt, "reply_to_top_id", None)
    return int(top) if top else None
