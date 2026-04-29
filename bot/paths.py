"""Per-user filesystem layout. Each Telegram user gets an isolated HOME so
their ``claude`` subprocesses read/write credentials in their own dir."""

from __future__ import annotations

import os


def user_home(data_dir: str, user_id: int) -> str:
    """Return ``<data_dir>/users/<user_id>/home``, creating it if missing."""
    path = os.path.join(data_dir, "users", str(user_id), "home")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path
