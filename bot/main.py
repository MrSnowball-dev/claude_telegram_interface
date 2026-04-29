"""Entrypoint. Loads config, opens state, builds Telethon client, registers handlers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from pathlib import Path

from telethon import TelegramClient

from .bot_api import BotAPI, BotAPIError
from .commands import register_commands
from .config import Settings
from .handlers import Handlers
from .i18n import load_locales, t
from .markdown import md_to_html
from .registry import SessionRegistry
from .state import State, User

log = logging.getLogger(__name__)


async def amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = Settings.from_env()
    load_locales(Path(__file__).parent / "locales")

    # Ensure the per-user data root exists; per-user homes are lazily created.
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True, mode=0o700)

    state = await State.open(settings.db_path)
    registry = SessionRegistry(idle_suspend_sec=settings.idle_suspend_sec)
    registry.start()

    client = TelegramClient(
        "tg_interface", settings.api_id, settings.api_hash,
        sequential_updates=False,
    )
    await client.start(bot_token=settings.bot_token)
    log.info("connected as bot, allow-list size=%d", len(settings.allowed_user_ids))

    async with BotAPI(settings.bot_token) as bot_api:
        await register_commands(bot_api)
        handlers = Handlers(
            client=client, settings=settings, state=state,
            registry=registry, bot_api=bot_api,
        )
        handlers.register()
        await _notify_alive_sessions(state, bot_api)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        log.info("ready")
        run_task = asyncio.create_task(client.run_until_disconnected())
        stop_task = asyncio.create_task(stop.wait())
        done, pending = await asyncio.wait(
            {run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        log.info("shutting down")
        await registry.stop()
        await client.disconnect()
        await state.close()


async def _notify_alive_sessions(state: State, bot_api: BotAPI) -> None:
    """Post a brief 'I'm back' message into every non-dead session topic so the
    user knows the bot finished restarting and they can keep talking."""
    sessions = await state.get_alive_sessions()
    if not sessions:
        return
    user_lang: dict[int, str] = {}

    async def lang_for(uid: int) -> str:
        cached = user_lang.get(uid)
        if cached is not None:
            return cached
        u: User | None = await state.get_user(uid)
        code = u.lang if u else "en"
        user_lang[uid] = code
        return code

    for sess in sessions:
        if sess.thread_id is None:
            continue
        code = await lang_for(sess.user_id)
        try:
            await bot_api.send_message(
                chat_id=sess.user_id,
                text=md_to_html(t("service_restarted", code)),
                message_thread_id=sess.thread_id,
                parse_mode="HTML",
            )
        except BotAPIError as e:
            log.info("restart notify skipped (%s/%s): %s",
                     sess.user_id, sess.thread_id, e)
    log.info("notified %d alive session topic(s)", len(sessions))


def run() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    run()
