from __future__ import annotations

import re
import sqlite3
from collections import Counter

from .db_sql import contains_sql_keyword, explicit_column_collations, extract_checks

SCHEMA_VERSION = 5
NOW_DEFAULT = "strftime('%s','now')"

CREATE_STATEMENTS = (
    ("table:config", """
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY NOT NULL,
            value TEXT NOT NULL
        )
    """),
    ("table:subscriptions", """
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('tag','blog')),
            role TEXT NOT NULL DEFAULT 'subscribe'
                CHECK(role IN ('subscribe','exclude')),
            target TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('warming','active')),
            revision INTEGER NOT NULL CHECK(revision > 0),
            initialized_at INTEGER,
            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            UNIQUE(session_id, type, role, target)
        )
    """),
    ("table:subscription_revisions", """
        CREATE TABLE IF NOT EXISTS subscription_revisions (
            session_id TEXT NOT NULL,
            subscription_type TEXT NOT NULL
                CHECK(subscription_type IN ('tag','blog')),
            revision INTEGER NOT NULL CHECK(revision > 0),
            updated_at INTEGER NOT NULL,
            PRIMARY KEY(session_id, subscription_type)
        )
    """),
    ("table:session_policies", """
        CREATE TABLE IF NOT EXISTS session_policies (
            session_id TEXT PRIMARY KEY NOT NULL,
            policy_generation INTEGER NOT NULL CHECK(policy_generation > 0),
            updated_at INTEGER NOT NULL
        )
    """),
    ("table:session_activity", """
        CREATE TABLE IF NOT EXISTS session_activity (
            session_id TEXT PRIMARY KEY NOT NULL,
            inactive_since INTEGER,
            updated_at INTEGER NOT NULL
        )
    """),
    ("table:seen_posts", """
        CREATE TABLE IF NOT EXISTS seen_posts (
            subscription_id INTEGER NOT NULL
                REFERENCES subscriptions(id) ON DELETE CASCADE,
            post_id TEXT NOT NULL,
            published_at INTEGER NOT NULL,
            seen_at INTEGER NOT NULL,
            PRIMARY KEY(subscription_id, post_id)
        )
    """),
    ("table:subscription_watermarks", """
        CREATE TABLE IF NOT EXISTS subscription_watermarks (
            subscription_id INTEGER PRIMARY KEY
                REFERENCES subscriptions(id) ON DELETE CASCADE,
            history_before INTEGER NOT NULL,
            legacy_post_id_floor TEXT,
            updated_at INTEGER NOT NULL
        )
    """),
    ("table:legacy_checkpoints", """
        CREATE TABLE IF NOT EXISTS legacy_checkpoints (
            subscription_id INTEGER PRIMARY KEY
                REFERENCES subscriptions(id) ON DELETE CASCADE,
            post_id TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
    """),
    ("table:deliveries", """
        CREATE TABLE IF NOT EXISTS deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            post_id TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK(status IN ('pending','sending','accepted','dead','cancelled')),
            payload_json TEXT,
            published_at INTEGER NOT NULL,
            sort_key TEXT NOT NULL,
            lease_token TEXT,
            lease_until INTEGER,
            next_attempt_at INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
            last_error_type TEXT,
            last_error TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            accepted_at INTEGER,
            UNIQUE(session_id, post_id),
            CHECK(
                (status = 'sending' AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
                OR
                (status <> 'sending' AND lease_token IS NULL AND lease_until IS NULL)
            ),
            CHECK(status <> 'accepted' OR accepted_at IS NOT NULL)
        )
    """),
    ("table:delivery_sources", """
        CREATE TABLE IF NOT EXISTS delivery_sources (
            delivery_id INTEGER NOT NULL
                REFERENCES deliveries(id) ON DELETE CASCADE,
            subscription_id INTEGER NOT NULL
                REFERENCES subscriptions(id) ON DELETE CASCADE,
            subscription_revision INTEGER NOT NULL CHECK(subscription_revision > 0),
            policy_generation INTEGER NOT NULL CHECK(policy_generation > 0),
            discovered_at INTEGER NOT NULL,
            PRIMARY KEY(delivery_id, subscription_id)
        )
    """),
    ("table:count_conditions", """
        CREATE TABLE IF NOT EXISTS count_conditions (
            name TEXT PRIMARY KEY NOT NULL,
            expression TEXT NOT NULL,
            updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        )
    """),
    ("table:author_blocks", """
        CREATE TABLE IF NOT EXISTS author_blocks (
            session_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('name','username')),
            value TEXT NOT NULL,
            display TEXT NOT NULL,
            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            PRIMARY KEY(session_id, kind, value)
        )
    """),
    ("index:subscriptions_session", """
        CREATE INDEX IF NOT EXISTS idx_subscriptions_session_type_state
        ON subscriptions(session_id, type, state, role)
    """),
    ("index:session_activity", """
        CREATE INDEX IF NOT EXISTS idx_session_activity_inactive_since
        ON session_activity(inactive_since)
    """),
    ("index:seen_posts_seen_at", """
        CREATE INDEX IF NOT EXISTS idx_seen_posts_seen_at ON seen_posts(seen_at)
    """),
    ("index:deliveries_session", """
        CREATE INDEX IF NOT EXISTS idx_deliveries_session_status_due_sort
        ON deliveries(session_id, status, next_attempt_at, sort_key)
    """),
    ("index:deliveries_lease", """
        CREATE INDEX IF NOT EXISTS idx_deliveries_status_lease_until
        ON deliveries(status, lease_until)
    """),
    ("index:deliveries_accepted", """
        CREATE INDEX IF NOT EXISTS idx_deliveries_accepted_at ON deliveries(accepted_at)
    """),
    ("index:delivery_sources_subscription", """
        CREATE INDEX IF NOT EXISTS idx_delivery_sources_subscription_id
        ON delivery_sources(subscription_id)
    """),
)


TABLE_COLUMNS = {
    "config": (("key", "TEXT", True, None, 1), ("value", "TEXT", True, None, 0)),
    "subscriptions": (
        ("id", "INTEGER", False, None, 1), ("session_id", "TEXT", True, None, 0),
        ("type", "TEXT", True, None, 0), ("role", "TEXT", True, "'subscribe'", 0),
        ("target", "TEXT", True, None, 0), ("state", "TEXT", True, None, 0),
        ("revision", "INTEGER", True, None, 0), ("initialized_at", "INTEGER", False, None, 0),
        ("created_at", "INTEGER", True, NOW_DEFAULT, 0),
        ("updated_at", "INTEGER", True, NOW_DEFAULT, 0),
    ),
    "subscription_revisions": (
        ("session_id", "TEXT", True, None, 1), ("subscription_type", "TEXT", True, None, 2),
        ("revision", "INTEGER", True, None, 0), ("updated_at", "INTEGER", True, None, 0),
    ),
    "session_policies": (
        ("session_id", "TEXT", True, None, 1), ("policy_generation", "INTEGER", True, None, 0),
        ("updated_at", "INTEGER", True, None, 0),
    ),
    "session_activity": (
        ("session_id", "TEXT", True, None, 1), ("inactive_since", "INTEGER", False, None, 0),
        ("updated_at", "INTEGER", True, None, 0),
    ),
    "seen_posts": (
        ("subscription_id", "INTEGER", True, None, 1), ("post_id", "TEXT", True, None, 2),
        ("published_at", "INTEGER", True, None, 0), ("seen_at", "INTEGER", True, None, 0),
    ),
    "subscription_watermarks": (
        ("subscription_id", "INTEGER", False, None, 1),
        ("history_before", "INTEGER", True, None, 0),
        ("legacy_post_id_floor", "TEXT", False, None, 0),
        ("updated_at", "INTEGER", True, None, 0),
    ),
    "legacy_checkpoints": (
        ("subscription_id", "INTEGER", False, None, 1),
        ("post_id", "TEXT", True, None, 0), ("created_at", "INTEGER", True, None, 0),
    ),
    "deliveries": (
        ("id", "INTEGER", False, None, 1), ("session_id", "TEXT", True, None, 0),
        ("post_id", "TEXT", True, None, 0), ("status", "TEXT", True, None, 0),
        ("payload_json", "TEXT", False, None, 0), ("published_at", "INTEGER", True, None, 0),
        ("sort_key", "TEXT", True, None, 0), ("lease_token", "TEXT", False, None, 0),
        ("lease_until", "INTEGER", False, None, 0), ("next_attempt_at", "INTEGER", False, None, 0),
        ("attempts", "INTEGER", True, "0", 0), ("last_error_type", "TEXT", False, None, 0),
        ("last_error", "TEXT", False, None, 0), ("created_at", "INTEGER", True, None, 0),
        ("updated_at", "INTEGER", True, None, 0), ("accepted_at", "INTEGER", False, None, 0),
    ),
    "delivery_sources": (
        ("delivery_id", "INTEGER", True, None, 1), ("subscription_id", "INTEGER", True, None, 2),
        ("subscription_revision", "INTEGER", True, None, 0),
        ("policy_generation", "INTEGER", True, None, 0), ("discovered_at", "INTEGER", True, None, 0),
    ),
    "count_conditions": (
        ("name", "TEXT", True, None, 1), ("expression", "TEXT", True, None, 0),
        ("updated_at", "INTEGER", True, NOW_DEFAULT, 0),
    ),
    "author_blocks": (
        ("session_id", "TEXT", True, None, 1), ("kind", "TEXT", True, None, 2),
        ("value", "TEXT", True, None, 3), ("display", "TEXT", True, None, 0),
        ("created_at", "INTEGER", True, NOW_DEFAULT, 0),
    ),
}

UNIQUE_KEYS = {
    "subscriptions": (("session_id", "type", "role", "target"),),
    "deliveries": (("session_id", "post_id"),),
}
INDEXES = {
    "idx_subscriptions_session_type_state": (
        "subscriptions", ("session_id", "type", "state", "role")
    ),
    "idx_session_activity_inactive_since": ("session_activity", ("inactive_since",)),
    "idx_seen_posts_seen_at": ("seen_posts", ("seen_at",)),
    "idx_deliveries_session_status_due_sort": (
        "deliveries", ("session_id", "status", "next_attempt_at", "sort_key")
    ),
    "idx_deliveries_status_lease_until": ("deliveries", ("status", "lease_until")),
    "idx_deliveries_accepted_at": ("deliveries", ("accepted_at",)),
    "idx_delivery_sources_subscription_id": ("delivery_sources", ("subscription_id",)),
}
FOREIGN_KEYS = {
    "seen_posts": {("subscriptions", "subscription_id", "id", "NO ACTION", "CASCADE", "NONE")},
    "subscription_watermarks": {
        ("subscriptions", "subscription_id", "id", "NO ACTION", "CASCADE", "NONE")
    },
    "legacy_checkpoints": {
        ("subscriptions", "subscription_id", "id", "NO ACTION", "CASCADE", "NONE")
    },
    "delivery_sources": {
        ("deliveries", "delivery_id", "id", "NO ACTION", "CASCADE", "NONE"),
        ("subscriptions", "subscription_id", "id", "NO ACTION", "CASCADE", "NONE"),
    },
}
CHECK_EXPRESSIONS = {
    "subscriptions": (
        "typein('tag','blog')", "rolein('subscribe','exclude')",
        "statein('warming','active')", "revision>0",
    ),
    "subscription_revisions": ("subscription_typein('tag','blog')", "revision>0"),
    "session_policies": ("policy_generation>0",),
    "deliveries": (
        "statusin('pending','sending','accepted','dead','cancelled')", "attempts>=0",
        "(status='sending'andlease_tokenisnotnullandlease_untilisnotnull)or"
        "(status<>'sending'andlease_tokenisnullandlease_untilisnull)",
        "status<>'accepted'oraccepted_atisnotnull",
    ),
    "delivery_sources": ("subscription_revision>0", "policy_generation>0"),
    "author_blocks": ("kindin('name','username')",),
}


class SchemaValidationError(RuntimeError):
    pass


def create_schema(conn: sqlite3.Connection, step=None) -> None:
    for name, sql in CREATE_STATEMENTS:
        if step:
            step(name, conn)
        conn.execute(sql)


def validate_schema(conn: sqlite3.Connection) -> None:
    _validate_table_set(conn)
    for table, specs in TABLE_COLUMNS.items():
        _validate_columns(conn, table, specs)
        _validate_checks(conn, table)
        _validate_foreign_keys(conn, table)
        _validate_unique_keys(conn, table)
        _validate_primary_key_index(conn, table, specs)
    _validate_indexes(conn)
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise SchemaValidationError(f"foreign_key_check failed: {violations[:5]}")


def _validate_table_set(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    actual = {row[0] for row in rows}
    expected = set(TABLE_COLUMNS)
    if actual != expected:
        raise SchemaValidationError(f"table mismatch: expected={sorted(expected)}, actual={sorted(actual)}")


def _normalize_default(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"\s+", "", value.lower())
    while normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1]
    return normalized


def _validate_columns(conn: sqlite3.Connection, table: str, specs: tuple) -> None:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    actual = [(r[1], r[2].upper(), bool(r[3]), _normalize_default(r[4]), r[5]) for r in rows]
    expected = [(n, t, nn, _normalize_default(d), pk) for n, t, nn, d, pk in specs]
    if actual != expected:
        raise SchemaValidationError(f"column mismatch for {table}: expected={expected}, actual={actual}")
    sql = _table_sql(conn, table)
    collations = explicit_column_collations(sql, {spec[0] for spec in specs})
    if collations:
        raise SchemaValidationError(f"column collation mismatch for {table}: {collations}")


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] or "" if row else ""


def _validate_checks(conn: sqlite3.Connection, table: str) -> None:
    actual = Counter(extract_checks(_table_sql(conn, table)))
    expected = Counter(CHECK_EXPRESSIONS.get(table, ()))
    if actual != expected:
        raise SchemaValidationError(
            f"CHECK mismatch for {table}: expected={list(expected.elements())}, "
            f"actual={list(actual.elements())}"
        )


def _validate_foreign_keys(conn: sqlite3.Connection, table: str) -> None:
    rows = conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
    actual = {
        (r[2], r[3], r[4], r[5].upper(), r[6].upper(), r[7].upper()) for r in rows
    }
    expected = FOREIGN_KEYS.get(table, set())
    sql = _table_sql(conn, table)
    forbidden = ("match", "deferrable", "initially")
    if contains_sql_keyword(sql, *forbidden):
        raise SchemaValidationError(f"foreign key DDL mismatch for {table}")
    if actual != expected:
        raise SchemaValidationError(
            f"foreign key mismatch for {table}: expected={expected}, actual={actual}"
        )


def _index_columns(conn: sqlite3.Connection, name: str) -> tuple[tuple[str, bool, str], ...]:
    rows = conn.execute(f'PRAGMA index_xinfo("{name}")').fetchall()
    return tuple(
        (row[2], bool(row[3]), (row[4] or "BINARY").upper())
        for row in rows if row[5]
    )


def _unique_keys(conn: sqlite3.Connection, table: str) -> tuple[tuple, ...]:
    result = []
    for row in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
        if not row[2] or row[3] == "pk":
            continue
        result.append((tuple(column[0] for column in _index_columns(conn, row[1])), row[3], bool(row[4]), _index_columns(conn, row[1])))
    return tuple(sorted(result, key=repr))


def _validate_unique_keys(conn: sqlite3.Connection, table: str) -> None:
    expected = tuple(sorted((
        (columns, "u", False, tuple((column, False, "BINARY") for column in columns))
        for columns in UNIQUE_KEYS.get(table, ())
    ), key=repr))
    actual = _unique_keys(conn, table)
    if actual != expected:
        raise SchemaValidationError(
            f"unique mismatch for {table}: expected={expected}, actual={actual}"
        )


def _validate_primary_key_index(conn: sqlite3.Connection, table: str, specs: tuple) -> None:
    expected_columns = tuple(
        name for name, _, _, _, pk_order in sorted(specs, key=lambda item: item[4]) if pk_order
    )
    if len(expected_columns) == 1:
        column = next(item for item in specs if item[0] == expected_columns[0])
        if column[1] == "INTEGER":
            return
    rows = conn.execute(f'PRAGMA index_list("{table}")').fetchall()
    pk_rows = [row for row in rows if row[3] == "pk"]
    expected = tuple((column, False, "BINARY") for column in expected_columns)
    actual = _index_columns(conn, pk_rows[0][1]) if len(pk_rows) == 1 else ()
    if actual != expected or (pk_rows and bool(pk_rows[0][4])):
        raise SchemaValidationError(
            f"primary key mismatch for {table}: expected={expected}, actual={actual}"
        )


def _validate_indexes(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
    ).fetchall()
    actual_names = {row[0] for row in rows}
    if actual_names != set(INDEXES):
        raise SchemaValidationError(
            f"index set mismatch: expected={sorted(INDEXES)}, actual={sorted(actual_names)}"
        )
    for name, (table, columns) in INDEXES.items():
        row = conn.execute(
            "SELECT tbl_name,sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
        ).fetchone()
        if not row or row[0] != table:
            raise SchemaValidationError(f"missing or misplaced index: {name}")
        metadata = next(
            (item for item in conn.execute(f'PRAGMA index_list("{table}")') if item[1] == name),
            None,
        )
        expected_columns = tuple((column, False, "BINARY") for column in columns)
        if metadata is None or (bool(metadata[2]), metadata[3], bool(metadata[4])) != (False, "c", False):
            raise SchemaValidationError(f"index metadata mismatch for {name}: {metadata}")
        actual_columns = _index_columns(conn, name)
        if actual_columns != expected_columns:
            raise SchemaValidationError(
                f"index mismatch for {name}: expected={expected_columns}, actual={actual_columns}"
            )
