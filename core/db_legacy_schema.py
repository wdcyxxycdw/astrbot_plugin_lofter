from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from .db_schema import SchemaValidationError
from .db_sql import canonical_sql, explicit_column_collations, extract_checks

NOW_DEFAULT = "strftime('%s','now')"
ColumnSpec = tuple[str, str, bool, Optional[str], int]


class SchemaVersionError(SchemaValidationError):
    pass


@dataclass(frozen=True)
class LegacyTableSpec:
    columns: tuple[ColumnSpec, ...]
    unique_keys: frozenset[tuple[str, ...]] = frozenset()
    checks: tuple[str, ...] = ()
    autoincrement: bool = False


CONFIG = LegacyTableSpec((
    ("key", "TEXT", False, None, 1),
    ("value", "TEXT", True, None, 0),
))
V1_SUBSCRIPTIONS = LegacyTableSpec(
    (
        ("id", "INTEGER", False, None, 1),
        ("session_id", "TEXT", True, None, 0),
        ("type", "TEXT", True, None, 0),
        ("target", "TEXT", True, None, 0),
        ("created_at", "INTEGER", True, NOW_DEFAULT, 0),
    ),
    frozenset({("session_id", "type", "target")}),
    autoincrement=True,
)
V1_SUBSCRIPTIONS_FILTER = LegacyTableSpec(
    V1_SUBSCRIPTIONS.columns[:4]
    + (("filter_rule", "TEXT", False, "NULL", 0), V1_SUBSCRIPTIONS.columns[-1]),
    V1_SUBSCRIPTIONS.unique_keys,
    autoincrement=True,
)
SUBSCRIPTIONS = LegacyTableSpec(
    (
        ("id", "INTEGER", False, None, 1),
        ("session_id", "TEXT", True, None, 0),
        ("type", "TEXT", True, None, 0),
        ("role", "TEXT", True, "'subscribe'", 0),
        ("target", "TEXT", True, None, 0),
        ("created_at", "INTEGER", True, NOW_DEFAULT, 0),
    ),
    frozenset({("session_id", "type", "role", "target")}),
    ("typein('tag','blog')", "rolein('subscribe','exclude')"),
    True,
)
V1_SEEN = LegacyTableSpec((
    ("subscription_id", "INTEGER", True, None, 1),
    ("post_id", "TEXT", True, None, 2),
    ("seen_at", "INTEGER", True, NOW_DEFAULT, 0),
))
SEEN = LegacyTableSpec((
    ("session_id", "TEXT", True, None, 1),
    ("type", "TEXT", True, None, 2),
    ("post_id", "TEXT", True, None, 3),
    ("seen_at", "INTEGER", True, NOW_DEFAULT, 0),
))
SENT = LegacyTableSpec((
    ("session_id", "TEXT", True, None, 1),
    ("post_id", "TEXT", True, None, 2),
    ("sent_at", "INTEGER", True, NOW_DEFAULT, 0),
))
COUNT_CONDITIONS = LegacyTableSpec((
    ("name", "TEXT", False, None, 1),
    ("expression", "TEXT", True, None, 0),
    ("updated_at", "INTEGER", True, NOW_DEFAULT, 0),
))
AUTHOR_BLOCKS = LegacyTableSpec(
    (
        ("session_id", "TEXT", True, None, 1),
        ("kind", "TEXT", True, None, 2),
        ("value", "TEXT", True, None, 3),
        ("display", "TEXT", True, None, 0),
        ("created_at", "INTEGER", True, NOW_DEFAULT, 0),
    ),
    checks=("kindin('name','username')",),
)


LEGACY_TABLES = {
    1: ({"config", "subscriptions", "seen_posts"}, {"sent_posts"}),
    2: ({"config", "subscriptions", "seen_posts", "sent_posts"}, set()),
    3: ({"config", "subscriptions", "seen_posts", "sent_posts", "count_conditions"}, set()),
    4: ({
        "config", "subscriptions", "seen_posts", "sent_posts",
        "count_conditions", "author_blocks",
    }, set()),
}
COMMON_SPECS = {
    "config": CONFIG,
    "subscriptions": SUBSCRIPTIONS,
    "seen_posts": SEEN,
    "sent_posts": SENT,
    "count_conditions": COUNT_CONDITIONS,
    "author_blocks": AUTHOR_BLOCKS,
}


def validate_legacy_schema(conn: sqlite3.Connection, version: int) -> None:
    if version not in LEGACY_TABLES:
        raise SchemaVersionError(f"unsupported legacy schema version: {version}")
    required, optional = LEGACY_TABLES[version]
    actual = _table_names(conn)
    if not required <= actual or actual - required - optional:
        raise SchemaVersionError(f"unknown v{version} table set: {sorted(actual)}")
    for table in actual:
        _validate_table(conn, table, _specs_for(version, table))


def _specs_for(version: int, table: str) -> tuple[LegacyTableSpec, ...]:
    if version == 1 and table == "subscriptions":
        return V1_SUBSCRIPTIONS, V1_SUBSCRIPTIONS_FILTER
    if version == 1 and table == "seen_posts":
        return (V1_SEEN,)
    return (COMMON_SPECS[table],)


def _validate_table(
    conn: sqlite3.Connection, table: str, alternatives: tuple[LegacyTableSpec, ...]
) -> None:
    failures = []
    for spec in alternatives:
        failure = _table_mismatch(conn, table, spec)
        if failure is None:
            return
        failures.append(failure)
    raise SchemaVersionError(f"malformed legacy {table}: {'; '.join(failures)}")


def _table_mismatch(
    conn: sqlite3.Connection, table: str, spec: LegacyTableSpec
) -> str | None:
    columns = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    actual = tuple(
        (row[1], row[2].upper(), bool(row[3]), _normalize_default(row[4]), row[5])
        for row in columns
    )
    expected = tuple(
        (name, kind, not_null, _normalize_default(default), pk)
        for name, kind, not_null, default, pk in spec.columns
    )
    if actual != expected:
        return f"columns expected={expected}, actual={actual}"
    unique_keys = _unique_keys(conn, table)
    expected_unique = _expected_unique_keys(spec)
    if unique_keys != expected_unique:
        return f"unique expected={expected_unique}, actual={unique_keys}"
    raw_sql = _raw_table_sql(conn, table)
    checks = Counter(extract_checks(raw_sql))
    expected_checks = Counter(spec.checks)
    if checks != expected_checks:
        return f"CHECK expected={expected_checks}, actual={checks}"
    collations = explicit_column_collations(raw_sql, {item[0] for item in spec.columns})
    if collations:
        return f"unexpected column collations={collations}"
    foreign_keys = conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
    if foreign_keys:
        return "unexpected foreign keys"
    if spec.autoincrement and "autoincrement" not in canonical_sql(raw_sql):
        return "missing AUTOINCREMENT"
    return None


def _unique_keys(conn: sqlite3.Connection, table: str) -> tuple[tuple, ...]:
    result = []
    for row in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
        if not row[2] or row[3] == "pk":
            continue
        columns = _index_columns(conn, row[1])
        result.append((tuple(item[0] for item in columns), row[3], bool(row[4]), columns))
    return tuple(sorted(result, key=repr))


def _index_columns(conn: sqlite3.Connection, name: str) -> tuple[tuple[str, bool, str], ...]:
    rows = conn.execute(f'PRAGMA index_xinfo("{name}")').fetchall()
    return tuple(
        (row[2], bool(row[3]), (row[4] or "BINARY").upper())
        for row in rows if row[5]
    )


def _expected_unique_keys(spec: LegacyTableSpec) -> tuple[tuple, ...]:
    expected = (
        (columns, "u", False, tuple((column, False, "BINARY") for column in columns))
        for columns in spec.unique_keys
    )
    return tuple(sorted(expected, key=repr))


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row[0] for row in rows}


def _raw_table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] or "" if row else ""


def _normalize_default(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"\s+", "", value.lower())
    while normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1]
    return normalized
