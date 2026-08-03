from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import LofterDB
from .db_repository import _add_subscription
from .post_identity import canonical_post_id

JSON_MIGRATION_VERSION = "2"


class JsonMigrationError(ValueError):
    pass


@dataclass(frozen=True)
class JsonSubscription:
    session_id: str
    type: str
    role: str
    target: str
    last_post_id: str | None


@dataclass(frozen=True)
class JsonMigrationResult:
    source_found: bool
    already_migrated: bool
    inserted: int
    total: int


async def migrate_json_v2(
    db: LofterDB,
    json_path: str,
    *,
    fault_hook=None,
) -> JsonMigrationResult:
    if await db.get_config("json_migration_version") == JSON_MIGRATION_VERSION:
        return JsonMigrationResult(False, True, 0, 0)
    if not os.path.exists(json_path):
        return JsonMigrationResult(False, False, 0, 0)
    subscriptions = load_json_subscriptions(json_path)

    def _migrate(conn: sqlite3.Connection) -> JsonMigrationResult:
        marker = conn.execute(
            "SELECT value FROM config WHERE key='json_migration_version'"
        ).fetchone()
        if marker and marker[0] == JSON_MIGRATION_VERSION:
            return JsonMigrationResult(True, True, 0, len(subscriptions))
        inserted = _insert_subscriptions(conn, subscriptions, fault_hook)
        if fault_hook:
            fault_hook("marker", conn)
        conn.execute("""
            INSERT INTO config(key,value) VALUES('json_migration_version',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (JSON_MIGRATION_VERSION,))
        if fault_hook:
            fault_hook("commit", conn)
        return JsonMigrationResult(True, False, inserted, len(subscriptions))

    return await db.transaction(_migrate)


def _insert_subscriptions(
    conn: sqlite3.Connection,
    subscriptions: tuple[JsonSubscription, ...],
    fault_hook,
) -> int:
    inserted = 0
    for index, item in enumerate(subscriptions):
        if fault_hook:
            fault_hook(f"insert:{index}", conn)
        inserted += _add_subscription(
            conn, item.session_id, item.type, item.target, item.role
        )
        if item.last_post_id:
            _insert_last_post(conn, item)
    return inserted


def load_json_subscriptions(json_path: str) -> tuple[JsonSubscription, ...]:
    try:
        raw = Path(json_path).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JsonMigrationError(f"invalid subscriptions JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise JsonMigrationError("subscriptions JSON root must be an object")
    unknown = set(data) - {"subscriptions"}
    if unknown:
        raise JsonMigrationError(f"unknown subscriptions JSON keys: {sorted(unknown)}")
    items = data.get("subscriptions", [])
    if not isinstance(items, list):
        raise JsonMigrationError("subscriptions must be a list")
    return tuple(_validate_subscription(item, index) for index, item in enumerate(items))


def _validate_subscription(item: Any, index: int) -> JsonSubscription:
    if not isinstance(item, dict):
        raise JsonMigrationError(f"subscription {index} must be an object")
    allowed = {"session_id", "type", "target", "role", "last_post_id"}
    unknown = set(item) - allowed
    if unknown:
        raise JsonMigrationError(f"subscription {index} has unknown keys: {sorted(unknown)}")
    session_id = _required_string(item, "session_id", index)
    sub_type = _required_string(item, "type", index)
    target = _required_string(item, "target", index)
    role = item.get("role", "subscribe")
    if type(role) is not str or role not in {"subscribe", "exclude"}:
        raise JsonMigrationError(f"subscription {index} has invalid role")
    if sub_type not in {"tag", "blog"}:
        raise JsonMigrationError(f"subscription {index} has invalid type")
    if sub_type == "blog" and role != "subscribe":
        raise JsonMigrationError(f"subscription {index} has invalid blog role")
    last_post_id = item.get("last_post_id")
    if last_post_id is not None and type(last_post_id) is not str:
        raise JsonMigrationError(f"subscription {index} has invalid last_post_id")
    return JsonSubscription(session_id, sub_type, role, target, last_post_id or None)


def _insert_last_post(conn: sqlite3.Connection, item: JsonSubscription) -> None:
    row = conn.execute("""
        SELECT id FROM subscriptions
        WHERE session_id=? AND type=? AND role=? AND target=?
    """, (item.session_id, item.type, item.role, item.target)).fetchone()
    if row is None or item.role != "subscribe":
        return
    now = conn.execute("SELECT CAST(strftime('%s','now') AS INTEGER)").fetchone()[0]
    canonical = canonical_post_id(item.last_post_id)
    conn.execute("""
        INSERT INTO legacy_checkpoints(subscription_id,post_id,created_at) VALUES(?,?,?)
        ON CONFLICT(subscription_id) DO UPDATE SET
            post_id=excluded.post_id,created_at=excluded.created_at
    """, (row[0], canonical, now))
    if _is_numeric_checkpoint(canonical):
        conn.execute("""
            UPDATE subscription_watermarks
            SET legacy_post_id_floor=?,updated_at=? WHERE subscription_id=?
        """, (canonical, now, row[0]))


def _is_numeric_checkpoint(post_id: str) -> bool:
    if post_id.isdigit():
        return True
    parts = post_id.split("_")
    return len(parts) == 2 and all(
        part and all(char in "0123456789abcdef" for char in part) for part in parts
    )


def _required_string(item: dict, key: str, index: int) -> str:
    value = item.get(key)
    if type(value) is not str or not value.strip():
        raise JsonMigrationError(f"subscription {index} has invalid {key}")
    return value
