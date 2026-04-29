"""Register the bot's slash-command list with Telegram, per-language, scoped to
private chats. Visible in the Telegram client's command menu (the ``/`` button).

Telegram rate-limits ``setMyCommands`` aggressively (retry_after values up to
600 s have been observed). To avoid hitting that on every restart triggered by
a file edit, we hash the rendered command payload (per language + scope) and
store the digest in ``bot_kv``. If the hash matches what's stored, we skip the
API calls entirely. Calls only fire when descriptions or commands actually
change."""

from __future__ import annotations

import hashlib
import json
import logging

from .bot_api import BotAPI
from .i18n import available_languages, t
from .state import State

log = logging.getLogger(__name__)

# (command, locale-key-for-description). Keep aligned with handlers.py routing.
COMMANDS: tuple[tuple[str, str], ...] = (
    ("new", "cmd_new_desc"),
    ("exit", "cmd_exit_desc"),
    ("cd", "cmd_cd_desc"),
    ("login", "cmd_login_desc"),
    ("logout", "cmd_logout_desc"),
    ("perm", "cmd_perm_desc"),
    ("lang", "cmd_lang_desc"),
    ("help", "cmd_help_desc"),
)

_PRIVATE_SCOPE = {"type": "all_private_chats"}
_FINGERPRINT_KEY = "commands_fingerprint_v1"


def _build_payloads() -> list[dict]:
    payloads: list[dict] = []
    for code in available_languages():
        commands = [
            {"command": cmd, "description": t(desc_key, code)}
            for cmd, desc_key in COMMANDS
        ]
        payloads.append({"lang": code, "scope": _PRIVATE_SCOPE, "commands": commands})
    fallback = [
        {"command": cmd, "description": t(desc_key, "en")}
        for cmd, desc_key in COMMANDS
    ]
    payloads.append({"lang": None, "scope": _PRIVATE_SCOPE, "commands": fallback})
    return payloads


def _fingerprint(payloads: list[dict]) -> str:
    blob = json.dumps(payloads, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


async def register_commands(bot_api: BotAPI, state: State) -> None:
    payloads = _build_payloads()
    fingerprint = _fingerprint(payloads)

    stored = await state.get_kv(_FINGERPRINT_KEY)
    if stored == fingerprint:
        log.info("bot commands unchanged (%s); skipping setMyCommands", fingerprint[:8])
        return

    log.info("bot commands changed; pushing to Telegram")
    all_ok = True
    for p in payloads:
        try:
            await bot_api.set_my_commands(
                commands=p["commands"], scope=p["scope"], language_code=p["lang"],
            )
        except Exception as e:
            log.warning("setMyCommands(%s) failed: %s", p["lang"] or "default", e)
            all_ok = False

    if all_ok:
        # Only persist the fingerprint when EVERY language actually went through.
        # If one was rate-limited, we leave the stored fingerprint stale so the
        # next restart retries.
        await state.set_kv(_FINGERPRINT_KEY, fingerprint)
        log.info("bot commands updated")
