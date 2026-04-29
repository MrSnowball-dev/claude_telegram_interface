"""Telegram event routing. /start /help /new /cd /login /perm /lang and plain text → session."""

from __future__ import annotations

import asyncio
import contextlib
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
from .markdown import md_to_html
from .paths import user_home as user_home_path
from .permissions import normalize_perm_mode
from .registry import SessionRegistry
from .render import chunk_message, tool_summary
from .state import State, User
from .streaming import DraftStreamer

log = logging.getLogger(__name__)

_AUTH_CACHE_TTL = 30.0  # seconds


def _token_fp(token: str | None) -> str:
    """Last-4-char fingerprint of the OAuth token. ``setup-token`` doesn't
    grant ``user:profile`` scope so we can't fetch the email. The fingerprint
    matches the tail of what platform.claude.com showed the user."""
    if not token:
        return "?"
    return token[-4:]


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
        self.client.add_event_handler(self._on_callback, events.CallbackQuery)

    async def on_startup(self) -> None:
        """After (re)connecting to Telegram: notify each non-dead session topic
        that the bot is back, and re-issue any turn that was streaming when
        the previous process exited (its row carries a non-NULL pending_text).
        """
        sessions = await self.state.get_alive_sessions()
        for sess_row in sessions:
            if sess_row.thread_id is None:
                # Orphan: no topic to surface anything in. If it had a pending
                # turn, drop it — we have nowhere to stream the recovery.
                if sess_row.pending_text is not None:
                    await self.state.clear_pending_turn(sess_row.session_id)
                continue
            user = await self.state.get_user(sess_row.user_id)
            if user is None:
                continue
            if sess_row.pending_text:
                # Fire-and-forget recovery so other sessions don't block on this
                # one's network latency.
                asyncio.create_task(self._recover_turn(sess_row, user))
            else:
                await self._send_restart_notice(sess_row, user)

    async def _send_restart_notice(self, sess_row, user: User) -> None:
        try:
            await self.bot_api.send_message(
                chat_id=sess_row.user_id,
                text=md_to_html(t("service_restarted", user.lang)),
                message_thread_id=sess_row.thread_id,
                parse_mode="HTML",
            )
        except BotAPIError as e:
            log.info("restart notify skipped (%s/%s): %s",
                     sess_row.user_id, sess_row.thread_id, e)

    async def _recover_turn(self, sess_row, user: User) -> None:
        """Re-issue an interrupted turn. The user_text is in sess_row.pending_text;
        the session_id resumes via --resume so Claude has full conversation context."""
        try:
            try:
                await self.bot_api.send_message(
                    chat_id=sess_row.user_id,
                    text=md_to_html(t("recovering_turn", user.lang)),
                    message_thread_id=sess_row.thread_id,
                    parse_mode="HTML",
                )
            except BotAPIError as e:
                log.info("recovery banner skipped: %s", e)

            sess = self.registry.get(sess_row.session_id)
            if sess is None:
                sess = ClaudeSession(
                    session_id=sess_row.session_id,
                    user_id=sess_row.user_id,
                    cwd=sess_row.cwd,
                    perm_mode=sess_row.perm_mode,
                    allowed_tools=self.settings.allowed_tools,
                    claude_bin=self.settings.claude_bin,
                    home=user_home_path(self.settings.data_dir, sess_row.user_id),
                    oauth_token=user.oauth_token,
                )
                sess._known = True  # noqa: SLF001
                await self.registry.put(sess)

            target_thread = sess_row.thread_id
            draft_id = (
                await self.state.next_draft_id(target_thread)
                if target_thread is not None else 1
            )
            await self._run_streamed_turn(
                session=sess,
                user_text=sess_row.pending_text,
                chat_id=sess_row.user_id,
                thread_id=target_thread,
                draft_id=draft_id,
                lang=user.lang,
            )
        except Exception:
            log.exception("recovery turn crashed for session %s", sess_row.session_id)
        finally:
            await self.state.clear_pending_turn(sess_row.session_id)

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
            # Wipe the secret from chat history so it doesn't linger.
            with contextlib.suppress(Exception):
                await event.delete()
            return

        if not await self._is_logged_in(user):
            await self._send(
                chat_id, t("auth_required", user.lang),
                thread_id=_reply_thread(event, user),
            )
            return

        await self._route_to_session(event, user, text)

    # ---- callback queries -------------------------------------------------

    async def _on_callback(self, event: events.CallbackQuery.Event) -> None:
        sender = await event.get_sender()
        if sender is None or sender.bot:
            with contextlib.suppress(Exception):
                await event.answer()
            return
        user_id = int(sender.id)
        if user_id not in self.settings.allowed_user_ids:
            with contextlib.suppress(Exception):
                await event.answer()
            return

        user = await self.state.get_or_create_user(
            user_id, pick_lang(getattr(sender, "lang_code", None)), self.settings.default_cwd,
        )
        data = event.data.decode() if isinstance(event.data, bytes) else str(event.data)
        chat_id = int(event.chat_id)
        message_id = int(event.message_id)
        empty_markup = {"inline_keyboard": []}

        try:
            if data.startswith("lang:"):
                code = data[5:]
                langs = available_languages()
                if code in langs:
                    await self.state.set_user_lang(user_id, code)
                    with contextlib.suppress(BotAPIError):
                        await self.bot_api.edit_message_text(
                            chat_id=chat_id, message_id=message_id,
                            text=t("lang_set", code, name=langs[code]),
                            reply_markup=empty_markup,
                        )
            elif data.startswith("perm:"):
                mode = data[5:]
                if any(mode == m for m, _ in self._PERM_MENU):
                    # Detect whether the menu lives in a session topic; if so,
                    # apply to that session only. Else apply to user default.
                    msg = None
                    with contextlib.suppress(Exception):
                        msg = await event.get_message()
                    msg_thread = _thread_id_of_msg(msg)
                    session_row = await self._session_for_thread(msg_thread, user)
                    if session_row is not None:
                        await self.state.set_session_perm(session_row.session_id, mode)
                        await self.registry.drop(session_row.session_id)
                        confirmation = t("perm_session_set", user.lang, mode=mode)
                    else:
                        await self.state.set_user_perm(user_id, mode)
                        confirmation = t("perm_set", user.lang, mode=mode)
                    with contextlib.suppress(BotAPIError):
                        await self.bot_api.edit_message_text(
                            chat_id=chat_id, message_id=message_id,
                            text=confirmation, reply_markup=empty_markup,
                        )
        finally:
            with contextlib.suppress(Exception):
                await event.answer()

    # ---- commands ---------------------------------------------------------

    async def _handle_command(self, event: events.NewMessage.Event, user: User, text: str) -> None:
        cmd, _, rest = text.partition(" ")
        cmd = cmd.split("@", 1)[0].lower()
        rest = rest.strip()

        # Open commands — no auth required.
        reply_thread = _reply_thread(event, user)
        if cmd in ("/start", "/help"):
            await self._send(
                int(event.chat_id),
                t("welcome" if cmd == "/start" else "help", user.lang),
                thread_id=reply_thread,
            )
            if cmd == "/start" and not await self._is_logged_in(user):
                await self._send(
                    int(event.chat_id),
                    t("auth_required", user.lang),
                    thread_id=reply_thread,
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
        if cmd == "/exit":
            await self._cmd_exit(event, user)
            return

        # Gated commands.
        if not await self._is_logged_in(user):
            await self._send(
                int(event.chat_id), t("auth_required", user.lang),
                thread_id=reply_thread,
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
                int(event.chat_id), t("help", user.lang), thread_id=reply_thread,
            )

    async def _session_for_thread(self, msg_thread: int | None, user: User):
        """Return the Session row bound to the topic this command came from, or
        None if the command isn't inside a session topic."""
        if msg_thread is None or msg_thread == user.system_thread_id:
            return None
        return await self.state.get_session_by_thread(msg_thread)

    async def _cmd_cd(self, event, user: User, rest: str) -> None:
        reply_thread = _reply_thread(event, user)
        session_row = await self._session_for_thread(_extract_thread_id(event), user)

        if not rest:
            await self._send(
                int(event.chat_id), t("cd_missing", user.lang), thread_id=reply_thread,
            )
            return
        path = os.path.realpath(rest)
        if not os.path.isdir(path) or not os.access(path, os.R_OK):
            await self._send(
                int(event.chat_id),
                t("cd_bad", user.lang, path=path), thread_id=reply_thread,
            )
            return

        if session_row is not None:
            # Topic-scoped: update this session's cwd and suspend the subprocess
            # so the next message respawns it with the new cwd/--add-dir.
            # We keep the session in the registry (instead of drop()) so that
            # its _known flag is preserved — drop() would cause the replacement
            # object to blindly set _known=True, making --resume fail for
            # sessions that were never actually started.
            await self.state.set_session_cwd(session_row.session_id, path)
            sess = self.registry.get(session_row.session_id)
            if sess is not None:
                sess.cwd = path
                await sess.suspend()
            await self._send(
                int(event.chat_id),
                t("cd_session", user.lang, path=path), thread_id=reply_thread,
            )
            return

        # Outside any session topic: update the user default for new sessions.
        await self.state.set_user_cwd(user.user_id, path)
        await self._send(
            int(event.chat_id),
            t("cd_ok", user.lang, path=path), thread_id=reply_thread,
        )

    _PERM_MENU: tuple[tuple[str, str], ...] = (
        ("acceptEdits", "perm_label_acceptedits"),
        ("plan", "perm_label_plan"),
        ("bypassPermissions", "perm_label_bypass"),
        ("default", "perm_label_default"),
    )

    async def _cmd_perm(self, event, user: User, rest: str) -> None:
        reply_thread = _reply_thread(event, user)
        session_row = await self._session_for_thread(_extract_thread_id(event), user)

        if rest:
            mode = normalize_perm_mode(rest)
            if mode is None:
                await self._send(
                    int(event.chat_id), t("perm_bad", user.lang),
                    thread_id=reply_thread,
                )
                return
            if session_row is not None:
                await self.state.set_session_perm(session_row.session_id, mode)
                await self.registry.drop(session_row.session_id)
                await self._send(
                    int(event.chat_id),
                    t("perm_session_set", user.lang, mode=mode),
                    thread_id=reply_thread,
                )
            else:
                await self.state.set_user_perm(user.user_id, mode)
                await self._send(
                    int(event.chat_id),
                    t("perm_set", user.lang, mode=mode),
                    thread_id=reply_thread,
                )
            return

        keyboard = {"inline_keyboard": [
            [{"text": t(label_key, user.lang), "callback_data": f"perm:{mode}"}]
            for mode, label_key in self._PERM_MENU
        ]}
        await self._send(
            int(event.chat_id), t("perm_pick", user.lang),
            thread_id=reply_thread, reply_markup=keyboard,
        )

    async def _cmd_lang(self, event, user: User, rest: str) -> None:
        reply_thread = _reply_thread(event, user)
        langs = available_languages()
        if rest:
            code = rest.lower().split("-", 1)[0]
            if code not in langs:
                await self._send(
                    int(event.chat_id),
                    t("lang_bad", user.lang, available=", ".join(sorted(langs))),
                    thread_id=reply_thread,
                )
                return
            await self.state.set_user_lang(user.user_id, code)
            await self._send(
                int(event.chat_id),
                t("lang_set", code, name=langs[code]), thread_id=reply_thread,
            )
            return

        keyboard = {"inline_keyboard": [
            [{"text": name, "callback_data": f"lang:{code}"}]
            for code, name in sorted(langs.items())
        ]}
        await self._send(
            int(event.chat_id), t("lang_pick", user.lang),
            thread_id=reply_thread, reply_markup=keyboard,
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
        reply_thread = _reply_thread(event, user)
        if user.user_id in self._pending_logins:
            await self._send(
                chat_id, t("login_already", user.lang), thread_id=reply_thread,
            )
            return

        if await self._is_logged_in(user, force=True):
            await self._send(
                chat_id,
                t("login_already_authed", user.lang, fp=_token_fp(user.oauth_token)),
                thread_id=reply_thread,
            )
            return

        # One status message that the whole flow edits in place.
        initial = await self._send(
            chat_id, t("login_start", user.lang), thread_id=reply_thread,
        )
        msg_id = int(initial["message_id"]) if initial else None

        queue: asyncio.Queue[str] = asyncio.Queue()
        home = user_home_path(self.settings.data_dir, user.user_id)

        async def update(text: str, *, reply_markup: dict | None = None) -> None:
            if msg_id is not None:
                await self._edit(chat_id, msg_id, text, reply_markup=reply_markup)
            else:
                await self._send(
                    chat_id, text, thread_id=reply_thread, reply_markup=reply_markup,
                )

        async def send_url(url: str) -> None:
            keyboard = {"inline_keyboard": [[
                {"text": t("login_url_button", user.lang), "url": url},
            ]]}
            await update(t("login_url_text", user.lang), reply_markup=keyboard)

        async def await_code(timeout: float) -> str:
            return await asyncio.wait_for(queue.get(), timeout=timeout)

        async def on_code_received() -> None:
            await update(t("login_checking", user.lang))

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
                for sess in list(self.registry.iter_user(user.user_id)):
                    sess.oauth_token = token
                await update(t("login_done", user.lang, fp=_token_fp(token)))
            except LoginError as e:
                log.warning("login failed: %s", e)
                await update(t("login_failed", user.lang, detail=str(e)))
            except Exception as e:
                log.exception("login flow crashed")
                await update(t("login_failed", user.lang, detail=repr(e)))
            finally:
                self._pending_logins.pop(user.user_id, None)

        task = asyncio.create_task(runner())
        self._pending_logins[user.user_id] = _PendingLogin(queue=queue, task=task)

    async def _cmd_exit(self, event, user: User) -> None:
        chat_id = int(event.chat_id)
        reply_thread = _reply_thread(event, user)
        thread_id = _extract_thread_id(event)
        if thread_id is None:
            await self._send(
                chat_id, t("exit_no_topic", user.lang), thread_id=reply_thread,
            )
            return
        if user.system_thread_id is not None and thread_id == user.system_thread_id:
            await self._send(
                chat_id, t("exit_cant_delete_system", user.lang),
                thread_id=reply_thread,
            )
            return

        # Kill any live subprocess + mark session dead. Done before the API call
        # so even if delete races / fails we still tear down our own state.
        session_row = await self.state.get_session_by_thread(thread_id)
        if session_row is not None:
            await self.registry.drop(session_row.session_id)
            await self.state.set_session_status(session_row.session_id, "dead")
            if user.active_session_id == session_row.session_id:
                await self.state.set_active_session(user.user_id, None)

        try:
            await self.bot_api.delete_forum_topic(
                chat_id=chat_id, message_thread_id=thread_id,
            )
        except BotAPIError as e:
            # Topic survives; can't reply IN it (was supposed to be deleted) so
            # surface the error in the system topic.
            log.warning("deleteForumTopic failed: %s", e)
            await self._send(
                chat_id, t("exit_failed", user.lang, detail=str(e)),
                thread_id=user.system_thread_id,
            )

    async def _cmd_logout(self, event, user: User) -> None:
        chat_id = int(event.chat_id)
        reply_thread = _reply_thread(event, user)
        if not await self._is_logged_in(user, force=True):
            await self._send(
                chat_id, t("logout_already", user.lang), thread_id=reply_thread,
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
            chat_id, t("logout_done", user.lang), thread_id=reply_thread,
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
        reply_thread = thread_id if thread_id is not None else user.system_thread_id

        # Messages in the system topic are not session-bound; nudge the user.
        if thread_id is not None and thread_id == user.system_thread_id:
            await self._send(
                chat_id, t("session_none", user.lang), thread_id=reply_thread,
            )
            return

        session_row = None
        if thread_id is not None:
            session_row = await self.state.get_session_by_thread(thread_id)
        if session_row is None and user.active_session_id is not None:
            session_row = await self.state.get_session(user.active_session_id)

        if session_row is None:
            await self._send(
                chat_id, t("session_none", user.lang), thread_id=reply_thread,
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

        # Persist the in-flight turn so a restart mid-stream can recover it.
        await self.state.set_pending_turn(session_row.session_id, text)
        try:
            await self._run_streamed_turn(
                session=sess,
                user_text=text,
                chat_id=chat_id,
                thread_id=target_thread,
                draft_id=draft_id,
                lang=user.lang,
            )
        finally:
            await self.state.clear_pending_turn(session_row.session_id)

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
            try:
                await self.bot_api.send_message_draft(
                    chat_id=chat_id, draft_id=draft_id,
                    text=md_to_html(text),
                    message_thread_id=thread_id,
                    parse_mode="HTML",
                )
            except BotAPIError as e:
                # Fall back to plain text if HTML rendering trips Telegram's parser
                # (rare; usually a streaming chunk that ends mid-formatting).
                if "parse" in e.description.lower() or "tag" in e.description.lower():
                    await self.bot_api.send_message_draft(
                        chat_id=chat_id, draft_id=draft_id, text=text,
                        message_thread_id=thread_id,
                    )
                else:
                    raise

        async def commit(text: str) -> None:
            for chunk in chunk_message(text):
                try:
                    await self.bot_api.send_message(
                        chat_id=chat_id, text=md_to_html(chunk),
                        message_thread_id=thread_id, parse_mode="HTML",
                    )
                except BotAPIError as e:
                    if "parse" in e.description.lower() or "tag" in e.description.lower():
                        await self.bot_api.send_message(
                            chat_id=chat_id, text=chunk, message_thread_id=thread_id,
                        )
                    else:
                        raise

        streamer = DraftStreamer(send_draft, commit)

        async def typing_pinger() -> None:
            # Run for the entire turn so thinking gaps and tool-execution pauses
            # all show typing. Telegram clears the indicator ~5 s after each
            # sendChatAction; refresh every 4 s to keep it visible.
            try:
                while True:
                    with contextlib.suppress(BotAPIError):
                        await self.bot_api.send_chat_action(
                            chat_id=chat_id, action="typing",
                            message_thread_id=thread_id,
                        )
                    await asyncio.sleep(4.0)
            except asyncio.CancelledError:
                pass

        ping_task = asyncio.create_task(typing_pinger())

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
                # No inline marker — typing pinger covers the gap. Adding a
                # "[thinking…]" line here would just clutter the final message.
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
        finally:
            ping_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await ping_task

    # ---- send helpers -----------------------------------------------------

    async def _send(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        reply_markup: dict | None = None,
    ) -> dict | None:
        try:
            return await self.bot_api.send_message(
                chat_id=chat_id, text=text, message_thread_id=thread_id,
                reply_markup=reply_markup,
            )
        except BotAPIError as e:
            log.warning("send failed: %s", e)
            return None

    async def _edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
    ) -> None:
        try:
            await self.bot_api.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text,
                reply_markup=reply_markup,
            )
        except BotAPIError as e:
            # 400 "message is not modified" means the new text matches the old
            # exactly — harmless; ignore.
            if "not modified" not in e.description.lower():
                log.warning("edit failed: %s", e)


def _thread_id_of_msg(msg) -> int | None:
    """Pull the forum-topic id from a Telethon Message object, if any."""
    if msg is None:
        return None
    rt = getattr(msg, "reply_to", None)
    if rt is None:
        return None
    if getattr(rt, "forum_topic", False):
        return int(getattr(rt, "reply_to_top_id", None) or getattr(rt, "reply_to_msg_id", 0)) or None
    top = getattr(rt, "reply_to_top_id", None)
    return int(top) if top else None


def _extract_thread_id(event) -> int | None:
    return _thread_id_of_msg(event.message)


def _reply_thread(event, user: User) -> int | None:
    """Where to put a reply. Default rule: in the topic the command was issued
    in. If the command came from outside any topic (the chat's General area),
    fall back to the user's system topic so admin chatter stays organized."""
    return _extract_thread_id(event) or user.system_thread_id
