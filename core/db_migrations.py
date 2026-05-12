import json
import sqlite3

SCHEMA_VERSION = 3

DDL = """
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

CREATE TABLE IF NOT EXISTS count_conditions (
    name       TEXT PRIMARY KEY,
    expression TEXT NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
"""


def get_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM config WHERE key='schema_version'").fetchone()
    return int(row[0]) if row else 1


def migrate(conn: sqlite3.Connection, from_ver: int):
    if from_ver < 2:
        _migrate_v1_to_v2(conn)
    if from_ver < 3:
        _migrate_v2_to_v3(conn)
    conn.execute(
        "INSERT INTO config(key,value) VALUES('schema_version',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _migrate_v2_to_v3(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS count_conditions (
            name       TEXT PRIMARY KEY,
            expression TEXT NOT NULL,
            updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        );
    """)


def _migrate_v1_to_v2(conn: sqlite3.Connection):
    tables = _table_names(conn)
    if "subscriptions" not in tables:
        return

    cols = _column_names(conn, "subscriptions")
    if "role" in cols:
        return

    conn.execute("ALTER TABLE subscriptions RENAME TO subscriptions_old")
    _create_subscriptions(conn)

    rows = _old_subscription_rows(conn, cols)
    id_map: dict[int, tuple[str, str]] = {}
    for old_id, session_id, sub_type, target, filter_rule_json in rows:
        id_map[old_id] = (session_id, sub_type)
        _insert_sub(conn, session_id, sub_type, "subscribe", target)
        if filter_rule_json:
            _migrate_filter_rule(conn, session_id, sub_type, filter_rule_json)

    _migrate_seen_posts_v2(conn, id_map)
    conn.execute("DROP TABLE subscriptions_old")


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _create_subscriptions(conn: sqlite3.Connection):
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


def _old_subscription_rows(conn: sqlite3.Connection, cols: set[str]) -> list[tuple]:
    if "filter_rule" in cols:
        return conn.execute(
            "SELECT id,session_id,type,target,filter_rule FROM subscriptions_old"
        ).fetchall()
    return conn.execute(
        "SELECT id,session_id,type,target,NULL FROM subscriptions_old"
    ).fetchall()


def _migrate_filter_rule(conn: sqlite3.Connection, session_id: str, sub_type: str, filter_rule_json: str):
    try:
        data = json.loads(filter_rule_json)
    except Exception:
        return

    _insert_filter_tags(conn, session_id, sub_type, "subscribe", data.get("search_tags", [])[1:])
    _insert_filter_tags(conn, session_id, sub_type, "subscribe", data.get("include_tags", []))
    for group in data.get("or_tag_groups", []):
        _insert_filter_tags(conn, session_id, sub_type, "subscribe", group)
    _insert_filter_tags(conn, session_id, sub_type, "exclude", data.get("exclude_tags", []))


def _insert_filter_tags(conn: sqlite3.Connection, session_id: str, sub_type: str, role: str, tags: list[str]):
    for tag in tags:
        if tag:
            _insert_sub(conn, session_id, sub_type, role, tag)


def _insert_sub(conn: sqlite3.Connection, session_id: str, sub_type: str, role: str, target: str):
    try:
        conn.execute(
            "INSERT OR IGNORE INTO subscriptions(session_id,type,role,target) VALUES(?,?,?,?)",
            (session_id, sub_type, role, target),
        )
    except sqlite3.IntegrityError:
        pass


def _migrate_seen_posts_v2(conn: sqlite3.Connection, id_map: dict[int, tuple[str, str]]):
    if "seen_posts" not in _table_names(conn):
        return
    if "subscription_id" not in _column_names(conn, "seen_posts"):
        return

    conn.execute("ALTER TABLE seen_posts RENAME TO seen_posts_old")
    _create_seen_posts(conn)
    _copy_seen_posts(conn, id_map)
    conn.execute("DROP TABLE seen_posts_old")


def _create_seen_posts(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS seen_posts (
            session_id TEXT    NOT NULL,
            type       TEXT    NOT NULL,
            post_id    TEXT    NOT NULL,
            seen_at    INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            PRIMARY KEY (session_id, type, post_id)
        );
    """)


def _copy_seen_posts(conn: sqlite3.Connection, id_map: dict[int, tuple[str, str]]):
    rows = conn.execute("SELECT subscription_id,post_id,seen_at FROM seen_posts_old").fetchall()
    for sub_id, post_id, seen_at in rows:
        if sub_id not in id_map:
            continue
        session_id, sub_type = id_map[sub_id]
        conn.execute(
            "INSERT OR IGNORE INTO seen_posts(session_id,type,post_id,seen_at) VALUES(?,?,?,?)",
            (session_id, sub_type, post_id, seen_at),
        )
