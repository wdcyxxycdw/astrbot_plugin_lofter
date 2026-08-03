import json
import sqlite3

NOW = "strftime('%s','now')"


def create_legacy_db(db_path: str, version: int, filter_rule: str | None = None) -> None:
    conn = sqlite3.connect(db_path)
    try:
        _create_config(conn, version)
        if version == 1:
            _create_v1(conn)
        else:
            _create_v2_base(conn)
            if version >= 3:
                _create_count_conditions(conn)
            if version >= 4:
                _create_author_blocks(conn)
        _insert_fixture(conn, version, filter_rule)
        conn.commit()
    finally:
        conn.close()


def _create_config(conn: sqlite3.Connection, version: int) -> None:
    conn.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    if version > 1:
        conn.execute("INSERT INTO config(key,value) VALUES('schema_version',?)", (str(version),))


def _create_v1(conn: sqlite3.Connection) -> None:
    conn.execute(f"""
        CREATE TABLE subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            type TEXT NOT NULL,
            target TEXT NOT NULL,
            filter_rule TEXT DEFAULT NULL,
            created_at INTEGER NOT NULL DEFAULT ({NOW}),
            UNIQUE(session_id,type,target)
        )
    """)
    conn.execute(f"""
        CREATE TABLE seen_posts (
            subscription_id INTEGER NOT NULL,
            post_id TEXT NOT NULL,
            seen_at INTEGER NOT NULL DEFAULT ({NOW}),
            PRIMARY KEY(subscription_id,post_id)
        )
    """)
    _create_sent_posts(conn)


def _create_v2_base(conn: sqlite3.Connection) -> None:
    conn.execute(f"""
        CREATE TABLE subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('tag','blog')),
            role TEXT NOT NULL DEFAULT 'subscribe' CHECK(role IN ('subscribe','exclude')),
            target TEXT NOT NULL,
            created_at INTEGER NOT NULL DEFAULT ({NOW}),
            UNIQUE(session_id,type,role,target)
        )
    """)
    conn.execute(f"""
        CREATE TABLE seen_posts (
            session_id TEXT NOT NULL,
            type TEXT NOT NULL,
            post_id TEXT NOT NULL,
            seen_at INTEGER NOT NULL DEFAULT ({NOW}),
            PRIMARY KEY(session_id,type,post_id)
        )
    """)
    _create_sent_posts(conn)


def _create_sent_posts(conn: sqlite3.Connection) -> None:
    conn.execute(f"""
        CREATE TABLE sent_posts (
            session_id TEXT NOT NULL,
            post_id TEXT NOT NULL,
            sent_at INTEGER NOT NULL DEFAULT ({NOW}),
            PRIMARY KEY(session_id,post_id)
        )
    """)


def _create_count_conditions(conn: sqlite3.Connection) -> None:
    conn.execute(f"""
        CREATE TABLE count_conditions (
            name TEXT PRIMARY KEY,
            expression TEXT NOT NULL,
            updated_at INTEGER NOT NULL DEFAULT ({NOW})
        )
    """)


def _create_author_blocks(conn: sqlite3.Connection) -> None:
    conn.execute(f"""
        CREATE TABLE author_blocks (
            session_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('name','username')),
            value TEXT NOT NULL,
            display TEXT NOT NULL,
            created_at INTEGER NOT NULL DEFAULT ({NOW}),
            PRIMARY KEY(session_id,kind,value)
        )
    """)


def _insert_fixture(conn: sqlite3.Connection, version: int, filter_rule: str | None) -> None:
    if version == 1:
        _insert_v1_fixture(conn, filter_rule)
    else:
        _insert_v2_fixture(conn)
    if version >= 3:
        conn.execute(
            "INSERT INTO count_conditions(name,expression,updated_at) VALUES('cond','tag',103)"
        )
    if version >= 4:
        conn.execute("""
            INSERT INTO author_blocks(session_id,kind,value,display,created_at)
            VALUES('sess','username','blocked','Blocked',104)
        """)


def _insert_v1_fixture(conn: sqlite3.Connection, filter_rule: str | None) -> None:
    if filter_rule is None:
        filter_rule = json.dumps(
            {"search_tags": ["primary", "extra"], "exclude_tags": ["blocked"]}
        )
    conn.execute("""
        INSERT INTO subscriptions(id,session_id,type,target,filter_rule,created_at)
        VALUES(7,'sess','tag','primary',?,100)
    """, (filter_rule,))
    conn.execute(
        "INSERT INTO seen_posts(subscription_id,post_id,seen_at) VALUES(7,'00A_000B',101)"
    )
    conn.execute(
        "INSERT INTO sent_posts(session_id,post_id,sent_at) VALUES('sess','00A_000B',102)"
    )


def _insert_v2_fixture(conn: sqlite3.Connection) -> None:
    conn.execute("""
        INSERT INTO subscriptions(id,session_id,type,role,target,created_at) VALUES
        (7,'sess','tag','subscribe','primary',100),
        (8,'sess','tag','subscribe','second',100),
        (9,'sess','tag','exclude','blocked',100)
    """)
    conn.execute("""
        INSERT INTO seen_posts(session_id,type,post_id,seen_at) VALUES
        ('sess','tag','00A_000B',101),('sess','tag','a_b',102)
    """)
    conn.execute("""
        INSERT INTO sent_posts(session_id,post_id,sent_at) VALUES
        ('sess','00A_000B',102),('sess','a_b',103)
    """)
