from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from core.db import LofterDB
from core.db_json_migration import migrate_json_v2
from core.db_migrations import SchemaVersionError, initialize_schema
from tests.db_fixtures import create_legacy_db


class CommitFailConnection(sqlite3.Connection):
    fail_commit = False

    def commit(self) -> None:
        if self.fail_commit:
            raise sqlite3.OperationalError("injected commit failure")
        super().commit()


def migration_stages(db_path: str) -> list[str]:
    stages = []
    conn = sqlite3.connect(db_path)
    initialize_schema(conn, lambda name, _: stages.append(name))
    conn.close()
    return stages


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2, 3, 4])
async def test_every_executed_migration_stage_rolls_back(tmp_path, version):
    probe = str(tmp_path / f"probe-v{version}.db")
    create_legacy_db(probe, version)
    stages = migration_stages(probe)
    assert len(stages) == len(set(stages))

    for index, stage in enumerate(stages):
        path = str(tmp_path / f"v{version}-{index}.db")
        create_legacy_db(path, version)

        def fail(name, conn, target=stage):
            assert conn.in_transaction
            if name == target:
                raise RuntimeError(target)

        database = LofterDB(path, migration_fault_hook=fail)
        with pytest.raises(RuntimeError, match=stage):
            await database.initialize()
        await database.close()
        assert_legacy_schema_unchanged(path, version)


def assert_legacy_schema_unchanged(path: str, version: int) -> None:
    conn = sqlite3.connect(path)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        assert not any(name.startswith("_v5_old_") for name in tables)
        marker = conn.execute(
            "SELECT value FROM config WHERE key='schema_version'"
        ).fetchone()
        assert marker == (str(version),) if version > 1 else marker is None
    finally:
        conn.close()


def test_real_schema_commit_failure_rolls_back(tmp_path):
    path = str(tmp_path / "commit.db")
    create_legacy_db(path, 4)
    conn = sqlite3.connect(path, factory=CommitFailConnection)
    conn.fail_commit = True
    with pytest.raises(sqlite3.OperationalError, match="commit failure"):
        initialize_schema(conn)
    assert conn.in_transaction is False
    conn.close()
    assert_legacy_schema_unchanged(path, 4)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filter_rule",
    [
        "{bad",
        "[]",
        json.dumps({"search_tags": "tag"}),
        json.dumps({"include_tags": {"tag": True}}),
        json.dumps({"exclude_tags": [""]}),
        json.dumps({"exclude_tags": [" \t\n"]}),
        json.dumps({"or_tag_groups": "group"}),
        json.dumps({"or_tag_groups": ["tag"]}),
        json.dumps({"or_tag_groups": [["tag", 1]]}),
    ],
)
async def test_invalid_v1_filter_rule_rolls_back_entire_migration(tmp_path, filter_rule):
    path = str(tmp_path / "invalid-filter.db")
    create_legacy_db(path, 1, filter_rule)
    database = LofterDB(path)
    with pytest.raises(SchemaVersionError, match="filter_rule"):
        await database.initialize()
    await database.close()
    assert_legacy_schema_unchanged(path, 1)


@pytest.mark.asyncio
async def test_malformed_legacy_constraints_fail_closed(tmp_path):
    path = str(tmp_path / "malformed-v4.db")
    conn = sqlite3.connect(path)
    statements = (
        "CREATE TABLE config(key TEXT,value TEXT)",
        "CREATE TABLE subscriptions(id INTEGER,session_id TEXT,type TEXT,role TEXT,target TEXT,created_at INTEGER)",
        "CREATE TABLE seen_posts(session_id TEXT,type TEXT,post_id TEXT,seen_at INTEGER)",
        "CREATE TABLE sent_posts(session_id TEXT,post_id TEXT,sent_at INTEGER)",
        "CREATE TABLE count_conditions(name TEXT,expression TEXT,updated_at INTEGER)",
        "CREATE TABLE author_blocks(session_id TEXT,kind TEXT,value TEXT,display TEXT,created_at INTEGER)",
    )
    for statement in statements:
        conn.execute(statement)
    conn.execute("INSERT INTO config VALUES('schema_version','4')")
    conn.commit()
    conn.close()

    database = LofterDB(path)
    with pytest.raises(SchemaVersionError, match="malformed legacy"):
        await database.initialize()
    await database.close()
    assert_legacy_schema_unchanged(path, 4)


def _legacy_subscription_ddl(version: int, variant: str) -> tuple[str, str | None]:
    if version == 1:
        type_column = "type TEXT NOT NULL"
        role_column = ""
        keys = "session_id,type,target"
        filter_column = "filter_rule TEXT DEFAULT NULL,"
    else:
        type_check = "CHECK(type IN ('tag','blog'))"
        if variant == "changed-check":
            type_check = "CHECK(type IN ('tag','blog','other'))"
        elif variant == "comment-check":
            type_check = "/* CHECK(type IN ('tag','blog')) */"
        type_column = f"type TEXT NOT NULL {type_check}"
        role_column = "role TEXT NOT NULL DEFAULT 'subscribe' " \
            "CHECK(role IN ('subscribe','exclude')),"
        keys = "session_id,type,role,target"
        filter_column = ""
    target = "target TEXT COLLATE NOCASE NOT NULL" if variant == "collation" else "target TEXT NOT NULL"
    unique = f"UNIQUE({keys})"
    external = None
    if variant in {"partial", "origin"}:
        unique = ""
        where = " WHERE target <> ''" if variant == "partial" else ""
        external = f"CREATE UNIQUE INDEX legacy_unique ON subscriptions({keys}){where}"
    elif variant == "desc":
        unique = f"UNIQUE({keys.rsplit(',', 1)[0]},target DESC)"
    elif variant == "order":
        unique = f"UNIQUE({','.join(reversed(keys.split(',')))})"
    extra_check = ",CHECK(length(target)>0)" if variant == "extra-check" else ""
    comma = "," if unique else ""
    sql = f"""
        CREATE TABLE subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            {type_column},
            {role_column}
            {target},
            {filter_column}
            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
            {comma}{unique}{extra_check}
        )
    """
    return sql, external


def _replace_legacy_subscriptions(path: str, version: int, variant: str) -> None:
    conn = sqlite3.connect(path)
    columns = "id,session_id,type,target,filter_rule,created_at" if version == 1 else (
        "id,session_id,type,role,target,created_at"
    )
    try:
        conn.execute("ALTER TABLE subscriptions RENAME TO source_subscriptions")
        ddl, external = _legacy_subscription_ddl(version, variant)
        conn.execute(ddl)
        conn.execute(f"INSERT INTO subscriptions({columns}) SELECT {columns} FROM source_subscriptions")
        conn.execute("DROP TABLE source_subscriptions")
        if external:
            conn.execute(external)
        conn.commit()
    finally:
        conn.close()


def _schema_snapshot(path: str) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("""
            SELECT type,name,tbl_name,sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name
        """).fetchall()
    finally:
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "variant"),
    [
        (version, variant)
        for version in (1, 2, 3, 4)
        for variant in (
            "partial", "origin", "desc", "order", "collation", "extra-check",
            "changed-check", "comment-check",
        )
        if version > 1 or variant not in {"changed-check", "comment-check"}
    ],
)
async def test_legacy_unique_and_checks_fail_closed_before_migration(tmp_path, version, variant):
    path = str(tmp_path / f"v{version}-{variant}.db")
    create_legacy_db(path, version)
    _replace_legacy_subscriptions(path, version, variant)
    before = _schema_snapshot(path)

    database = LofterDB(path)
    with pytest.raises(SchemaVersionError, match="malformed legacy"):
        await database.initialize()
    await database.close()

    assert _schema_snapshot(path) == before
    assert_legacy_schema_unchanged(path, version)


@pytest.mark.asyncio
async def test_non_target_unique_and_foreign_key_errors_propagate(db_path):
    database = LofterDB(db_path)
    await database.initialize()
    await database.add_subscription("session", "tag", "source")
    sub_id = await database.get_subscription_id("session", "tag", "source")

    with pytest.raises(sqlite3.IntegrityError):
        await database.transaction(lambda conn: conn.execute(
            "INSERT INTO subscriptions(id,session_id,type,role,target,state,revision,created_at,updated_at) "
            "VALUES(?,?,'tag','subscribe','other','active',1,1,1)",
            (sub_id, "other-session"),
        ))
    with pytest.raises(sqlite3.IntegrityError):
        await database.transaction(lambda conn: conn.execute(
            "INSERT INTO seen_posts(subscription_id,post_id,published_at,seen_at) VALUES(999,'x',1,1)"
        ))
    assert database._conn.in_transaction is False
    await database.close()


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "errors.db")


@pytest.mark.asyncio
async def test_json_v2_competing_database_connections_are_atomic(tmp_path):
    db_path = str(tmp_path / "shared.db")
    first = LofterDB(db_path)
    second = LofterDB(db_path)
    await first.initialize()
    await second.initialize()
    json_path = tmp_path / "subscriptions.json"
    json_path.write_text(json.dumps({"subscriptions": [
        {"session_id": "session", "type": "tag", "target": "one"},
        {"session_id": "session", "type": "tag", "target": "two"},
    ]}), encoding="utf-8")
    try:
        results = await asyncio.gather(
            migrate_json_v2(first, str(json_path)),
            migrate_json_v2(second, str(json_path)),
        )
        assert sorted(result.inserted for result in results) == [0, 2]
        assert len(await first.list_subscriptions()) == 2
        assert await first.get_config("json_migration_version") == "2"
        assert json_path.exists()
    finally:
        await first.close()
        await second.close()
