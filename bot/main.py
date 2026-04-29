"""Entrypoint. Loads config, opens state, builds Telethon client, registers handlers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from pathlib import Path

from telethon import TelegramClient

from .bot_api import BotAPI
from .commands import register_commands
from .config import Settings
from .handlers import Handlers
from .i18n import load_locales
from .registry import SessionRegistry
from .state import State

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
        await register_commands(bot_api, state)
        handlers = Handlers(
            client=client, settings=settings, state=state,
            registry=registry, bot_api=bot_api,
        )
        handlers.register()
        await handlers.on_startup()

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


def run() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    run()
