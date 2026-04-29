"""Persistent state in SQLite (WAL). Single connection, async via aiosqlite."""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  lang TEXT NOT NULL,
  cwd TEXT NOT NULL,
  perm_mode TEXT NOT NULL DEFAULT 'acceptEdits',
  active_session_id TEXT,
  system_thread_id INTEGER,
  oauth_token TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  thread_id INTEGER,
  cwd TEXT NOT NULL,
  perm_mode TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  last_used INTEGER NOT NULL,
  status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
  thread_id INTEGER PRIMARY KEY,
  next_draft_id INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_thread ON sessions(thread_id);
"""


@dataclass(slots=True)
class User:
    user_id: int
    lang: str
    cwd: str
    perm_mode: str
    active_session_id: str | None
    system_thread_id: int | None = None
    oauth_token: str | None = None


@dataclass(slots=True)
class Session:
    session_id: str
    user_id: int
    thread_id: int | None
    cwd: str
    perm_mode: str
    created_at: int
    last_used: int
    status: str  # 'active' | 'suspended' | 'dead'


def _now() -> int:
    return int(time.time())


class State:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    @classmethod
    async def open(cls, db_path: str) -> State:
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.executescript(_SCHEMA)
        # Idempotent migrations for DBs created before these columns existed.
        for ddl in (
            "ALTER TABLE users ADD COLUMN system_thread_id INTEGER",
            "ALTER TABLE users ADD COLUMN oauth_token TEXT",
        ):
            with contextlib.suppress(aiosqlite.OperationalError):
                await conn.execute(ddl)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    # ---- users -------------------------------------------------------------

    async def get_or_create_user(self, user_id: int, lang_hint: str, default_cwd: str) -> User:
        cur = await self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if row:
            return User(**row)
        await self.conn.execute(
            "INSERT INTO users(user_id, lang, cwd, perm_mode) VALUES (?, ?, ?, 'acceptEdits')",
            (user_id, lang_hint, default_cwd),
        )
        await self.conn.commit()
        return User(user_id, lang_hint, default_cwd, "acceptEdits", None)

    async def set_user_lang(self, user_id: int, lang: str) -> None:
        await self.conn.execute("UPDATE users SET lang=? WHERE user_id=?", (lang, user_id))
        await self.conn.commit()

    async def set_user_cwd(self, user_id: int, cwd: str) -> None:
        await self.conn.execute("UPDATE users SET cwd=? WHERE user_id=?", (cwd, user_id))
        await self.conn.commit()

    async def set_user_perm(self, user_id: int, perm_mode: str) -> None:
        await self.conn.execute("UPDATE users SET perm_mode=? WHERE user_id=?", (perm_mode, user_id))
        await self.conn.commit()

    async def set_active_session(self, user_id: int, session_id: str | None) -> None:
        await self.conn.execute(
            "UPDATE users SET active_session_id=? WHERE user_id=?", (session_id, user_id),
        )
        await self.conn.commit()

    async def set_system_thread(self, user_id: int, thread_id: int | None) -> None:
        await self.conn.execute(
            "UPDATE users SET system_thread_id=? WHERE user_id=?", (thread_id, user_id),
        )
        await self.conn.commit()

    async def set_user_token(self, user_id: int, token: str | None) -> None:
        await self.conn.execute(
            "UPDATE users SET oauth_token=? WHERE user_id=?", (token, user_id),
        )
        await self.conn.commit()

    # ---- sessions ----------------------------------------------------------

    async def new_session(
        self, session_id: str, user_id: int, thread_id: int | None, cwd: str, perm_mode: str,
    ) -> Session:
        now = _now()
        await self.conn.execute(
            "INSERT INTO sessions(session_id, user_id, thread_id, cwd, perm_mode, created_at, last_used, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'active')",
            (session_id, user_id, thread_id, cwd, perm_mode, now, now),
        )
        await self.conn.commit()
        return Session(session_id, user_id, thread_id, cwd, perm_mode, now, now, "active")

    async def get_session(self, session_id: str) -> Session | None:
        cur = await self.conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,))
        row = await cur.fetchone()
        return Session(**row) if row else None

    async def get_session_by_thread(self, thread_id: int) -> Session | None:
        cur = await self.conn.execute(
            "SELECT * FROM sessions WHERE thread_id=? AND status != 'dead' "
            "ORDER BY last_used DESC LIMIT 1", (thread_id,),
        )
        row = await cur.fetchone()
        return Session(**row) if row else None

    async def get_alive_sessions(self) -> list[Session]:
        """All sessions that haven't been explicitly closed via /exit. Used at
        startup to notify each topic that the bot has come back online."""
        cur = await self.conn.execute(
            "SELECT * FROM sessions WHERE status != 'dead' ORDER BY last_used DESC"
        )
        rows = await cur.fetchall()
        return [Session(**row) for row in rows]

    async def get_user(self, user_id: int) -> User | None:
        cur = await self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return User(**row) if row else None

    async def touch(self, session_id: str) -> None:
        await self.conn.execute(
            "UPDATE sessions SET last_used=?, status='active' WHERE session_id=?",
            (_now(), session_id),
        )
        await self.conn.commit()

    async def set_session_cwd(self, session_id: str, cwd: str) -> None:
        await self.conn.execute(
            "UPDATE sessions SET cwd=? WHERE session_id=?", (cwd, session_id),
        )
        await self.conn.commit()

    async def set_session_perm(self, session_id: str, perm_mode: str) -> None:
        await self.conn.execute(
            "UPDATE sessions SET perm_mode=? WHERE session_id=?", (perm_mode, session_id),
        )
        await self.conn.commit()

    async def set_session_status(self, session_id: str, status: str) -> None:
        await self.conn.execute(
            "UPDATE sessions SET status=? WHERE session_id=?", (status, session_id),
        )
        await self.conn.commit()

    # ---- drafts ------------------------------------------------------------

    async def next_draft_id(self, thread_id: int) -> int:
        await self.conn.execute(
            "INSERT INTO drafts(thread_id, next_draft_id) VALUES (?, 1) "
            "ON CONFLICT(thread_id) DO UPDATE SET next_draft_id = next_draft_id + 1",
            (thread_id,),
        )
        cur = await self.conn.execute(
            "SELECT next_draft_id FROM drafts WHERE thread_id=?", (thread_id,),
        )
        row = await cur.fetchone()
        await self.conn.commit()
        return int(row["next_draft_id"])
