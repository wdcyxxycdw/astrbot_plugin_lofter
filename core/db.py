import asyncio
import sqlite3
from typing import Optional


_DDL = """
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    type       TEXT NOT NULL CHECK(type IN ('tag','blog')),
    target     TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE(session_id, type, target)
);

CREATE TABLE IF NOT EXISTS seen_posts (
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    post_id         TEXT    NOT NULL,
    seen_at         INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (subscription_id, post_id)
);

CREATE INDEX IF NOT EXISTS idx_seen_posts_sub ON seen_posts(subscription_id);

CREATE TABLE IF NOT EXISTS sent_posts (
    session_id TEXT    NOT NULL,
    post_id    TEXT    NOT NULL,
    sent_at    INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (session_id, post_id)
);
"""


class LofterDB:
    def __init__(self, db_path: str):
        self._path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _run(self, fn):
        loop = asyncio.get_event_loop()
        return loop.run_in_executor(None, fn)

    async def initialize(self):
        def _init():
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_DDL)
            conn.commit()
            return conn

        self._conn = await self._run(_init)

    async def close(self):
        if self._conn:
            conn = self._conn
            await self._run(conn.close)
            self._conn = None

    async def get_config(self, key: str) -> Optional[str]:
        conn = self._conn

        def _get():
            row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

        return await self._run(_get)

    async def set_config(self, key: str, value: str):
        conn = self._conn

        def _set():
            conn.execute(
                "INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            conn.commit()

        await self._run(_set)

    async def add_subscription(self, session_id: str, sub_type: str, target: str) -> bool:
        conn = self._conn

        def _add():
            try:
                conn.execute(
                    "INSERT INTO subscriptions(session_id,type,target) VALUES(?,?,?)",
                    (session_id, sub_type, target),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

        return await self._run(_add)

    async def remove_subscription(self, session_id: str, sub_type: str, target: str) -> bool:
        conn = self._conn

        def _remove():
            cur = conn.execute(
                "DELETE FROM subscriptions WHERE session_id=? AND type=? AND target=?",
                (session_id, sub_type, target),
            )
            conn.commit()
            return cur.rowcount > 0

        return await self._run(_remove)

    async def get_subscription_id(self, session_id: str, sub_type: str, target: str) -> Optional[int]:
        conn = self._conn

        def _get():
            row = conn.execute(
                "SELECT id FROM subscriptions WHERE session_id=? AND type=? AND target=?",
                (session_id, sub_type, target),
            ).fetchone()
            return row[0] if row else None

        return await self._run(_get)

    async def list_subscriptions(self, session_id: Optional[str] = None) -> list[tuple]:
        conn = self._conn

        def _list():
            if session_id:
                return conn.execute(
                    "SELECT id,session_id,type,target FROM subscriptions WHERE session_id=?",
                    (session_id,),
                ).fetchall()
            return conn.execute(
                "SELECT id,session_id,type,target FROM subscriptions"
            ).fetchall()

        return await self._run(_list)

    async def filter_unseen(self, subscription_id: int, post_ids: list[str]) -> list[str]:
        if not post_ids:
            return []
        conn = self._conn

        def _filter():
            placeholders = ",".join("?" * len(post_ids))
            seen = {
                row[0]
                for row in conn.execute(
                    f"SELECT post_id FROM seen_posts WHERE subscription_id=? AND post_id IN ({placeholders})",
                    (subscription_id, *post_ids),
                ).fetchall()
            }
            return [pid for pid in post_ids if pid not in seen]

        return await self._run(_filter)

    async def filter_unsent(self, session_id: str, post_ids: list[str]) -> list[str]:
        if not post_ids:
            return []
        conn = self._conn

        def _filter():
            placeholders = ",".join("?" * len(post_ids))
            sent = {
                row[0]
                for row in conn.execute(
                    f"SELECT post_id FROM sent_posts WHERE session_id=? AND post_id IN ({placeholders})",
                    (session_id, *post_ids),
                ).fetchall()
            }
            return [pid for pid in post_ids if pid not in sent]

        return await self._run(_filter)

    async def mark_sent(self, session_id: str, post_ids: list[str]):
        if not post_ids:
            return
        conn = self._conn

        def _mark():
            conn.executemany(
                "INSERT OR IGNORE INTO sent_posts(session_id,post_id) VALUES(?,?)",
                [(session_id, pid) for pid in post_ids],
            )
            conn.commit()

        await self._run(_mark)

    async def mark_seen(self, subscription_id: int, post_ids: list[str]):
        if not post_ids:
            return
        conn = self._conn

        def _mark():
            conn.executemany(
                "INSERT OR IGNORE INTO seen_posts(subscription_id,post_id) VALUES(?,?)",
                [(subscription_id, pid) for pid in post_ids],
            )
            conn.commit()

        await self._run(_mark)
