"""Environment-driven settings. A .env file in the working directory is loaded
into os.environ on import (without overwriting existing variables) so dev runs
work the same as systemd EnvironmentFile= deployments."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_dotenv(Path.cwd() / ".env")


def _req(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def _ids(name: str) -> frozenset[int]:
    raw = os.environ.get(name, "")
    return frozenset(int(p) for p in raw.replace(",", " ").split() if p)


@dataclass(frozen=True, slots=True)
class Settings:
    api_id: int
    api_hash: str
    bot_token: str
    allowed_user_ids: frozenset[int]
    claude_bin: str
    default_cwd: str
    idle_suspend_sec: int
    allowed_tools: tuple[str, ...]
    db_path: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            api_id=int(_req("API_ID")),
            api_hash=_req("API_HASH"),
            bot_token=_req("BOT_TOKEN"),
            allowed_user_ids=_ids("ALLOWED_USER_IDS"),
            claude_bin=os.environ.get("CLAUDE_BIN", "/root/.local/bin/claude"),
            default_cwd=os.environ.get("DEFAULT_CWD", os.getcwd()),
            idle_suspend_sec=int(os.environ.get("IDLE_SUSPEND_SEC", "600")),
            allowed_tools=tuple(os.environ.get("ALLOWED_TOOLS", "Bash Read Write Edit Glob Grep").split()),
            db_path=os.environ.get("DB_PATH", "state.db"),
        )
