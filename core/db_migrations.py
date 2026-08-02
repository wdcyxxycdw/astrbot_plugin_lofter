from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable

from .db_legacy_schema import SchemaVersionError, validate_legacy_schema
from .db_schema import SCHEMA_VERSION, SchemaValidationError, create_schema, validate_schema
from .post_identity import canonical_post_id

FaultHook = Callable[[str, sqlite3.Connection], None]


def initialize_schema(conn: sqlite3.Connection, fault_hook: FaultHook | None = None) -> int:
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = _initialize_in_transaction(conn, fault_hook)
        _stage(fault_hook, "commit", conn)
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise SchemaValidationError("failed to enable foreign keys")
    return version


def _initialize_in_transaction(conn: sqlite3.Connection, fault_hook: FaultHook | None) -> int:
    tables = _table_names(conn)
    if not tables:
        create_schema(conn, lambda name, c: _stage(fault_hook, name, c))
        _write_marker(conn, fault_hook)
    else:
        version = get_schema_version(conn)
        if version > SCHEMA_VERSION:
            raise SchemaVersionError(f"unsupported future schema version: {version}")
        if version == SCHEMA_VERSION:
            _stage(fault_hook, "validate", conn)
            validate_schema(conn)
            return version
        validate_legacy_schema(conn, version)
        _migrate_legacy(conn, version, fault_hook)
        _write_marker(conn, fault_hook)
    _stage(fault_hook, "validate", conn)
    validate_schema(conn)
    return SCHEMA_VERSION


def get_schema_version(conn: sqlite3.Connection) -> int:
    if "config" not in _table_names(conn):
        raise SchemaVersionError("existing database has no config table")
    row = conn.execute("SELECT value FROM config WHERE key='schema_version'").fetchone()
    if row is None:
        return 1
    value = row[0]
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value):
        raise SchemaVersionError(f"malformed schema version marker: {value!r}")
    return int(value)


def _migrate_legacy(conn: sqlite3.Connection, version: int, fault_hook: FaultHook | None) -> None:
    old_tables = sorted(_table_names(conn))
    for table in old_tables:
        _execute(conn, fault_hook, f"rename:{table}", f'ALTER TABLE "{table}" RENAME TO "_v5_old_{table}"')
    create_schema(conn, lambda name, c: _stage(fault_hook, name, c))
    now = int(time.time())
    _copy_config(conn, fault_hook)
    _copy_subscriptions(conn, version, now, fault_hook)
    _copy_seen(conn, version, fault_hook)
    _copy_sent(conn, version, fault_hook)
    _copy_optional_tables(conn, version, fault_hook)
    _initialize_metadata(conn, now, fault_hook)
    for table in _drop_order(old_tables):
        _execute(conn, fault_hook, f"drop:{table}", f'DROP TABLE "_v5_old_{table}"')


def _copy_config(conn: sqlite3.Connection, fault_hook: FaultHook | None) -> None:
    rows = conn.execute("SELECT key,value FROM _v5_old_config WHERE key<>'schema_version'").fetchall()
    sql = "INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING"
    for index, (key, value) in enumerate(rows):
        _execute(conn, fault_hook, f"copy:config:{index}", sql, (key, value))


def _copy_subscriptions(conn: sqlite3.Connection, version: int, now: int, fault_hook: FaultHook | None) -> None:
    if version == 1:
        rows = _v1_subscription_rows(conn)
        for index, row in enumerate(rows):
            _insert_subscription(conn, row[:5], f"copy:subscription:{index}", fault_hook)
        for index, row in enumerate(rows):
            _copy_filter_rule(
                conn, row[1], row[2], row[5], row[4],
                f"copy:filter-subscription:{index}", fault_hook,
            )
        return
    rows = conn.execute(
        "SELECT id,session_id,type,role,target,created_at FROM _v5_old_subscriptions ORDER BY id"
    ).fetchall()
    for index, (sub_id, session_id, sub_type, role, target, created_at) in enumerate(rows):
        _insert_subscription(
            conn, (sub_id, session_id, sub_type, role, target, created_at),
            f"copy:subscription:{index}", fault_hook,
        )


def _v1_subscription_rows(conn: sqlite3.Connection) -> list[tuple]:
    cols = _column_names(conn, "_v5_old_subscriptions")
    filter_expr = "filter_rule" if "filter_rule" in cols else "NULL"
    return conn.execute(
        f"SELECT id,session_id,type,target,created_at,{filter_expr} FROM _v5_old_subscriptions ORDER BY id"
    ).fetchall()


def _insert_subscription(
    conn: sqlite3.Connection, row: tuple, stage: str, fault_hook: FaultHook | None
) -> None:
    if len(row) == 5:
        sub_id, session_id, sub_type, target, created_at = row
        role = "subscribe"
    else:
        sub_id, session_id, sub_type, role, target, created_at = row
    sql = """
        INSERT INTO subscriptions(
            id,session_id,type,role,target,state,revision,initialized_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,'active',1,?,?,?)
        ON CONFLICT(session_id,type,role,target) DO NOTHING
    """
    _execute(conn, fault_hook, stage, sql, (
        sub_id, session_id, sub_type, role, target, created_at, created_at, created_at
    ))


def _copy_filter_rule(
    conn: sqlite3.Connection, session_id: str, sub_type: str,
    raw: str | None, now: int, stage_prefix: str,
    fault_hook: FaultHook | None,
) -> None:
    if raw is None:
        return
    data = _parse_filter_rule(raw)
    pairs = [
        ("subscribe", data.get("search_tags", [])[1:]),
        ("subscribe", data.get("include_tags", [])),
    ]
    pairs.extend(("subscribe", group) for group in data.get("or_tag_groups", []))
    pairs.append(("exclude", data.get("exclude_tags", [])))
    generated_index = 0
    for role, tags in pairs:
        for target in tags:
            stage = f"{stage_prefix}:{generated_index}"
            _insert_generated_subscription(
                conn, session_id, sub_type, role, target, now, stage, fault_hook
            )
            generated_index += 1


def _parse_filter_rule(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SchemaVersionError("invalid v1 filter_rule JSON") from exc
    if not isinstance(data, dict):
        raise SchemaVersionError("v1 filter_rule root must be an object")
    for field in ("search_tags", "include_tags", "exclude_tags"):
        _validate_tag_list(data.get(field, []), field)
    groups = data.get("or_tag_groups", [])
    if not isinstance(groups, list):
        raise SchemaVersionError("v1 filter_rule or_tag_groups must be a list")
    for index, group in enumerate(groups):
        _validate_tag_list(group, f"or_tag_groups[{index}]")
    return data


def _validate_tag_list(value: object, field: str) -> None:
    if not isinstance(value, list):
        raise SchemaVersionError(f"v1 filter_rule {field} must be a list")
    if any(type(tag) is not str or not tag.strip() for tag in value):
        raise SchemaVersionError(f"v1 filter_rule {field} contains invalid tag")


def _insert_generated_subscription(
    conn: sqlite3.Connection, session_id: str, sub_type: str, role: str,
    target: str, now: int, stage: str, fault_hook: FaultHook | None,
) -> bool:
    sql = """
        INSERT INTO subscriptions(
            session_id,type,role,target,state,revision,initialized_at,created_at,updated_at
        ) VALUES(?,?,?,?,'active',1,?,?,?)
        ON CONFLICT(session_id,type,role,target) DO NOTHING
    """
    cur = _execute(conn, fault_hook, stage, sql, (
        session_id, sub_type, role, target, now, now, now
    ))
    return cur.rowcount > 0


def _copy_seen(conn: sqlite3.Connection, version: int, fault_hook: FaultHook | None) -> None:
    if version == 1:
        rows = conn.execute("SELECT subscription_id,post_id,seen_at FROM _v5_old_seen_posts").fetchall()
        for old_sub_id, post_id, seen_at in rows:
            source = conn.execute(
                "SELECT session_id,type FROM _v5_old_subscriptions WHERE id=?", (old_sub_id,)
            ).fetchone()
            if source is None:
                continue
            sub_rows = conn.execute(
                "SELECT id FROM subscriptions WHERE session_id=? AND type=? AND role='subscribe'",
                source,
            ).fetchall()
            for sub_index, (sub_id,) in enumerate(sub_rows):
                stage = f"copy:seen:{old_sub_id}:{sub_index}"
                _insert_seen(conn, sub_id, post_id, seen_at, stage, fault_hook)
        return
    rows = conn.execute("SELECT session_id,type,post_id,seen_at FROM _v5_old_seen_posts").fetchall()
    for seen_index, (session_id, sub_type, post_id, seen_at) in enumerate(rows):
        sub_rows = conn.execute(
            "SELECT id FROM subscriptions WHERE session_id=? AND type=? AND role='subscribe'",
            (session_id, sub_type),
        ).fetchall()
        for sub_index, (sub_id,) in enumerate(sub_rows):
            stage = f"copy:seen:{seen_index}:{sub_index}"
            _insert_seen(conn, sub_id, post_id, seen_at, stage, fault_hook)


def _insert_seen(
    conn: sqlite3.Connection, sub_id: int, post_id: str,
    seen_at: int, stage: str, fault_hook: FaultHook | None,
) -> None:
    sql = """
        INSERT INTO seen_posts(subscription_id,post_id,published_at,seen_at) VALUES(?,?,?,?)
        ON CONFLICT(subscription_id,post_id) DO UPDATE SET
            published_at=MAX(published_at,excluded.published_at),
            seen_at=MAX(seen_at,excluded.seen_at)
    """
    canonical = canonical_post_id(post_id)
    _execute(conn, fault_hook, stage, sql, (sub_id, canonical, seen_at, seen_at))


def _copy_sent(conn: sqlite3.Connection, version: int, fault_hook: FaultHook | None) -> None:
    if "sent_posts" not in _legacy_table_names(conn):
        return
    rows = conn.execute("SELECT session_id,post_id,sent_at FROM _v5_old_sent_posts").fetchall()
    sql = """
        INSERT INTO deliveries(
            session_id,post_id,status,payload_json,published_at,sort_key,attempts,
            created_at,updated_at,accepted_at
        ) VALUES(?,?,'accepted',NULL,?,?,0,?,?,?)
        ON CONFLICT(session_id,post_id) DO UPDATE SET
            published_at=MAX(published_at,excluded.published_at),
            updated_at=MAX(updated_at,excluded.updated_at),
            accepted_at=MIN(accepted_at,excluded.accepted_at)
    """
    for index, (session_id, post_id, sent_at) in enumerate(rows):
        canonical = canonical_post_id(post_id)
        sort_key = f"{int(sent_at):020d}:{canonical}"
        _execute(conn, fault_hook, f"copy:sent:{index}", sql, (
            session_id, canonical, sent_at, sort_key, sent_at, sent_at, sent_at
        ))


def _copy_optional_tables(conn: sqlite3.Connection, version: int, fault_hook: FaultHook | None) -> None:
    if version >= 3:
        rows = conn.execute("SELECT name,expression,updated_at FROM _v5_old_count_conditions").fetchall()
        sql = "INSERT INTO count_conditions(name,expression,updated_at) VALUES(?,?,?) ON CONFLICT(name) DO NOTHING"
        for index, row in enumerate(rows):
            _execute(conn, fault_hook, f"copy:count-condition:{index}", sql, row)
    if version >= 4:
        rows = conn.execute("SELECT session_id,kind,value,display,created_at FROM _v5_old_author_blocks").fetchall()
        sql = """
            INSERT INTO author_blocks(session_id,kind,value,display,created_at) VALUES(?,?,?,?,?)
            ON CONFLICT(session_id,kind,value) DO NOTHING
        """
        for index, row in enumerate(rows):
            _execute(conn, fault_hook, f"copy:author-block:{index}", sql, row)


def _initialize_metadata(conn: sqlite3.Connection, now: int, fault_hook: FaultHook | None) -> None:
    _initialize_revisions(conn, fault_hook)
    sessions = _all_session_ids(conn)
    for index, session_id in enumerate(sessions):
        has_subscribe = conn.execute(
            "SELECT 1 FROM subscriptions WHERE session_id=? AND role='subscribe' LIMIT 1", (session_id,)
        ).fetchone()
        inactive_since = None if has_subscribe else now
        _execute(conn, fault_hook, f"init:policy:{index}", """
            INSERT INTO session_policies(session_id,policy_generation,updated_at) VALUES(?,1,?)
            ON CONFLICT(session_id) DO NOTHING
        """, (session_id, now))
        _execute(conn, fault_hook, f"init:activity:{index}", """
            INSERT INTO session_activity(session_id,inactive_since,updated_at) VALUES(?,?,?)
            ON CONFLICT(session_id) DO NOTHING
        """, (session_id, inactive_since, now))
    rows = conn.execute("SELECT id,COALESCE(initialized_at,created_at) FROM subscriptions").fetchall()
    for index, (sub_id, initialized_at) in enumerate(rows):
        _execute(conn, fault_hook, f"init:watermark:{index}", """
            INSERT INTO subscription_watermarks(
                subscription_id,history_before,legacy_post_id_floor,updated_at
            ) VALUES(?,?,NULL,?) ON CONFLICT(subscription_id) DO NOTHING
        """, (sub_id, initialized_at, now))


def _initialize_revisions(conn: sqlite3.Connection, fault_hook: FaultHook | None) -> None:
    rows = conn.execute(
        "SELECT session_id,type,MAX(revision),MAX(updated_at) FROM subscriptions GROUP BY session_id,type"
    ).fetchall()
    sql = """
        INSERT INTO subscription_revisions(session_id,subscription_type,revision,updated_at)
        VALUES(?,?,?,?) ON CONFLICT(session_id,subscription_type) DO NOTHING
    """
    for index, row in enumerate(rows):
        _execute(conn, fault_hook, f"init:revision:{index}", sql, row)


def _all_session_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("""
        SELECT session_id FROM subscriptions
        UNION SELECT session_id FROM deliveries
        UNION SELECT session_id FROM author_blocks
    """).fetchall()
    return [row[0] for row in rows]


def _write_marker(conn: sqlite3.Connection, fault_hook: FaultHook | None) -> None:
    _stage(fault_hook, "marker", conn)
    conn.execute("""
        INSERT INTO config(key,value) VALUES('schema_version',?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (str(SCHEMA_VERSION),))


def _execute(
    conn: sqlite3.Connection, fault_hook: FaultHook | None,
    name: str, sql: str, params: tuple = (),
) -> sqlite3.Cursor:
    _stage(fault_hook, name, conn)
    return conn.execute(sql, params)


def _stage(fault_hook: FaultHook | None, name: str, conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise RuntimeError(f"migration stage outside transaction: {name}")
    if fault_hook:
        fault_hook(name, conn)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    return {row[0] for row in rows}


def _legacy_table_names(conn: sqlite3.Connection) -> set[str]:
    return {name.removeprefix("_v5_old_") for name in _table_names(conn) if name.startswith("_v5_old_")}


def _column_names(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall())


def _drop_order(tables: list[str]) -> list[str]:
    priority = {"seen_posts": 0, "sent_posts": 1, "subscriptions": 2, "config": 9}
    return sorted(tables, key=lambda table: priority.get(table, 5))
