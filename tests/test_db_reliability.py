import asyncio
import json
import sqlite3
import threading

import pytest
import pytest_asyncio

from core.db import DatabaseClosedError, DatabaseState, LofterDB, SQLiteBusyError
from core.db_json_migration import JsonMigrationError, migrate_json_v2
from core.db_migrations import SchemaVersionError
from core.db_schema import CREATE_STATEMENTS, SCHEMA_VERSION, SchemaValidationError, validate_schema
from core.db_sql import extract_checks, strip_sql_comments
from core.instance_lock import InstanceLock, InstanceLockHeldError
from tests.db_fixtures import create_legacy_db


@pytest_asyncio.fixture
async def db(tmp_path):
    database = LofterDB(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


async def add_source(db: LofterDB, session: str = "sess", sub_type: str = "tag") -> int:
    assert await db.add_subscription(session, sub_type, "source") is True
    sub_id = await db.get_subscription_id(session, sub_type, "source")
    assert sub_id is not None
    return sub_id


def _create_modified_v5(path, old: str, new: str) -> None:
    conn = sqlite3.connect(path)
    try:
        for _, statement in CREATE_STATEMENTS:
            conn.execute(statement.replace(old, new))
        conn.execute(
            "INSERT INTO config(key,value) VALUES('schema_version','5')"
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("CHECK(revision > 0)", "CHECK(revision > 0 OR 1)"),
        (
            "UNIQUE(session_id, type, role, target)",
            "UNIQUE(target, role, type, session_id)",
        ),
        (
            "REFERENCES subscriptions(id) ON DELETE CASCADE",
            "REFERENCES subscriptions(id) ON UPDATE CASCADE ON DELETE CASCADE",
        ),
        (
            "REFERENCES subscriptions(id) ON DELETE CASCADE",
            "REFERENCES subscriptions(id) ON DELETE CASCADE MATCH FULL",
        ),
        (
            "REFERENCES subscriptions(id) ON DELETE CASCADE",
            "REFERENCES subscriptions(id) ON DELETE CASCADE "
            "DEFERRABLE INITIALLY DEFERRED",
        ),
        (
            "REFERENCES subscriptions(id) ON DELETE CASCADE",
            "REFERENCES subscriptions(id) ON DELETE CASCADE "
            "NOT DEFERRABLE INITIALLY IMMEDIATE",
        ),
    ],
)
def test_strict_v5_rejects_weakened_constraints(tmp_path, old, new):
    path = tmp_path / "modified-v5.db"
    _create_modified_v5(path, old, new)
    conn = sqlite3.connect(path)
    try:
        with pytest.raises(SchemaValidationError):
            validate_schema(conn)
    finally:
        conn.close()


def test_strict_v5_rejects_partial_unique_index(tmp_path):
    path = tmp_path / "partial-v5.db"
    conn = sqlite3.connect(path)
    try:
        for name, statement in CREATE_STATEMENTS:
            if name == "table:subscriptions":
                statement = statement.replace(
                    ",\n            UNIQUE(session_id, type, role, target)", ""
                )
            conn.execute(statement)
        conn.execute("""
            CREATE UNIQUE INDEX subscriptions_partial_unique
            ON subscriptions(session_id,type,role,target) WHERE role='subscribe'
        """)
        with pytest.raises(SchemaValidationError):
            validate_schema(conn)
    finally:
        conn.close()


def test_strict_v5_rejects_business_unique_collation_change(tmp_path):
    path = tmp_path / "unique-collation-v5.db"
    _create_modified_v5(
        path,
        "target TEXT NOT NULL",
        "target TEXT COLLATE NOCASE NOT NULL",
    )
    conn = sqlite3.connect(path)
    try:
        with pytest.raises(SchemaValidationError):
            validate_schema(conn)
    finally:
        conn.close()


def test_strict_v5_rejects_unindexed_column_collation_change(tmp_path):
    path = tmp_path / "plain-column-collation-v5.db"
    _create_modified_v5(
        path,
        "payload_json TEXT",
        "payload_json TEXT COLLATE NOCASE",
    )
    conn = sqlite3.connect(path)
    try:
        with pytest.raises(SchemaValidationError, match="column collation"):
            validate_schema(conn)
    finally:
        conn.close()


def test_sql_comment_stripping_preserves_string_literals():
    sql = "SELECT '--keep', '/*keep*/'; -- drop\n/* drop */ CHECK(value='a--b')"
    assert strip_sql_comments(sql) == "SELECT '--keep', '/*keep*/'; \n  CHECK(value='a--b')"
    assert extract_checks(sql) == ("value='a--b'",)


@pytest.mark.parametrize("comment", ["/* CHECK(revision > 0) */", "-- CHECK(revision > 0)\n"])
def test_strict_v5_rejects_comment_forged_check(tmp_path, comment):
    path = tmp_path / "comment-check-v5.db"
    _create_modified_v5(path, "CHECK(revision > 0)", comment)
    conn = sqlite3.connect(path)
    try:
        with pytest.raises(SchemaValidationError, match="CHECK mismatch"):
            validate_schema(conn)
    finally:
        conn.close()


def test_strict_v5_ignores_constraint_words_inside_comments(tmp_path):
    path = tmp_path / "harmless-comment-v5.db"
    _create_modified_v5(
        path,
        "REFERENCES subscriptions(id) ON DELETE CASCADE",
        "REFERENCES subscriptions(id) ON DELETE CASCADE /* MATCH FULL DEFERRABLE */",
    )
    conn = sqlite3.connect(path)
    try:
        validate_schema(conn)
    finally:
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "REFERENCES subscriptions(id) ON DELETE CASCADE",
            "REFERENCES subscriptions(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED",
        ),
        (
            "REFERENCES subscriptions(id) ON DELETE CASCADE",
            "REFERENCES subscriptions(id) ON DELETE CASCADE "
            "NOT DEFERRABLE INITIALLY IMMEDIATE",
        ),
        ("CHECK(revision > 0)", "/* CHECK(revision > 0) */"),
        ("CHECK(revision > 0)", "-- CHECK(revision > 0)\n"),
        ("payload_json TEXT", "payload_json TEXT COLLATE NOCASE"),
    ],
    ids=[
        "deferred-fk", "immediate-fk", "block-comment-check",
        "line-comment-check", "plain-column-collation",
    ],
)
async def test_v5_marker_rejects_ddl_variants_on_initialize(tmp_path, old, new):
    path = tmp_path / "modified-marker-v5.db"
    _create_modified_v5(path, old, new)
    database = LofterDB(str(path))
    with pytest.raises(SchemaValidationError):
        await database.initialize()
    await database.close()
    conn = sqlite3.connect(path)
    try:
        assert conn.execute(
            "SELECT value FROM config WHERE key='schema_version'"
        ).fetchone() == ("5",)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "CREATE INDEX IF NOT EXISTS idx_seen_posts_seen_at",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_seen_posts_seen_at",
        ),
        (
            "ON seen_posts(seen_at)",
            "ON seen_posts(seen_at) WHERE seen_at > 0",
        ),
    ],
    ids=["unique", "partial"],
)
def test_strict_v5_rejects_named_index_metadata_changes(tmp_path, old, new):
    path = tmp_path / f"named-index-{old.startswith('ON')}.db"
    _create_modified_v5(path, old, new)
    conn = sqlite3.connect(path)
    try:
        with pytest.raises(SchemaValidationError):
            validate_schema(conn)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_new_subscribe_inherits_seen_union_and_survives_source_removal(db):
    assert await db.add_subscription("sess", "tag", "A")
    first = await db.get_subscription_id("sess", "tag", "A")
    await db.mark_seen_session("sess", "tag", ["00A_000B", "opaque"])
    await db.transaction(lambda conn: conn.execute(
        "UPDATE seen_posts SET published_at=10,seen_at=20 WHERE subscription_id=?", (first,)
    ))

    assert await db.add_subscription("sess", "tag", "B")
    second = await db.get_subscription_id("sess", "tag", "B")
    rows = await db.transaction(lambda conn: conn.execute(
        "SELECT post_id,published_at,seen_at FROM seen_posts WHERE subscription_id=? ORDER BY post_id",
        (second,),
    ).fetchall())
    assert rows == [("a_b", 10, 20), ("opaque", 10, 20)]
    assert await db.remove_subscription("sess", "tag", "A")
    assert await db.filter_unseen_session("sess", "tag", ["a_b", "opaque"]) == []


@pytest.mark.asyncio
async def test_blog_seen_union_and_exclude_does_not_inherit(db):
    assert await db.add_subscription("sess", "blog", "A")
    await db.mark_seen_session("sess", "blog", ["old"])
    assert await db.add_subscription("sess", "blog", "B")
    assert await db.remove_subscription("sess", "blog", "A")
    assert await db.filter_unseen_session("sess", "blog", ["old"]) == []

    assert await db.add_subscription("tags", "tag", "A")
    await db.mark_seen_session("tags", "tag", ["old"])
    assert await db.add_subscription("tags", "tag", "blocked", "exclude")
    inherited = await db.transaction(lambda conn: conn.execute("""
        SELECT COUNT(*) FROM seen_posts sp JOIN subscriptions s ON s.id=sp.subscription_id
        WHERE s.session_id='tags' AND s.role='exclude'
    """).fetchone()[0])
    assert inherited == 0


@pytest.mark.asyncio
async def test_close_waits_for_cancelled_initialize_and_closes_connection_once(tmp_path, monkeypatch):
    database = LofterDB(str(tmp_path / "cancel.db"))
    started = threading.Event()
    release = threading.Event()
    close_count = 0
    original_open = database._open_connection

    def increment():
        nonlocal close_count
        close_count += 1

    def delayed_open():
        conn = original_open()
        proxy = _CountingConnection(conn, increment)
        started.set()
        release.wait(2)
        return proxy

    monkeypatch.setattr(database, "_open_connection", delayed_open)
    waiter = asyncio.create_task(database.initialize())
    await asyncio.to_thread(started.wait, 2)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    close_task = asyncio.create_task(database.close())
    release.set()
    await close_task
    assert close_count == 1
    assert database._conn is None
    with pytest.raises(DatabaseClosedError):
        await database.initialize()


@pytest.mark.asyncio
async def test_cancelled_close_during_opening_finishes_cleanup(tmp_path, monkeypatch):
    database = LofterDB(str(tmp_path / "cancel-close.db"))
    started = threading.Event()
    release = threading.Event()
    close_count = 0
    original_open = database._open_connection

    def delayed_open():
        nonlocal close_count
        conn = original_open()
        proxy = _CountingConnection(conn, lambda: _increment())
        started.set()
        release.wait(2)
        return proxy

    def _increment():
        nonlocal close_count
        close_count += 1

    monkeypatch.setattr(database, "_open_connection", delayed_open)
    initialize_task = asyncio.create_task(database.initialize())
    await asyncio.to_thread(started.wait, 2)
    close_task = asyncio.create_task(database.close())
    await asyncio.sleep(0)
    assert database._close_task is not None
    close_task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await close_task
    assert database._close_task.done()
    await initialize_task

    assert close_count == 1
    assert database._conn is None
    assert database._executor_closed is True
    assert database._state is DatabaseState.CLOSED
    await asyncio.gather(database.close(), database.close())
    with pytest.raises(DatabaseClosedError):
        await database.initialize()


class _CountingConnection:
    def __init__(self, connection, on_close):
        self._connection = connection
        self._on_close = on_close

    def close(self):
        self._on_close()
        return self._connection.close()

    def __getattr__(self, name):
        return getattr(self._connection, name)


@pytest.mark.asyncio
async def test_initialize_failure_then_multiple_close_is_safe(tmp_path):
    def fault(name, conn):
        if name == "validate":
            raise RuntimeError("init failed")

    database = LofterDB(str(tmp_path / "failed.db"), migration_fault_hook=fault)
    with pytest.raises(RuntimeError, match="init failed"):
        await database.initialize()
    await asyncio.gather(database.close(), database.close())
    await database.close()


@pytest.mark.asyncio
async def test_json_marker_two_skips_missing_unreadable_and_corrupt_source(
    db, tmp_path, monkeypatch
):
    await db.set_config("json_migration_version", "2")
    missing = tmp_path / "missing.json"
    assert (await migrate_json_v2(db, str(missing))).already_migrated
    corrupt = tmp_path / "subscriptions.json"
    corrupt.write_text("{bad", encoding="utf-8")
    monkeypatch.setattr(
        "core.db_json_migration.Path.read_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("no")),
    )
    assert (await migrate_json_v2(db, str(corrupt))).already_migrated


@pytest.mark.asyncio
async def test_empty_database_creates_strict_v5_in_one_transaction(db):
    assert await db.get_config("schema_version") == str(SCHEMA_VERSION)
    await db.transaction(validate_schema)
    tables = await db.transaction(lambda conn: {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    })
    assert "sent_posts" not in tables
    assert {"subscription_watermarks", "legacy_checkpoints", "deliveries", "delivery_sources"} <= tables


@pytest.mark.asyncio
async def test_transaction_callback_is_atomic_and_never_crosses_await(db):
    def fail(conn):
        conn.execute("INSERT INTO config(key,value) VALUES('atomic','before')")
        raise RuntimeError("stop")

    with pytest.raises(RuntimeError, match="stop"):
        await db.transaction(fail)
    assert await db.get_config("atomic") is None

    async def invalid(conn):
        return None

    with pytest.raises(TypeError, match="synchronous"):
        await db.transaction(invalid)
    assert db._conn.in_transaction is False


@pytest.mark.asyncio
async def test_target_conflict_is_duplicate_but_other_integrity_errors_propagate(db):
    assert await db.add_subscription("sess", "tag", "same") is True
    assert await db.add_subscription("sess", "tag", "same") is False
    with pytest.raises(sqlite3.IntegrityError):
        await db.add_subscription("sess", "invalid", "bad")
    assert db._conn.in_transaction is False
    assert await db.add_subscription("sess", "blog", "ok") is True

    assert await db.add_author_block("sess", "name", "value", "Value") is True
    assert await db.add_author_block("sess", "name", "value", "Value") is False
    with pytest.raises(sqlite3.IntegrityError):
        await db.add_author_block("sess", "bad-kind", "value2", "Value")


@pytest.mark.asyncio
async def test_subscription_level_seen_and_delivery_compatibility(db):
    first = await add_source(db)
    assert await db.add_subscription("sess", "tag", "second") is True
    second = await db.get_subscription_id("sess", "tag", "second")
    assert await db.add_subscription("sess", "tag", "blocked", "exclude") is True

    assert await db.mark_seen_session("sess", "tag", ["00A_000B", "opaque"]) == 4
    assert await db.filter_unseen_session("sess", "tag", ["a_b", "opaque", "new"]) == ["new"]
    assert await db.seen_count("sess", "tag") == 2
    rows = await db.transaction(lambda conn: conn.execute(
        "SELECT subscription_id,post_id FROM seen_posts ORDER BY subscription_id,post_id"
    ).fetchall())
    assert {row[0] for row in rows} == {first, second}

    assert await db.mark_sent("sess", ["00A_000B"]) == 1
    assert await db.mark_sent("sess", ["a_b"]) == 0
    assert await db.filter_unsent("sess", ["00a_000b", "opaque"]) == ["opaque"]


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2, 3, 4])
async def test_real_legacy_versions_upgrade_to_v5(tmp_path, version):
    path = str(tmp_path / f"v{version}.db")
    create_legacy_db(path, version)
    migrated = LofterDB(path)
    await migrated.initialize()
    try:
        await migrated.transaction(validate_schema)
        assert await migrated.get_config("schema_version") == "5"
        rows = await migrated.list_subscriptions("sess")
        assert any(row[0] == 7 and row[3] == "subscribe" for row in rows)
        assert await migrated.filter_unseen_session("sess", "tag", ["a_b"]) == []
        if version == 1:
            seen_targets = await migrated.transaction(lambda conn: conn.execute("""
                SELECT s.target FROM seen_posts sp
                JOIN subscriptions s ON s.id=sp.subscription_id
                WHERE sp.post_id='a_b' ORDER BY s.target
            """).fetchall())
            assert seen_targets == [("extra",), ("primary",)]
        assert await migrated.filter_unsent("sess", ["a_b"]) == []
        if version >= 3:
            assert await migrated.list_count_conditions() == [("cond", "tag")]
        if version >= 4:
            assert len(await migrated.list_author_blocks("sess")) == 1
    finally:
        await migrated.close()


@pytest.mark.asyncio
async def test_v4_seen_copies_only_to_subscribe_and_merges_canonical_conflicts(tmp_path):
    path = str(tmp_path / "v4.db")
    create_legacy_db(path, 4)
    migrated = LofterDB(path)
    await migrated.initialize()
    try:
        rows = await migrated.transaction(lambda conn: conn.execute("""
            SELECT s.role,s.target,sp.post_id,sp.seen_at
            FROM seen_posts sp JOIN subscriptions s ON s.id=sp.subscription_id
            ORDER BY s.id,sp.post_id
        """).fetchall())
        assert {(role, target) for role, target, _, _ in rows} == {
            ("subscribe", "primary"), ("subscribe", "second")
        }
        assert {post_id for _, _, post_id, _ in rows} == {"a_b"}
        assert all(seen_at == 102 for *_, seen_at in rows)
        deliveries = await migrated.transaction(lambda conn: conn.execute(
            "SELECT post_id,status,accepted_at FROM deliveries"
        ).fetchall())
        assert deliveries == [("a_b", "accepted", 102)]
    finally:
        await migrated.close()


@pytest.mark.asyncio
async def test_v1_generated_subscriptions_keep_legacy_created_time(tmp_path):
    path = str(tmp_path / "v1.db")
    create_legacy_db(path, 1)
    migrated = LofterDB(path)
    await migrated.initialize()
    try:
        rows = await migrated.transaction(lambda conn: conn.execute("""
            SELECT target,created_at,initialized_at FROM subscriptions
            WHERE session_id='sess' ORDER BY target
        """).fetchall())
        assert rows == [
            ("blocked", 100, 100),
            ("extra", 100, 100),
            ("primary", 100, 100),
        ]
    finally:
        await migrated.close()


@pytest.mark.asyncio
async def test_migration_initializes_revision_policy_activity_and_watermarks(tmp_path):
    path = str(tmp_path / "v4.db")
    create_legacy_db(path, 4)
    migrated = LofterDB(path)
    await migrated.initialize()
    try:
        revisions = await migrated.transaction(lambda conn: conn.execute(
            "SELECT session_id,subscription_type,revision FROM subscription_revisions"
        ).fetchall())
        assert revisions == [("sess", "tag", 1)]
        assert await migrated.transaction(lambda conn: conn.execute(
            "SELECT policy_generation FROM session_policies WHERE session_id='sess'"
        ).fetchone()) == (1,)
        assert await migrated.transaction(lambda conn: conn.execute(
            "SELECT inactive_since FROM session_activity WHERE session_id='sess'"
        ).fetchone()) == (None,)
        assert await migrated.transaction(lambda conn: conn.execute(
            "SELECT COUNT(*) FROM subscription_watermarks"
        ).fetchone()) == (3,)
    finally:
        await migrated.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["6", "bad", "0", "5.0"])
async def test_future_or_malformed_marker_fails_closed(tmp_path, marker):
    path = tmp_path / "bad.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    conn.execute("INSERT INTO config VALUES('schema_version',?)", (marker,))
    conn.commit()
    conn.close()
    database = LofterDB(str(path))
    with pytest.raises(SchemaVersionError):
        await database.initialize()
    await database.close()


@pytest.mark.asyncio
async def test_v5_marker_with_malformed_structure_fails_closed(tmp_path):
    path = tmp_path / "bad-v5.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    conn.execute("INSERT INTO config VALUES('schema_version','5')")
    conn.commit()
    conn.close()
    database = LofterDB(str(path))
    with pytest.raises(SchemaValidationError):
        await database.initialize()
    await database.close()


@pytest.mark.asyncio
async def test_foreign_key_check_fails_initialization(tmp_path):
    path = tmp_path / "fk.db"
    good = LofterDB(str(path))
    await good.initialize()
    await add_source(good)
    await good.close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO seen_posts(subscription_id,post_id,published_at,seen_at) VALUES(999,'x',1,1)"
    )
    conn.commit()
    conn.close()
    reopened = LofterDB(str(path))
    with pytest.raises(SchemaValidationError, match="foreign_key_check"):
        await reopened.initialize()
    await reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["rename:config", "table:deliveries", "copy:subscription:0", "copy:seen:0:0", "copy:sent:0", "marker", "validate", "commit"])
async def test_migration_faults_rollback_without_mixed_schema(tmp_path, stage):
    path = str(tmp_path / f"fault-{stage.replace(':', '-')}.db")
    create_legacy_db(path, 4)

    def fault(name, conn):
        assert conn.in_transaction
        if name == stage:
            raise RuntimeError(name)

    database = LofterDB(path, migration_fault_hook=fault)
    with pytest.raises(RuntimeError, match=stage):
        await database.initialize()
    await database.close()
    conn = sqlite3.connect(path)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        assert not any(name.startswith("_v5_old_") for name in tables)
        assert conn.execute("SELECT value FROM config WHERE key='schema_version'").fetchone() == ("4",)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_sqlite_lock_is_classified_separately(tmp_path):
    path = str(tmp_path / "busy.db")
    database = LofterDB(path)
    await database.initialize()
    blocker = sqlite3.connect(path, timeout=0.1)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        database._conn.execute("PRAGMA busy_timeout=1")
        with pytest.raises(SQLiteBusyError):
            await database.set_config("busy", "1")
    finally:
        blocker.rollback()
        blocker.close()
        await database.close()


@pytest.mark.asyncio
async def test_close_is_idempotent_under_concurrency(tmp_path):
    database = LofterDB(str(tmp_path / "close.db"))
    await database.initialize()
    await asyncio.gather(database.close(), database.close())
    await database.close()


@pytest.mark.asyncio
async def test_instance_lock_is_process_lifecycle_advisory_lock(tmp_path):
    path = str(tmp_path / "locked.db")
    first = InstanceLock(path)
    second = InstanceLock(path)
    first.acquire()
    with pytest.raises(InstanceLockHeldError):
        second.acquire()
    first.release()
    second.acquire()
    second.release()
    assert (tmp_path / "locked.db.lock").exists()


@pytest.mark.asyncio
async def test_json_v2_validates_outside_db_and_preserves_source(db, tmp_path):
    path = tmp_path / "subscriptions.json"
    path.write_text(json.dumps({"subscriptions": [
        {"session_id": "sess", "type": "tag", "target": "tag"},
        {"session_id": "sess", "type": "invalid", "target": "bad"},
    ]}), encoding="utf-8")
    with pytest.raises(JsonMigrationError):
        await migrate_json_v2(db, str(path))
    assert await db.get_config("json_migration_version") is None
    assert await db.list_subscriptions() == []
    assert path.exists()


@pytest.mark.asyncio
async def test_json_v2_import_is_atomic_idempotent_and_ignores_old_marker(db, tmp_path):
    path = tmp_path / "subscriptions.json"
    path.write_text(json.dumps({"subscriptions": [
        {"session_id": "sess", "type": "tag", "target": "tag", "last_post_id": "00A_000B"},
        {"session_id": "sess", "type": "blog", "target": "blogger"},
    ]}), encoding="utf-8")
    await db.set_config("json_migrated", "1")
    first = await migrate_json_v2(db, str(path))
    second = await migrate_json_v2(db, str(path))
    assert (first.inserted, first.total, first.already_migrated) == (2, 2, False)
    assert second.already_migrated is True
    assert len(await db.list_subscriptions()) == 2
    assert await db.filter_unseen_session("sess", "tag", ["a_b"]) == ["a_b"]
    checkpoints = await db.transaction(lambda conn: conn.execute(
        "SELECT post_id FROM legacy_checkpoints"
    ).fetchall())
    assert checkpoints == [("a_b",)]
    assert path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["insert:1", "marker", "commit"])
async def test_json_v2_fault_rolls_back_data_and_marker(db, tmp_path, stage):
    path = tmp_path / "subscriptions.json"
    path.write_text(json.dumps({"subscriptions": [
        {"session_id": "s", "type": "tag", "target": "one"},
        {"session_id": "s", "type": "tag", "target": "two"},
    ]}), encoding="utf-8")

    def fault(name, conn):
        assert conn.in_transaction
        if name == stage:
            raise RuntimeError(stage)

    with pytest.raises(RuntimeError, match=stage):
        await migrate_json_v2(db, str(path), fault_hook=fault)
    assert await db.list_subscriptions() == []
    assert await db.get_config("json_migration_version") is None
    assert path.exists()


@pytest.mark.asyncio
async def test_json_v2_concurrent_calls_recheck_marker_under_write_lock(db, tmp_path):
    path = tmp_path / "subscriptions.json"
    path.write_text(json.dumps({"subscriptions": [
        {"session_id": "s", "type": "tag", "target": "one"}
    ]}), encoding="utf-8")
    results = await asyncio.gather(
        migrate_json_v2(db, str(path)),
        migrate_json_v2(db, str(path)),
    )
    assert sorted(result.inserted for result in results) == [0, 1]
    assert len(await db.list_subscriptions()) == 1
