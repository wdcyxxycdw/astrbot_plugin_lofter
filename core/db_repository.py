from __future__ import annotations

import sqlite3
import time
from typing import Optional

from .db_checkpoints import (
    consume_legacy_checkpoints as _consume_legacy_checkpoints,
    filter_legacy_floor as _filter_legacy_floor,
    update_checkpoint_watermark as _update_checkpoint_watermark,
)
from .post_identity import canonical_post_id


class LofterRepositoryMixin:
    async def get_config(self, key: str) -> Optional[str]:
        def get(conn: sqlite3.Connection) -> Optional[str]:
            row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

        return await self.transaction(get)

    async def set_config(self, key: str, value: str) -> None:
        await self.transaction(lambda conn: conn.execute(
            "INSERT INTO config(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        ))

    async def delete_config(self, key: str) -> bool:
        return await self.transaction(
            lambda conn: conn.execute("DELETE FROM config WHERE key=?", (key,)).rowcount > 0
        )

    async def add_subscription(
        self, session_id: str, sub_type: str, target: str, role: str = "subscribe"
    ) -> bool:
        return await self.transaction(
            lambda conn: _add_subscription(conn, session_id, sub_type, target, role)
        )

    async def remove_subscription(
        self, session_id: str, sub_type: str, target: str, role: str = "subscribe"
    ) -> bool:
        def remove(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "DELETE FROM subscriptions WHERE session_id=? AND type=? AND role=? AND target=?",
                (session_id, sub_type, role, target),
            )
            if cur.rowcount:
                _refresh_session_activity(conn, session_id)
            return cur.rowcount > 0

        return await self.transaction(remove)

    async def remove_subscription_by_id(self, sub_id: int) -> bool:
        def remove(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT session_id FROM subscriptions WHERE id=?", (sub_id,)
            ).fetchone()
            if not row:
                return False
            conn.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
            _refresh_session_activity(conn, row[0])
            return True

        return await self.transaction(remove)

    async def get_subscription_id(
        self, session_id: str, sub_type: str, target: str, role: str = "subscribe"
    ) -> Optional[int]:
        def get(conn: sqlite3.Connection) -> Optional[int]:
            row = conn.execute(
                "SELECT id FROM subscriptions WHERE session_id=? AND type=? AND role=? AND target=?",
                (session_id, sub_type, role, target),
            ).fetchone()
            return row[0] if row else None

        return await self.transaction(get)

    async def list_subscriptions(self, session_id: Optional[str] = None) -> list[tuple]:
        def list_rows(conn: sqlite3.Connection) -> list[tuple]:
            columns = (
                "id,session_id,type,role,target,created_at,state,revision,initialized_at"
            )
            if session_id is not None:
                return conn.execute(
                    f"SELECT {columns} FROM subscriptions WHERE session_id=? ORDER BY id ASC",
                    (session_id,),
                ).fetchall()
            return conn.execute(
                f"SELECT {columns} FROM subscriptions ORDER BY id ASC"
            ).fetchall()

        return await self.transaction(list_rows)

    async def capture_session_snapshot(self, session_id: str) -> tuple:
        return await self.transaction(
            lambda conn: _capture_session_snapshot(conn, session_id)
        )

    async def initialize_runtime_subscriptions(
        self,
        session_id: str,
        sub_type: str,
        subscribes: list[str],
        excludes: list[str],
        seen_by_target: dict[str, list[tuple[str, int]]],
        expected_type_revision: int,
        expected_policy_generation: int,
        expected_rows: tuple[tuple, ...],
        mark_existing_seen: bool = False,
    ) -> tuple[list[str], list[str]]:
        return await self.transaction(lambda conn: _initialize_runtime_subscriptions(
            conn,
            session_id,
            sub_type,
            subscribes,
            excludes,
            seen_by_target,
            expected_type_revision,
            expected_policy_generation,
            expected_rows,
            mark_existing_seen,
        ))

    async def remove_runtime_subscription(
        self, session_id: str, sub_type: str, target: str, role: str
    ) -> bool:
        return await self.transaction(lambda conn: _remove_runtime_subscription(
            conn, session_id, sub_type, target, role
        ))

    async def remove_runtime_subscription_by_index(
        self, session_id: str, index: int
    ) -> tuple[tuple | None, int]:
        return await self.transaction(lambda conn: _remove_runtime_subscription_by_index(
            conn, session_id, index
        ))

    async def filter_unseen_session(
        self, session_id: str, sub_type: str, post_ids: list[str]
    ) -> list[str]:
        if not post_ids:
            return []
        return await self.transaction(
            lambda conn: _filter_unseen(conn, session_id, sub_type, post_ids)
        )

    async def filter_unseen_targets(
        self, session_id: str, sub_type: str, posts_by_target: dict[str, list[str]]
    ) -> list[str]:
        if not any(posts_by_target.values()):
            return []
        return await self.transaction(
            lambda conn: _filter_unseen_targets(
                conn, session_id, sub_type, posts_by_target
            )
        )

    async def mark_seen_session(
        self, session_id: str, sub_type: str, post_ids: list[str]
    ) -> int:
        if not post_ids:
            return 0
        return await self.transaction(
            lambda conn: _mark_seen_session(conn, session_id, sub_type, post_ids)
        )

    async def mark_seen_targets(
        self, session_id: str, sub_type: str, posts_by_target: dict[str, list[str]]
    ) -> int:
        if not any(posts_by_target.values()):
            return 0
        return await self.transaction(
            lambda conn: _mark_seen_targets(
                conn, session_id, sub_type, posts_by_target
            )
        )

    async def seen_count(self, session_id: str, sub_type: str) -> int:
        def count(conn: sqlite3.Connection) -> int:
            row = conn.execute("""
                SELECT COUNT(DISTINCT sp.post_id) FROM seen_posts sp
                JOIN subscriptions s ON s.id=sp.subscription_id
                WHERE s.session_id=? AND s.type=? AND s.role='subscribe' AND s.state='active'
            """, (session_id, sub_type)).fetchone()
            return row[0] if row else 0

        return await self.transaction(count)

    async def filter_unsent(self, session_id: str, post_ids: list[str]) -> list[str]:
        if not post_ids:
            return []
        return await self.transaction(
            lambda conn: _filter_unsent(conn, session_id, post_ids)
        )

    async def mark_sent(self, session_id: str, post_ids: list[str]) -> int:
        if not post_ids:
            return 0
        return await self.transaction(lambda conn: _mark_sent(conn, session_id, post_ids))

    async def mark_accepted_session(
        self, session_id: str, sub_type: str, post_ids: list[str]
    ) -> int:
        if not post_ids:
            return 0

        def accept(conn: sqlite3.Connection) -> int:
            _mark_seen_session(conn, session_id, sub_type, post_ids)
            return _mark_sent(conn, session_id, post_ids)

        return await self.transaction(accept)

    async def mark_accepted_targets(
        self, session_id: str, sub_type: str, posts_by_target: dict[str, list[str]]
    ) -> int:
        if not any(posts_by_target.values()):
            return 0

        def accept(conn: sqlite3.Connection) -> int:
            _mark_seen_targets(conn, session_id, sub_type, posts_by_target)
            post_ids = list(dict.fromkeys(
                post_id for values in posts_by_target.values() for post_id in values
            ))
            return _mark_sent(conn, session_id, post_ids)

        return await self.transaction(accept)

    async def apply_tag_legacy_rules(
        self, session_id: str, posts_by_target: dict[str, list[str]]
    ) -> tuple[dict[str, list[str]], bool]:
        if not any(posts_by_target.values()):
            return posts_by_target, False
        return await self.transaction(
            lambda conn: _apply_tag_legacy_rules(conn, session_id, posts_by_target)
        )

    async def consume_legacy_checkpoints(
        self, session_id: str, sub_type: str, post_ids: list[str],
        subscription_target: str | None = None,
    ) -> list[str] | None:
        return await self.transaction(lambda conn: _consume_legacy_checkpoints(
            conn, session_id, sub_type, post_ids, subscription_target
        ))

    async def import_legacy_checkpoint(
        self, session_id: str, sub_type: str, target: str, post_id: str
    ) -> None:
        def insert(conn: sqlite3.Connection) -> None:
            row = conn.execute("""
                SELECT id FROM subscriptions WHERE session_id=? AND type=?
                  AND role='subscribe' AND target=?
            """, (session_id, sub_type, target)).fetchone()
            if row is None:
                return
            conn.execute("""
                INSERT INTO legacy_checkpoints(subscription_id,post_id,created_at)
                VALUES(?,?,?) ON CONFLICT(subscription_id) DO UPDATE SET
                    post_id=excluded.post_id,created_at=excluded.created_at
            """, (row[0], canonical_post_id(post_id), int(time.time())))
            _update_checkpoint_watermark(conn, row[0], post_id)

        await self.transaction(insert)

    async def filter_legacy_floor(
        self, session_id: str, sub_type: str, post_ids: list[str],
        subscription_target: str | None = None,
    ) -> tuple[list[str], list[str]]:
        return await self.transaction(lambda conn: _filter_legacy_floor(
            conn, session_id, sub_type, post_ids, subscription_target
        ))

    async def clear_session(self, session_id: str) -> None:
        def clear(conn: sqlite3.Connection) -> None:
            conn.execute(
                "DELETE FROM seen_posts WHERE subscription_id IN "
                "(SELECT id FROM subscriptions WHERE session_id=?)",
                (session_id,),
            )
            conn.execute("DELETE FROM deliveries WHERE session_id=?", (session_id,))

        await self.transaction(clear)

    async def add_author_block(
        self, session_id: str, kind: str, value: str, display: str
    ) -> bool:
        def add(conn: sqlite3.Connection) -> bool:
            cur = conn.execute("""
                INSERT INTO author_blocks(session_id,kind,value,display) VALUES(?,?,?,?)
                ON CONFLICT(session_id,kind,value) DO NOTHING
            """, (session_id, kind, value, display))
            return cur.rowcount > 0

        return await self.transaction(add)

    async def remove_author_block(self, session_id: str, kind: str, value: str) -> bool:
        return await self.transaction(lambda conn: conn.execute(
            "DELETE FROM author_blocks WHERE session_id=? AND kind=? AND value=?",
            (session_id, kind, value),
        ).rowcount > 0)

    async def list_author_blocks(self, session_id: str) -> list[tuple]:
        return await self.transaction(lambda conn: conn.execute("""
            SELECT session_id,kind,value,display,created_at FROM author_blocks
            WHERE session_id=? ORDER BY created_at ASC, display ASC
        """, (session_id,)).fetchall())

    async def mutate_author_blocks(
        self, session_id: str, keys: list[tuple[str, str, str]], add: bool
    ) -> bool:
        if not keys:
            return False
        return await self.transaction(
            lambda conn: _mutate_author_blocks(conn, session_id, keys, add)
        )

    async def upsert_count_condition(self, name: str, expression: str) -> None:
        await self.transaction(lambda conn: conn.execute("""
            INSERT INTO count_conditions(name,expression) VALUES(?,?)
            ON CONFLICT(name) DO UPDATE SET
                expression=excluded.expression,updated_at=strftime('%s','now')
        """, (name, expression)))

    async def delete_count_condition(self, name: str) -> bool:
        return await self.transaction(
            lambda conn: conn.execute(
                "DELETE FROM count_conditions WHERE name=?", (name,)
            ).rowcount > 0
        )

    async def list_count_conditions(self) -> list[tuple[str, str]]:
        return await self.transaction(lambda conn: conn.execute(
            "SELECT name,expression FROM count_conditions ORDER BY updated_at DESC, name ASC"
        ).fetchall())


def _capture_session_snapshot(
    conn: sqlite3.Connection, session_id: str
) -> tuple[int, int, int, tuple[tuple, ...], tuple[tuple, ...]]:
    tag_revision = _current_type_revision(conn, session_id, "tag")
    blog_revision = _current_type_revision(conn, session_id, "blog")
    policy_generation = _current_policy_generation(conn, session_id)
    subscriptions = tuple(conn.execute("""
        SELECT id,type,role,target,state,revision FROM subscriptions
        WHERE session_id=? AND state='active' ORDER BY id ASC
    """, (session_id,)).fetchall())
    blocks = tuple(conn.execute("""
        SELECT session_id,kind,value,display,created_at FROM author_blocks
        WHERE session_id=? ORDER BY created_at ASC,display ASC
    """, (session_id,)).fetchall())
    return tag_revision, blog_revision, policy_generation, subscriptions, blocks


def _current_type_revision(
    conn: sqlite3.Connection, session_id: str, sub_type: str
) -> int:
    row = conn.execute("""
        SELECT revision FROM subscription_revisions
        WHERE session_id=? AND subscription_type=?
    """, (session_id, sub_type)).fetchone()
    return row[0] if row else 0


def _current_policy_generation(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT policy_generation FROM session_policies WHERE session_id=?",
        (session_id,),
    ).fetchone()
    return row[0] if row else 0


def _snapshot_rows(
    conn: sqlite3.Connection, session_id: str
) -> tuple[tuple, ...]:
    return tuple(conn.execute("""
        SELECT id,type,role,target,state,revision FROM subscriptions
        WHERE session_id=? AND state='active' ORDER BY id ASC
    """, (session_id,)).fetchall())


def _assert_subscription_snapshot(
    conn: sqlite3.Connection,
    session_id: str,
    sub_type: str,
    expected_type_revision: int,
    expected_policy_generation: int,
    expected_rows: tuple[tuple, ...],
) -> None:
    current = (
        _current_type_revision(conn, session_id, sub_type),
        _current_policy_generation(conn, session_id),
        _snapshot_rows(conn, session_id),
    )
    expected = (
        expected_type_revision,
        expected_policy_generation,
        expected_rows,
    )
    if current != expected:
        raise RuntimeError("subscription snapshot changed")


def _next_type_revision(
    conn: sqlite3.Connection, session_id: str, sub_type: str, now: int
) -> int:
    revision = _current_type_revision(conn, session_id, sub_type) + 1
    conn.execute("""
        INSERT INTO subscription_revisions(
            session_id,subscription_type,revision,updated_at
        ) VALUES(?,?,?,?) ON CONFLICT(session_id,subscription_type) DO UPDATE SET
            revision=excluded.revision,updated_at=excluded.updated_at
    """, (session_id, sub_type, revision, now))
    return revision


def _advance_policy_generation(
    conn: sqlite3.Connection, session_id: str, now: int
) -> int:
    generation = _current_policy_generation(conn, session_id) + 1
    conn.execute("""
        INSERT INTO session_policies(session_id,policy_generation,updated_at)
        VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET
            policy_generation=excluded.policy_generation,updated_at=excluded.updated_at
    """, (session_id, generation, now))
    return generation


def _initialize_runtime_subscriptions(
    conn: sqlite3.Connection,
    session_id: str,
    sub_type: str,
    subscribes: list[str],
    excludes: list[str],
    seen_by_target: dict[str, list[tuple[str, int]]],
    expected_type_revision: int,
    expected_policy_generation: int,
    expected_rows: tuple[tuple, ...],
    mark_existing_seen: bool,
) -> tuple[list[str], list[str]]:
    _assert_subscription_snapshot(
        conn,
        session_id,
        sub_type,
        expected_type_revision,
        expected_policy_generation,
        expected_rows,
    )
    existing = _existing_subscription_keys(conn, session_id, sub_type)
    added_subs = [target for target in subscribes if ("subscribe", target) not in existing]
    added_excludes = [target for target in excludes if ("exclude", target) not in existing]
    if not added_subs and not added_excludes:
        if mark_existing_seen:
            _mark_existing_preview_seen(conn, session_id, sub_type, seen_by_target)
        return [], []
    now = int(time.time())
    revision = _next_type_revision(conn, session_id, sub_type, now)
    _advance_policy_generation(conn, session_id, now)
    new_sub_ids = []
    for target in added_subs:
        sub_id = _insert_runtime_subscription(
            conn, session_id, sub_type, "subscribe", target, revision, now
        )
        _inherit_seen_union(conn, session_id, sub_type, sub_id)
        _mark_seen_rows(conn, sub_id, seen_by_target.get(target, ()), now)
        new_sub_ids.append(sub_id)
    for sub_id in new_sub_ids:
        _activate_runtime_subscription(conn, sub_id, now)
    for target in added_excludes:
        sub_id = _insert_runtime_subscription(
            conn, session_id, sub_type, "exclude", target, revision, now
        )
        _activate_runtime_subscription(conn, sub_id, now)
    if mark_existing_seen:
        _mark_existing_preview_seen(conn, session_id, sub_type, seen_by_target)
    _refresh_session_activity(conn, session_id)
    return added_subs, added_excludes


def _existing_subscription_keys(
    conn: sqlite3.Connection, session_id: str, sub_type: str
) -> set[tuple[str, str]]:
    return {
        (row[0], row[1])
        for row in conn.execute("""
            SELECT role,target FROM subscriptions
            WHERE session_id=? AND type=?
        """, (session_id, sub_type)).fetchall()
    }


def _insert_runtime_subscription(
    conn: sqlite3.Connection,
    session_id: str,
    sub_type: str,
    role: str,
    target: str,
    revision: int,
    now: int,
) -> int:
    cursor = conn.execute("""
        INSERT INTO subscriptions(
            session_id,type,role,target,state,revision,
            initialized_at,created_at,updated_at
        ) VALUES(?,?,?,?,'warming',?,NULL,?,?)
    """, (session_id, sub_type, role, target, revision, now, now))
    sub_id = cursor.lastrowid
    conn.execute("""
        INSERT INTO subscription_watermarks(
            subscription_id,history_before,legacy_post_id_floor,updated_at
        ) VALUES(?,?,NULL,?)
    """, (sub_id, now, now))
    return sub_id


def _activate_runtime_subscription(
    conn: sqlite3.Connection, sub_id: int, now: int
) -> None:
    conn.execute("""
        UPDATE subscriptions SET state='active',initialized_at=?,updated_at=?
        WHERE id=? AND state='warming'
    """, (now, now, sub_id))


def _mark_seen_rows(
    conn: sqlite3.Connection,
    sub_id: int,
    rows: list[tuple[str, int]] | tuple[tuple[str, int], ...],
    seen_at: int,
) -> None:
    conn.executemany("""
        INSERT INTO seen_posts(subscription_id,post_id,published_at,seen_at)
        VALUES(?,?,?,?) ON CONFLICT(subscription_id,post_id) DO NOTHING
    """, [
        (sub_id, canonical_post_id(post_id), published_at, seen_at)
        for post_id, published_at in rows
    ])


def _mark_existing_preview_seen(
    conn: sqlite3.Connection,
    session_id: str,
    sub_type: str,
    seen_by_target: dict[str, list[tuple[str, int]]],
) -> None:
    now = int(time.time())
    for target, posts in seen_by_target.items():
        rows = conn.execute("""
            SELECT id FROM subscriptions WHERE session_id=? AND type=?
              AND role='subscribe' AND target=? AND state='active'
        """, (session_id, sub_type, target)).fetchall()
        for (sub_id,) in rows:
            _mark_seen_rows(conn, sub_id, posts, now)


def _remove_runtime_subscription(
    conn: sqlite3.Connection,
    session_id: str,
    sub_type: str,
    target: str,
    role: str,
) -> bool:
    cursor = conn.execute("""
        DELETE FROM subscriptions
        WHERE session_id=? AND type=? AND target=? AND role=?
    """, (session_id, sub_type, target, role))
    if cursor.rowcount == 0:
        return False
    now = int(time.time())
    _next_type_revision(conn, session_id, sub_type, now)
    _advance_policy_generation(conn, session_id, now)
    _refresh_session_activity(conn, session_id)
    return True


def _remove_runtime_subscription_by_index(
    conn: sqlite3.Connection, session_id: str, index: int
) -> tuple[tuple | None, int]:
    rows = conn.execute("""
        SELECT id,session_id,type,role,target,created_at,state,revision,initialized_at
        FROM subscriptions WHERE session_id=? ORDER BY id ASC
    """, (session_id,)).fetchall()
    if index < 1 or index > len(rows):
        return None, len(rows)
    selected = rows[index - 1]
    conn.execute(
        "DELETE FROM subscriptions WHERE id=? AND session_id=?",
        (selected[0], session_id),
    )
    now = int(time.time())
    _next_type_revision(conn, session_id, selected[2], now)
    _advance_policy_generation(conn, session_id, now)
    _refresh_session_activity(conn, session_id)
    return selected, len(rows)


def _mutate_author_blocks(
    conn: sqlite3.Connection,
    session_id: str,
    keys: list[tuple[str, str, str]],
    add: bool,
) -> bool:
    changed = 0
    if add:
        for kind, value, display in keys:
            changed += conn.execute("""
                INSERT INTO author_blocks(session_id,kind,value,display)
                VALUES(?,?,?,?) ON CONFLICT(session_id,kind,value) DO NOTHING
            """, (session_id, kind, value, display)).rowcount
    else:
        for kind, value, _ in keys:
            changed += conn.execute("""
                DELETE FROM author_blocks
                WHERE session_id=? AND kind=? AND value=?
            """, (session_id, kind, value)).rowcount
    if changed:
        _advance_policy_generation(conn, session_id, int(time.time()))
    return changed > 0



def _add_subscription(
    conn: sqlite3.Connection, session_id: str, sub_type: str, target: str, role: str
) -> bool:
    now = int(time.time())
    revision_row = conn.execute("""
        SELECT revision FROM subscription_revisions
        WHERE session_id=? AND subscription_type=?
    """, (session_id, sub_type)).fetchone()
    revision = revision_row[0] if revision_row else 1
    cur = conn.execute("""
        INSERT INTO subscriptions(
            session_id,type,role,target,state,revision,initialized_at,created_at,updated_at
        ) VALUES(?,?,?,?,'active',?,?,?,?)
        ON CONFLICT(session_id,type,role,target) DO NOTHING
    """, (session_id, sub_type, role, target, revision, now, now, now))
    if cur.rowcount == 0:
        return False
    sub_id = cur.lastrowid
    _initialize_subscription_metadata(conn, session_id, sub_type, role, sub_id, revision, now)
    if role == "subscribe":
        _inherit_seen_union(conn, session_id, sub_type, sub_id)
    return True


def _initialize_subscription_metadata(
    conn: sqlite3.Connection, session_id: str, sub_type: str, role: str,
    sub_id: int, revision: int, now: int,
) -> None:
    conn.execute("""
        INSERT INTO subscription_revisions(session_id,subscription_type,revision,updated_at)
        VALUES(?,?,?,?) ON CONFLICT(session_id,subscription_type) DO NOTHING
    """, (session_id, sub_type, revision, now))
    conn.execute("""
        INSERT INTO session_policies(session_id,policy_generation,updated_at)
        VALUES(?,1,?) ON CONFLICT(session_id) DO NOTHING
    """, (session_id, now))
    inactive_since = None if role == "subscribe" else now
    conn.execute("""
        INSERT INTO session_activity(session_id,inactive_since,updated_at) VALUES(?,?,?)
        ON CONFLICT(session_id) DO UPDATE SET
            inactive_since=CASE WHEN ?='subscribe' THEN NULL ELSE inactive_since END,
            updated_at=excluded.updated_at
    """, (session_id, inactive_since, now, role))
    conn.execute("""
        INSERT INTO subscription_watermarks(
            subscription_id,history_before,legacy_post_id_floor,updated_at
        ) VALUES(?,?,NULL,?) ON CONFLICT(subscription_id) DO NOTHING
    """, (sub_id, now, now))


def _inherit_seen_union(
    conn: sqlite3.Connection, session_id: str, sub_type: str, new_sub_id: int
) -> None:
    rows = conn.execute("""
        SELECT sp.post_id,sp.published_at,sp.seen_at FROM seen_posts sp
        JOIN subscriptions s ON s.id=sp.subscription_id
        WHERE s.session_id=? AND s.type=? AND s.role='subscribe'
          AND s.state='active' AND s.id<>?
    """, (session_id, sub_type, new_sub_id)).fetchall()
    merged: dict[str, tuple[int, int]] = {}
    for post_id, published_at, seen_at in rows:
        canonical = canonical_post_id(post_id)
        previous = merged.get(canonical)
        values = (published_at, seen_at)
        merged[canonical] = values if previous is None else (
            min(previous[0], published_at), min(previous[1], seen_at)
        )
    conn.executemany("""
        INSERT INTO seen_posts(subscription_id,post_id,published_at,seen_at) VALUES(?,?,?,?)
        ON CONFLICT(subscription_id,post_id) DO NOTHING
    """, [(new_sub_id, post_id, *times) for post_id, times in merged.items()])


def _refresh_session_activity(conn: sqlite3.Connection, session_id: str) -> None:
    has_subscribe = conn.execute(
        "SELECT 1 FROM subscriptions WHERE session_id=? AND role='subscribe' LIMIT 1",
        (session_id,),
    ).fetchone()
    now = int(time.time())
    conn.execute("""
        INSERT INTO session_activity(session_id,inactive_since,updated_at) VALUES(?,?,?)
        ON CONFLICT(session_id) DO UPDATE SET
            inactive_since=CASE
                WHEN ? THEN NULL ELSE COALESCE(session_activity.inactive_since,excluded.inactive_since)
            END,
            updated_at=excluded.updated_at
    """, (session_id, None if has_subscribe else now, now, bool(has_subscribe)))


def _active_subscription_ids(
    conn: sqlite3.Connection, session_id: str, sub_type: str
) -> list[int]:
    return [row[0] for row in conn.execute("""
        SELECT id FROM subscriptions
        WHERE session_id=? AND type=? AND role='subscribe' AND state='active'
    """, (session_id, sub_type)).fetchall()]


def _apply_tag_legacy_rules(
    conn: sqlite3.Connection, session_id: str,
    posts_by_target: dict[str, list[str]],
) -> tuple[dict[str, list[str]], bool]:
    result: dict[str, list[str]] = {}
    checkpoint_applied = False
    for target, post_ids in posts_by_target.items():
        eligible = _consume_legacy_checkpoints(
            conn, session_id, "tag", post_ids, target
        )
        if eligible is not None:
            checkpoint_applied = True
            result[target] = eligible
            continue
        eligible, suppressed = _filter_legacy_floor(
            conn, session_id, "tag", post_ids, target
        )
        _mark_seen_targets(conn, session_id, "tag", {target: suppressed})
        result[target] = eligible
    return result, checkpoint_applied


def _filter_unseen_targets(
    conn: sqlite3.Connection, session_id: str, sub_type: str,
    posts_by_target: dict[str, list[str]],
) -> list[str]:
    eligible = []
    for target, post_ids in posts_by_target.items():
        canonical = [canonical_post_id(item) for item in post_ids]
        if not canonical:
            continue
        placeholders = ",".join("?" for _ in canonical)
        rows = conn.execute(f"""
            SELECT sp.post_id FROM seen_posts sp
            JOIN subscriptions s ON s.id=sp.subscription_id
            WHERE s.session_id=? AND s.type=? AND s.role='subscribe'
              AND s.state='active' AND s.target=?
              AND sp.post_id IN ({placeholders})
        """, (session_id, sub_type, target, *canonical)).fetchall()
        seen = {row[0] for row in rows}
        eligible.extend(
            original for original, key in zip(post_ids, canonical) if key not in seen
        )
    return list(dict.fromkeys(eligible))


def _filter_unseen(
    conn: sqlite3.Connection, session_id: str, sub_type: str, post_ids: list[str]
) -> list[str]:
    canonical = [canonical_post_id(item) for item in post_ids]
    placeholders = ",".join("?" for _ in canonical)
    rows = conn.execute(f"""
        SELECT DISTINCT sp.post_id FROM seen_posts sp
        JOIN subscriptions s ON s.id=sp.subscription_id
        WHERE s.session_id=? AND s.type=? AND s.role='subscribe' AND s.state='active'
          AND sp.post_id IN ({placeholders})
    """, (session_id, sub_type, *canonical)).fetchall()
    seen = {row[0] for row in rows}
    return [original for original, key in zip(post_ids, canonical) if key not in seen]


def _mark_seen_targets(
    conn: sqlite3.Connection, session_id: str, sub_type: str,
    posts_by_target: dict[str, list[str]],
) -> int:
    now = int(time.time())
    inserted = 0
    sql = """
        INSERT INTO seen_posts(subscription_id,post_id,published_at,seen_at) VALUES(?,?,?,?)
        ON CONFLICT(subscription_id,post_id) DO NOTHING
    """
    for target, post_ids in posts_by_target.items():
        rows = conn.execute("""
            SELECT id FROM subscriptions WHERE session_id=? AND type=?
              AND role='subscribe' AND state='active' AND target=?
        """, (session_id, sub_type, target)).fetchall()
        for (sub_id,) in rows:
            for post_id in post_ids:
                cur = conn.execute(sql, (sub_id, canonical_post_id(post_id), now, now))
                inserted += cur.rowcount
    return inserted


def _mark_seen_session(
    conn: sqlite3.Connection, session_id: str, sub_type: str, post_ids: list[str]
) -> int:
    subscriptions = _active_subscription_ids(conn, session_id, sub_type)
    now = int(time.time())
    inserted = 0
    sql = """
        INSERT INTO seen_posts(subscription_id,post_id,published_at,seen_at) VALUES(?,?,?,?)
        ON CONFLICT(subscription_id,post_id) DO NOTHING
    """
    for sub_id in subscriptions:
        for post_id in post_ids:
            cur = conn.execute(sql, (sub_id, canonical_post_id(post_id), now, now))
            inserted += cur.rowcount
    return inserted


def _filter_unsent(
    conn: sqlite3.Connection, session_id: str, post_ids: list[str]
) -> list[str]:
    canonical = [canonical_post_id(item) for item in post_ids]
    placeholders = ",".join("?" for _ in canonical)
    rows = conn.execute(f"""
        SELECT post_id FROM deliveries
        WHERE session_id=? AND status='accepted' AND post_id IN ({placeholders})
    """, (session_id, *canonical)).fetchall()
    sent = {row[0] for row in rows}
    return [original for original, key in zip(post_ids, canonical) if key not in sent]


def _mark_sent(conn: sqlite3.Connection, session_id: str, post_ids: list[str]) -> int:
    now = int(time.time())
    inserted = 0
    sql = """
        INSERT INTO deliveries(
            session_id,post_id,status,published_at,sort_key,attempts,created_at,updated_at,accepted_at
        ) VALUES(?,?,'accepted',?,?,0,?,?,?)
        ON CONFLICT(session_id,post_id) DO NOTHING
    """
    for post_id in post_ids:
        canonical = canonical_post_id(post_id)
        sort_key = f"{now:020d}:{canonical}"
        cur = conn.execute(sql, (session_id, canonical, now, sort_key, now, now, now))
        inserted += cur.rowcount
    return inserted


