import asyncio
import sqlite3
import json
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .db_migrations import DDL, SCHEMA_VERSION, get_schema_version, migrate
from .parser import Post


class LofterDB:
    def __init__(self, db_path: str):
        self._path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lofter-db")

    def _run(self, fn):
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(self._executor, fn)

    async def initialize(self):
        def _init():
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(DDL)
            conn.commit()
            ver = get_schema_version(conn)
            if ver < SCHEMA_VERSION:
                migrate(conn, ver)
            return conn

        self._conn = await self._run(_init)

    async def close(self):
        if self._conn:
            conn = self._conn
            await self._run(conn.close)
            self._conn = None
        self._executor.shutdown(wait=True)

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
            conn.executemany(
                "DELETE FROM pending_posts WHERE session_id=? AND type=? AND post_id=?",
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

    async def mark_delivered(self, session_id: str, sub_type: str, post_id: str):
        conn = self._conn

        def _mark():
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO seen_posts(session_id,type,post_id) VALUES(?,?,?)",
                    (session_id, sub_type, post_id),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO sent_posts(session_id,post_id) VALUES(?,?)",
                    (session_id, post_id),
                )
                conn.execute("DELETE FROM pending_posts WHERE session_id=? AND post_id=?", (session_id, post_id))

        await self._run(_mark)

    async def enqueue_posts(self, session_id: str, sub_type: str, source: str, posts: list[Post]):
        rows = [(session_id, sub_type, source, post.post_id, json.dumps(asdict(post), ensure_ascii=False)) for post in posts]

        def _enqueue():
            with self._conn:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO pending_posts(session_id,type,source,post_id,payload) VALUES(?,?,?,?,?)",
                    rows,
                )

        await self._run(_enqueue)

    async def pending_posts(self, session_id: str, sub_type: str, source: str) -> list[Post]:
        def _read():
            return self._conn.execute(
                "SELECT payload FROM pending_posts WHERE session_id=? AND type=? AND source=? ORDER BY rowid",
                (session_id, sub_type, source),
            ).fetchall()

        return [Post(**json.loads(row[0])) for row in await self._run(_read)]

    async def tag_scan_cursor(self, session_id: str, tag: str) -> tuple[int, int]:
        def _read():
            return self._conn.execute(
                "SELECT offset,before_time FROM tag_scan_cursors WHERE session_id=? AND target=?",
                (session_id, tag),
            ).fetchone()

        return await self._run(_read) or (0, 0)

    async def save_tag_page(self, session_id: str, tag: str, posts: list[Post], offset: int, before: int):
        rows = [(session_id, "tag", "", post.post_id, json.dumps(asdict(post), ensure_ascii=False)) for post in posts]

        def _save():
            with self._conn:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO pending_posts(session_id,type,source,post_id,payload) VALUES(?,?,?,?,?)", rows,
                )
                self._conn.execute(
                    "INSERT INTO tag_scan_cursors VALUES(?,?,?,?) ON CONFLICT(session_id,target) "
                    "DO UPDATE SET offset=excluded.offset,before_time=excluded.before_time",
                    (session_id, tag, offset, before),
                )

        await self._run(_save)

    async def clear_tag_scan_cursor(self, session_id: str, tag: str):
        def _clear():
            with self._conn:
                self._conn.execute("DELETE FROM tag_scan_cursors WHERE session_id=? AND target=?", (session_id, tag))

        await self._run(_clear)

    async def discard_pending(self, session_id: str, sub_type: str, post_ids: list[str]):
        def _discard():
            with self._conn:
                self._conn.executemany(
                    "DELETE FROM pending_posts WHERE session_id=? AND type=? AND post_id=?",
                    [(session_id, sub_type, post_id) for post_id in post_ids],
                )

        await self._run(_discard)

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
            conn.execute("DELETE FROM pending_posts WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM tag_scan_cursors WHERE session_id=?", (session_id,))
            conn.commit()

        await self._run(_clear)

    async def delete_config(self, key: str):
        """删除指定配置项。"""
        conn = self._conn

        def _del():
            conn.execute("DELETE FROM config WHERE key=?", (key,))
            conn.commit()

        await self._run(_del)

    async def add_author_block(self, session_id: str, kind: str, value: str, display: str) -> bool:
        conn = self._conn

        def _add():
            try:
                conn.execute(
                    "INSERT INTO author_blocks(session_id,kind,value,display) VALUES(?,?,?,?)",
                    (session_id, kind, value, display),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

        return await self._run(_add)

    async def remove_author_block(self, session_id: str, kind: str, value: str) -> bool:
        conn = self._conn

        def _remove():
            cur = conn.execute(
                "DELETE FROM author_blocks WHERE session_id=? AND kind=? AND value=?",
                (session_id, kind, value),
            )
            conn.commit()
            return cur.rowcount > 0

        return await self._run(_remove)

    async def list_author_blocks(self, session_id: str) -> list[tuple]:
        conn = self._conn

        def _list():
            return conn.execute(
                "SELECT session_id,kind,value,display,created_at FROM author_blocks WHERE session_id=? ORDER BY created_at ASC, display ASC",
                (session_id,),
            ).fetchall()

        return await self._run(_list)

    async def upsert_count_condition(self, name: str, expression: str):
        conn = self._conn

        def _upsert():
            conn.execute(
                "INSERT INTO count_conditions(name,expression) VALUES(?,?) "
                "ON CONFLICT(name) DO UPDATE SET expression=excluded.expression,updated_at=strftime('%s','now')",
                (name, expression),
            )
            conn.commit()

        await self._run(_upsert)

    async def delete_count_condition(self, name: str) -> bool:
        conn = self._conn

        def _delete():
            cur = conn.execute("DELETE FROM count_conditions WHERE name=?", (name,))
            conn.commit()
            return cur.rowcount > 0

        return await self._run(_delete)

    async def list_count_conditions(self) -> list[tuple[str, str]]:
        conn = self._conn

        def _list():
            return conn.execute(
                "SELECT name,expression FROM count_conditions ORDER BY updated_at DESC, name ASC"
            ).fetchall()

        return await self._run(_list)
