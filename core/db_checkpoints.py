from __future__ import annotations

import re
import sqlite3
import time

from .post_identity import canonical_post_id


def consume_legacy_checkpoints(
    conn: sqlite3.Connection,
    session_id: str,
    sub_type: str,
    post_ids: list[str],
    subscription_target: str | None,
) -> list[str] | None:
    if not post_ids:
        return None
    subscription_ids = _subscription_ids(
        conn, session_id, sub_type, subscription_target
    )
    if not subscription_ids:
        return None
    placeholders = ",".join("?" for _ in subscription_ids)
    rows = conn.execute(
        f"SELECT subscription_id,post_id FROM legacy_checkpoints "
        f"WHERE subscription_id IN ({placeholders})",
        subscription_ids,
    ).fetchall()
    if not rows:
        return None
    eligible, suppressed = _partition_by_checkpoint(
        [row[1] for row in rows], post_ids
    )
    for sub_id, checkpoint in rows:
        update_checkpoint_watermark(conn, sub_id, checkpoint)
    _mark_seen_ids(conn, subscription_ids, suppressed)
    conn.execute(
        f"DELETE FROM legacy_checkpoints WHERE subscription_id IN ({placeholders})",
        subscription_ids,
    )
    return eligible


def filter_legacy_floor(
    conn: sqlite3.Connection,
    session_id: str,
    sub_type: str,
    post_ids: list[str],
    subscription_target: str | None,
) -> tuple[list[str], list[str]]:
    params: list[object] = [session_id, sub_type]
    target_sql = ""
    if subscription_target is not None:
        target_sql = " AND s.target=?"
        params.append(subscription_target)
    rows = conn.execute(f"""
        SELECT w.legacy_post_id_floor FROM subscription_watermarks w
        JOIN subscriptions s ON s.id=w.subscription_id
        WHERE s.session_id=? AND s.type=? AND s.role='subscribe' AND s.state='active'
          AND w.legacy_post_id_floor IS NOT NULL{target_sql}
    """, params).fetchall()
    if not rows:
        return post_ids, []
    return _partition_by_checkpoint([row[0] for row in rows], post_ids)


def update_checkpoint_watermark(
    conn: sqlite3.Connection, subscription_id: int, post_id: str
) -> None:
    canonical = canonical_post_id(post_id)
    if _numeric_post_order(canonical) is None:
        return
    conn.execute("""
        UPDATE subscription_watermarks SET legacy_post_id_floor=?,updated_at=?
        WHERE subscription_id=?
    """, (canonical, int(time.time()), subscription_id))


def _subscription_ids(
    conn: sqlite3.Connection,
    session_id: str,
    sub_type: str,
    subscription_target: str | None,
) -> list[int]:
    params: list[object] = [session_id, sub_type]
    target_sql = ""
    if subscription_target is not None:
        target_sql = " AND target=?"
        params.append(subscription_target)
    return [row[0] for row in conn.execute(f"""
        SELECT id FROM subscriptions
        WHERE session_id=? AND type=? AND role='subscribe' AND state='active'{target_sql}
    """, params).fetchall()]


def _partition_by_checkpoint(
    checkpoints: list[str], post_ids: list[str]
) -> tuple[list[str], list[str]]:
    canonical = [canonical_post_id(post_id) for post_id in post_ids]
    checkpoint_values = [_numeric_post_order(value) for value in checkpoints]
    post_values = [_numeric_post_order(value) for value in canonical]
    all_values = checkpoint_values + post_values
    if any(value is None for value in all_values):
        return [], post_ids
    comparable = [value for value in all_values if value is not None]
    if len({len(value) for value in comparable}) != 1:
        return [], post_ids
    floor = max(value for value in checkpoint_values if value is not None)
    eligible = [
        original for original, value in zip(post_ids, post_values)
        if value is not None and value > floor
    ]
    eligible_set = set(eligible)
    return eligible, [post_id for post_id in post_ids if post_id not in eligible_set]


def _numeric_post_order(post_id: str) -> tuple[int, ...] | None:
    if post_id.isdigit():
        return (int(post_id),)
    parts = post_id.split("_")
    if len(parts) != 2:
        return None
    if not all(part and re.fullmatch(r"[0-9a-f]+", part) for part in parts):
        return None
    return tuple(int(part, 16) for part in parts)


def _mark_seen_ids(
    conn: sqlite3.Connection, subscription_ids: list[int], post_ids: list[str]
) -> None:
    if not post_ids:
        return
    now = int(time.time())
    rows = [
        (sub_id, canonical_post_id(post_id), now, now)
        for sub_id in subscription_ids for post_id in post_ids
    ]
    conn.executemany("""
        INSERT INTO seen_posts(subscription_id,post_id,published_at,seen_at) VALUES(?,?,?,?)
        ON CONFLICT(subscription_id,post_id) DO NOTHING
    """, rows)
