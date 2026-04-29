"""Thin aiohttp client for Bot-API-only methods (sendMessageDraft, createForumTopic).

Telethon talks MTProto and cannot call these. Everything else is sent via Telethon.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

_BASE = "https://api.telegram.org"


class BotAPIError(RuntimeError):
    def __init__(self, method: str, code: int, description: str) -> None:
        super().__init__(f"{method}: {code} {description}")
        self.method = method
        self.code = code
        self.description = description


class BotAPI:
    def __init__(self, token: str, *, timeout: float = 30.0) -> None:
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> BotAPI:
        self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _call(self, method: str, payload: dict[str, Any], *, attempts: int = 4) -> Any:
        if self._session is None:
            raise RuntimeError("BotAPI used outside async context")
        url = f"{_BASE}/bot{self._token}/{method}"
        for attempt in range(1, attempts + 1):
            async with self._session.post(url, json=payload) as resp:
                data = await resp.json()
            if data.get("ok"):
                return data["result"]
            code = int(data.get("error_code", resp.status))
            desc = str(data.get("description", ""))
            if code == 429 and attempt < attempts:
                retry_after = float(data.get("parameters", {}).get("retry_after", 1))
                log.warning("%s rate-limited, sleeping %.1fs", method, retry_after)
                await asyncio.sleep(retry_after)
                continue
            if 500 <= code < 600 and attempt < attempts:
                await asyncio.sleep(0.5 * attempt)
                continue
            raise BotAPIError(method, code, desc)
        raise BotAPIError(method, 0, "exhausted retries")

    async def send_message_draft(
        self,
        *,
        chat_id: int,
        draft_id: int,
        text: str,
        message_thread_id: int | None = None,
        parse_mode: str | None = None,
    ) -> bool:
        payload: dict[str, Any] = {"chat_id": chat_id, "draft_id": draft_id, "text": text}
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return bool(await self._call("sendMessageDraft", payload))

    async def create_forum_topic(
        self, *, chat_id: int, name: str, icon_color: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "name": name}
        if icon_color is not None:
            payload["icon_color"] = icon_color
        return await self._call("createForumTopic", payload)

    async def delete_forum_topic(
        self, *, chat_id: int, message_thread_id: int,
    ) -> bool:
        return bool(await self._call(
            "deleteForumTopic",
            {"chat_id": chat_id, "message_thread_id": message_thread_id},
        ))

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        message_thread_id: int | None = None,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Used as a Bot-API fallback when we want to commit inside a forum topic in a private chat;
        Telethon's MTProto path doesn't accept message_thread_id for private chats today."""
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self._call("sendMessage", payload)

    async def set_my_commands(
        self,
        *,
        commands: list[dict[str, str]],
        scope: dict[str, Any] | None = None,
        language_code: str | None = None,
    ) -> bool:
        payload: dict[str, Any] = {"commands": commands}
        if scope is not None:
            payload["scope"] = scope
        if language_code:
            payload["language_code"] = language_code
        return bool(await self._call("setMyCommands", payload))

    async def answer_callback_query(
        self, *, callback_query_id: str, text: str | None = None,
    ) -> bool:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return bool(await self._call("answerCallbackQuery", payload))

    async def send_chat_action(
        self,
        *,
        chat_id: int,
        action: str,
        message_thread_id: int | None = None,
    ) -> bool:
        payload: dict[str, Any] = {"chat_id": chat_id, "action": action}
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        return bool(await self._call("sendChatAction", payload))

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any] | bool:
        payload: dict[str, Any] = {
            "chat_id": chat_id, "message_id": message_id, "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self._call("editMessageText", payload)
