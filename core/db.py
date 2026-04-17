import asyncio
import json
import sqlite3
from typing import Optional

SCHEMA_VERSION = 2

_DDL_V2 = """
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    type       TEXT NOT NULL CHECK(type IN ('tag','blog')),
    role       TEXT NOT NULL CHECK(role IN ('subscribe','exclude')) DEFAULT 'subscribe',
    target     TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE(session_id, type, role, target)
);

CREATE TABLE IF NOT EXISTS seen_posts (
    session_id TEXT    NOT NULL,
    type       TEXT    NOT NULL,
    post_id    TEXT    NOT NULL,
    seen_at    INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (session_id, type, post_id)
);

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

    def _run(self, fn):
        loop = asyncio.get_event_loop()
        return loop.run_in_executor(None, fn)

    async def initialize(self):
        def _init():
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_DDL_V2)
            conn.commit()
            ver = _get_schema_version(conn)
            if ver < SCHEMA_VERSION:
                _migrate(conn, ver)
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

    async def add_subscription(self, session_id: str, sub_type: str, target: str, role: str = "subscribe") -> bool:
        conn = self._conn

        def _add():
            try:
                conn.execute(
                    "INSERT INTO subscriptions(session_id,type,role,target) VALUES(?,?,?,?)",
                    (session_id, sub_type, role, target),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

        return await self._run(_add)

    async def remove_subscription(self, session_id: str, sub_type: str, target: str, role: str = "subscribe") -> bool:
        conn = self._conn

        def _remove():
            cur = conn.execute(
                "DELETE FROM subscriptions WHERE session_id=? AND type=? AND role=? AND target=?",
                (session_id, sub_type, role, target),
            )
            conn.commit()
            return cur.rowcount > 0

        return await self._run(_remove)

    async def remove_subscription_by_id(self, sub_id: int) -> bool:
        conn = self._conn

        def _remove():
            cur = conn.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
            conn.commit()
            return cur.rowcount > 0

        return await self._run(_remove)

    async def get_subscription_id(self, session_id: str, sub_type: str, target: str, role: str = "subscribe") -> Optional[int]:
        conn = self._conn

        def _get():
            row = conn.execute(
                "SELECT id FROM subscriptions WHERE session_id=? AND type=? AND role=? AND target=?",
                (session_id, sub_type, role, target),
            ).fetchone()
            return row[0] if row else None

        return await self._run(_get)

    async def list_subscriptions(self, session_id: Optional[str] = None) -> list[tuple]:
        conn = self._conn

        def _list():
            if session_id:
                return conn.execute(
                    "SELECT id,session_id,type,role,target,created_at FROM subscriptions WHERE session_id=? ORDER BY id ASC",
                    (session_id,),
                ).fetchall()
            return conn.execute(
                "SELECT id,session_id,type,role,target,created_at FROM subscriptions ORDER BY id ASC"
            ).fetchall()

        return await self._run(_list)

    async def filter_unseen_session(self, session_id: str, sub_type: str, post_ids: list[str]) -> list[str]:
        if not post_ids:
            return []
        conn = self._conn

        def _filter():
            placeholders = ",".join("?" * len(post_ids))
            seen = {
                row[0]
                for row in conn.execute(
                    f"SELECT post_id FROM seen_posts WHERE session_id=? AND type=? AND post_id IN ({placeholders})",
                    (session_id, sub_type, *post_ids),
                ).fetchall()
            }
            return [pid for pid in post_ids if pid not in seen]

        return await self._run(_filter)

    async def mark_seen_session(self, session_id: str, sub_type: str, post_ids: list[str]):
        if not post_ids:
            return
        conn = self._conn

        def _mark():
            conn.executemany(
                "INSERT OR IGNORE INTO seen_posts(session_id,type,post_id) VALUES(?,?,?)",
                [(session_id, sub_type, pid) for pid in post_ids],
            )
            conn.commit()

        await self._run(_mark)

    async def seen_count(self, session_id: str, sub_type: str) -> int:
        conn = self._conn

        def _count():
            row = conn.execute(
                "SELECT COUNT(*) FROM seen_posts WHERE session_id=? AND type=?",
                (session_id, sub_type),
            ).fetchone()
            return row[0] if row else 0

        return await self._run(_count)

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

    async def clear_session(self, session_id: str):
        """删除指定 session 的所有 seen/sent 记录，用于测试清理。"""
        conn = self._conn

        def _clear():
            conn.execute("DELETE FROM seen_posts WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM sent_posts WHERE session_id=?", (session_id,))
            conn.commit()

        await self._run(_clear)

    async def delete_config(self, key: str):
        """删除指定配置项。"""
        conn = self._conn

        def _del():
            conn.execute("DELETE FROM config WHERE key=?", (key,))
            conn.commit()

        await self._run(_del)


def _get_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM config WHERE key='schema_version'").fetchone()
    return int(row[0]) if row else 1


def _migrate(conn: sqlite3.Connection, from_ver: int):
    if from_ver < 2:
        _migrate_v1_to_v2(conn)
    conn.execute(
        "INSERT INTO config(key,value) VALUES('schema_version','2') "
        "ON CONFLICT(key) DO UPDATE SET value='2'"
    )
    conn.commit()


def _migrate_v1_to_v2(conn: sqlite3.Connection):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "subscriptions" not in tables:
        return

    cols = {r[1] for r in conn.execute("PRAGMA table_info(subscriptions)").fetchall()}
    if "role" in cols:
        return

    conn.execute("ALTER TABLE subscriptions RENAME TO subscriptions_old")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            type       TEXT NOT NULL CHECK(type IN ('tag','blog')),
            role       TEXT NOT NULL CHECK(role IN ('subscribe','exclude')) DEFAULT 'subscribe',
            target     TEXT NOT NULL,
            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            UNIQUE(session_id, type, role, target)
        );
    """)

    rows = conn.execute("SELECT id,session_id,type,target,filter_rule FROM subscriptions_old").fetchall() if "filter_rule" in cols else conn.execute("SELECT id,session_id,type,target,NULL FROM subscriptions_old").fetchall()
    id_map: dict[int, tuple[str, str]] = {}
    for row in rows:
        old_id, session_id, sub_type, target, filter_rule_json = row
        id_map[old_id] = (session_id, sub_type)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO subscriptions(session_id,type,role,target) VALUES(?,?,?,?)",
                (session_id, sub_type, "subscribe", target),
            )
        except sqlite3.IntegrityError:
            pass
        if filter_rule_json:
            _migrate_filter_rule(conn, session_id, sub_type, filter_rule_json)

    _migrate_seen_posts_v2(conn, id_map)
    conn.execute("DROP TABLE subscriptions_old")


def _migrate_filter_rule(conn: sqlite3.Connection, session_id: str, sub_type: str, filter_rule_json: str):
    try:
        d = json.loads(filter_rule_json)
    except Exception:
        return
    for tag in d.get("search_tags", [])[1:]:
        if tag:
            _insert_sub(conn, session_id, sub_type, "subscribe", tag)
    for tag in d.get("include_tags", []):
        if tag:
            _insert_sub(conn, session_id, sub_type, "subscribe", tag)
    for group in d.get("or_tag_groups", []):
        for tag in group:
            if tag:
                _insert_sub(conn, session_id, sub_type, "subscribe", tag)
    for tag in d.get("exclude_tags", []):
        if tag:
            _insert_sub(conn, session_id, sub_type, "exclude", tag)


def _insert_sub(conn: sqlite3.Connection, session_id: str, sub_type: str, role: str, target: str):
    try:
        conn.execute(
            "INSERT OR IGNORE INTO subscriptions(session_id,type,role,target) VALUES(?,?,?,?)",
            (session_id, sub_type, role, target),
        )
    except sqlite3.IntegrityError:
        pass


def _migrate_seen_posts_v2(conn: sqlite3.Connection, id_map: dict[int, tuple[str, str]]):
    sp_tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "seen_posts" not in sp_tables:
        return
    sp_cols = {r[1] for r in conn.execute("PRAGMA table_info(seen_posts)").fetchall()}
    if "subscription_id" not in sp_cols:
        return

    conn.execute("ALTER TABLE seen_posts RENAME TO seen_posts_old")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS seen_posts (
            session_id TEXT    NOT NULL,
            type       TEXT    NOT NULL,
            post_id    TEXT    NOT NULL,
            seen_at    INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            PRIMARY KEY (session_id, type, post_id)
        );
    """)

    rows = conn.execute("SELECT subscription_id,post_id,seen_at FROM seen_posts_old").fetchall()
    for sub_id, post_id, seen_at in rows:
        if sub_id not in id_map:
            continue
        session_id, sub_type = id_map[sub_id]
        conn.execute(
            "INSERT OR IGNORE INTO seen_posts(session_id,type,post_id,seen_at) VALUES(?,?,?,?)",
            (session_id, sub_type, post_id, seen_at),
        )
    conn.execute("DROP TABLE seen_posts_old")
