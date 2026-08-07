import asyncio
import json
import sqlite3

import pytest
import pytest_asyncio

from core.db import LofterDB


@pytest_asyncio.fixture
async def db(tmp_path):
    d = LofterDB(str(tmp_path / "test.db"))
    await d.initialize()
    yield d
    await d.close()


def _create_v1_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            type TEXT NOT NULL,
            target TEXT NOT NULL,
            filter_rule TEXT DEFAULT NULL,
            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            UNIQUE(session_id, type, target)
        );
        CREATE TABLE seen_posts (
            subscription_id INTEGER NOT NULL,
            post_id TEXT NOT NULL,
            seen_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            PRIMARY KEY (subscription_id, post_id)
        );
        CREATE TABLE sent_posts (
            session_id TEXT NOT NULL,
            post_id TEXT NOT NULL,
            sent_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            PRIMARY KEY (session_id, post_id)
        );
    """)
    _insert_v1_fixture(conn)
    conn.commit()
    conn.close()


def _insert_v1_fixture(conn: sqlite3.Connection):
    filter_rule = json.dumps({
        "search_tags": ["原神"],
        "include_tags": [],
        "exclude_tags": ["R18"],
        "or_tag_groups": [],
        "raw": "原神 -R18",
    })
    conn.execute(
        "INSERT INTO subscriptions(session_id,type,target,filter_rule) VALUES(?,?,?,?)",
        ("sess1", "tag", "原神", filter_rule),
    )
    sub_id = conn.execute("SELECT id FROM subscriptions WHERE target='原神'").fetchone()[0]
    conn.execute(
        "INSERT INTO seen_posts(subscription_id,post_id,seen_at) VALUES(?,?,?)",
        (sub_id, "post123", 1000000),
    )


@pytest.mark.asyncio
async def test_config_get_set(db):
    assert await db.get_config("foo") is None
    await db.set_config("foo", "bar")
    assert await db.get_config("foo") == "bar"
    await db.set_config("foo", "baz")
    assert await db.get_config("foo") == "baz"


@pytest.mark.asyncio
async def test_add_remove_subscription(db):
    ok = await db.add_subscription("sess1", "tag", "原创", "subscribe")
    assert ok is True

    ok2 = await db.add_subscription("sess1", "tag", "原创", "subscribe")
    assert ok2 is False

    sub_id = await db.get_subscription_id("sess1", "tag", "原创", "subscribe")
    assert sub_id is not None

    rows = await db.list_subscriptions("sess1")
    assert len(rows) == 1

    removed = await db.remove_subscription("sess1", "tag", "原创", "subscribe")
    assert removed is True

    assert await db.get_subscription_id("sess1", "tag", "原创", "subscribe") is None


@pytest.mark.asyncio
async def test_concurrent_subscription_writes_do_not_share_connection_unsafely(db):
    async def add(i: int):
        return await db.add_subscription(f"sess{i % 20}", "tag", f"tag{i % 50}", "subscribe")

    results = await asyncio.gather(*(add(i) for i in range(1000)), return_exceptions=True)
    errors = [item for item in results if isinstance(item, Exception)]

    assert errors == []
    assert len(await db.list_subscriptions()) == 100


@pytest.mark.asyncio
async def test_add_subscribe_and_exclude_are_independent(db):
    ok1 = await db.add_subscription("sess1", "tag", "R18", "subscribe")
    ok2 = await db.add_subscription("sess1", "tag", "R18", "exclude")
    assert ok1 is True
    assert ok2 is True

    rows = await db.list_subscriptions("sess1")
    assert len(rows) == 2

    removed = await db.remove_subscription("sess1", "tag", "R18", "exclude")
    assert removed is True

    rows2 = await db.list_subscriptions("sess1")
    assert len(rows2) == 1
    assert rows2[0][3] == "subscribe"


@pytest.mark.asyncio
async def test_filter_unseen_session(db):
    ids = ["1", "2", "3"]
    unseen = await db.filter_unseen_session("sess1", "tag", ids)
    assert set(unseen) == {"1", "2", "3"}

    await db.mark_seen_session("sess1", "tag", ["1", "2"])
    unseen2 = await db.filter_unseen_session("sess1", "tag", ids)
    assert unseen2 == ["3"]

    await db.mark_seen_session("sess1", "tag", ["3"])
    unseen3 = await db.filter_unseen_session("sess1", "tag", ids)
    assert unseen3 == []


@pytest.mark.asyncio
async def test_seen_count(db):
    assert await db.seen_count("sess1", "tag") == 0
    await db.mark_seen_session("sess1", "tag", ["1", "2"])
    assert await db.seen_count("sess1", "tag") == 2
    assert await db.seen_count("sess1", "blog") == 0


@pytest.mark.asyncio
async def test_cold_start_no_push(db):
    all_ids = ["10", "20", "30"]
    unseen = await db.filter_unseen_session("sess1", "tag", all_ids)
    is_cold = await db.seen_count("sess1", "tag") == 0
    assert is_cold is True
    assert set(unseen) == {"10", "20", "30"}

    await db.mark_seen_session("sess1", "tag", unseen)
    unseen_after = await db.filter_unseen_session("sess1", "tag", all_ids)
    assert unseen_after == []


@pytest.mark.asyncio
async def test_filter_unsent_marks_sent(db):
    await db.mark_sent("sess1", ["a", "b"])
    result = await db.filter_unsent("sess1", ["a", "b", "c"])
    assert result == ["c"]

    await db.mark_sent("sess1", ["c"])
    result2 = await db.filter_unsent("sess1", ["a", "b", "c"])
    assert result2 == []


@pytest.mark.asyncio
async def test_sent_posts_session_isolated(db):
    await db.mark_sent("sess1", ["x"])
    result = await db.filter_unsent("sess2", ["x"])
    assert result == ["x"]


@pytest.mark.asyncio
async def test_seen_session_isolated(db):
    await db.mark_seen_session("sess1", "tag", ["a"])
    unseen = await db.filter_unseen_session("sess2", "tag", ["a"])
    assert unseen == ["a"]


@pytest.mark.asyncio
async def test_remove_subscription_by_id(db):
    await db.add_subscription("sess1", "tag", "test", "subscribe")
    sub_id = await db.get_subscription_id("sess1", "tag", "test", "subscribe")
    assert sub_id is not None

    ok = await db.remove_subscription_by_id(sub_id)
    assert ok is True

    assert await db.get_subscription_id("sess1", "tag", "test", "subscribe") is None


@pytest.mark.asyncio
async def test_migration_from_v1(tmp_path):
    """验证旧 v1 schema 数据迁移到 v2"""
    db_path = str(tmp_path / "old.db")
    _create_v1_db(db_path)

    db = LofterDB(db_path)
    await db.initialize()

    rows = await db.list_subscriptions("sess1")
    targets = {(r[3], r[4]) for r in rows}
    assert ("subscribe", "原神") in targets
    assert ("exclude", "R18") in targets

    unseen = await db.filter_unseen_session("sess1", "tag", ["post123"])
    assert unseen == []

    await db.close()


@pytest.mark.asyncio
async def test_count_condition_crud(db):
    await db.upsert_count_condition("米哈游相关", "原神 -R18")
    rows = await db.list_count_conditions()
    assert rows == [("米哈游相关", "原神 -R18")]

    await db.upsert_count_condition("米哈游相关", "原神 同人")
    rows = await db.list_count_conditions()
    assert rows == [("米哈游相关", "原神 同人")]

    deleted = await db.delete_count_condition("米哈游相关")
    assert deleted is True
    assert await db.list_count_conditions() == []


@pytest.mark.asyncio
async def test_migration_v2_to_v3_adds_count_conditions(tmp_path):
    import sqlite3

    db_path = str(tmp_path / "v2.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO config(key,value) VALUES('schema_version','2');
        CREATE TABLE subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('tag','blog')),
            role TEXT NOT NULL CHECK(role IN ('subscribe','exclude')) DEFAULT 'subscribe',
            target TEXT NOT NULL,
            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            UNIQUE(session_id, type, role, target)
        );
        CREATE TABLE seen_posts (
            session_id TEXT NOT NULL,
            type TEXT NOT NULL,
            post_id TEXT NOT NULL,
            seen_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            PRIMARY KEY (session_id, type, post_id)
        );
        CREATE TABLE sent_posts (
            session_id TEXT NOT NULL,
            post_id TEXT NOT NULL,
            sent_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            PRIMARY KEY (session_id, post_id)
        );
    """)
    conn.commit()
    conn.close()

    db = LofterDB(db_path)
    await db.initialize()
    await db.upsert_count_condition("条件", "原神")
    assert await db.list_count_conditions() == [("条件", "原神")]
    assert await db.get_config("schema_version") == "4"
    await db.close()


@pytest.mark.asyncio
async def test_author_block_crud_is_session_scoped(db):
    ok = await db.add_author_block("sess1", "name", "作者a", "作者A")
    assert ok is True
    assert await db.add_author_block("sess1", "name", "作者a", "作者A") is False
    assert await db.add_author_block("sess2", "name", "作者a", "作者A") is True

    rows = await db.list_author_blocks("sess1")
    assert [(r[1], r[2], r[3]) for r in rows] == [("name", "作者a", "作者A")]

    removed = await db.remove_author_block("sess1", "name", "作者a")
    assert removed is True
    assert await db.list_author_blocks("sess1") == []


@pytest.mark.asyncio
async def test_migration_v3_to_v4_adds_author_blocks(tmp_path):
    db_path = str(tmp_path / "v3.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO config(key,value) VALUES('schema_version','3');
        CREATE TABLE subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('tag','blog')),
            role TEXT NOT NULL CHECK(role IN ('subscribe','exclude')) DEFAULT 'subscribe',
            target TEXT NOT NULL,
            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            UNIQUE(session_id, type, role, target)
        );
        CREATE TABLE seen_posts (
            session_id TEXT NOT NULL,
            type TEXT NOT NULL,
            post_id TEXT NOT NULL,
            seen_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            PRIMARY KEY (session_id, type, post_id)
        );
        CREATE TABLE sent_posts (
            session_id TEXT NOT NULL,
            post_id TEXT NOT NULL,
            sent_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            PRIMARY KEY (session_id, post_id)
        );
        CREATE TABLE count_conditions (
            name TEXT PRIMARY KEY,
            expression TEXT NOT NULL,
            updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        );
    """)
    conn.commit()
    conn.close()

    migrated = LofterDB(db_path)
    await migrated.initialize()
    assert await migrated.add_author_block("sess1", "username", "someuser", "someuser") is True
    assert await migrated.get_config("schema_version") == "4"
    await migrated.close()
