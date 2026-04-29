"""Register the bot's slash-command list with Telegram, per-language, scoped to
private chats. Visible in the Telegram client's command menu (the ``/`` button)."""

from __future__ import annotations

import logging

from .bot_api import BotAPI
from .i18n import available_languages, t

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


async def register_commands(bot_api: BotAPI) -> None:
    for code in available_languages():
        commands = [
            {"command": cmd, "description": t(desc_key, code)}
            for cmd, desc_key in COMMANDS
        ]
        try:
            await bot_api.set_my_commands(
                commands=commands, scope=_PRIVATE_SCOPE, language_code=code,
            )
        except Exception as e:
            log.warning("setMyCommands(%s) failed: %s", code, e)
    # Default fallback (no language_code) — used for clients with a locale
    # we don't have a translation for.
    fallback = [
        {"command": cmd, "description": t(desc_key, "en")}
        for cmd, desc_key in COMMANDS
    ]
    try:
        await bot_api.set_my_commands(commands=fallback, scope=_PRIVATE_SCOPE)
    except Exception as e:
        log.warning("setMyCommands(default) failed: %s", e)
