from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .author_block import AuthorBlock, is_author_blocked
from .db import LofterDB
from .db_checkpoints import _partition_by_checkpoint, update_checkpoint_watermark
from .errors import SourceSchemaError
from .filter import FilterRule, matches
from .parser import POST_FIELDS, Post
from .post_fields import merge_post_fields, validate_post_evidence
from .post_identity import canonical_post_id, is_canonical_post_url, post_id_from_url
from .post_time import parse_publish_time
from .session_gate import SessionGateRegistry
from .subscription_service import SessionSnapshot, SubscriptionRef

DELIVERY_CAP = 5000
LEASE_SECONDS = 300
SEND_TIMEOUT_SECONDS = 60
MAX_ERROR_LENGTH = 500


@dataclass(frozen=True)
class DeliverySource:
    subscription_id: int
    subscription_revision: int
    type: str
    target: str


@dataclass(frozen=True)
class SourceBatch:
    source: DeliverySource
    posts: tuple[Post, ...]


@dataclass(frozen=True)
class DiscoveryResult:
    admitted: int
    backpressured: int
    passive_seen: int


class ClaimStatus(str, Enum):
    CLAIMED = "claimed"
    BUSY = "busy"
    ALREADY_ACCEPTED = "already_accepted"
    NO_VALID_SOURCE = "no_valid_source"


@dataclass(frozen=True)
class ClaimedDelivery:
    delivery_id: int
    session_id: str
    lease_token: str
    post: Post
    sources: tuple[DeliverySource, ...]


@dataclass(frozen=True)
class ClaimResult:
    status: ClaimStatus
    delivery: ClaimedDelivery | None = None


class DeliveryQueue:
    def __init__(
        self,
        db: LofterDB,
        gates: SessionGateRegistry,
        *,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._db = db
        self._gates = gates
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(24))

    async def session_ids(self) -> list[str]:
        return await self._db.transaction(_pollable_session_ids)

    async def persist_discovery(
        self, snapshot: SessionSnapshot, batches: list[SourceBatch]
    ) -> DiscoveryResult:
        now = int(self._clock())
        async with self._gates.hold(snapshot.session_id):
            return await self._db.transaction(
                lambda conn: _persist_discovery(conn, snapshot, batches, now)
            )

    async def claim_next(self, session_id: str) -> ClaimResult:
        now = int(self._clock())
        async with self._gates.hold(session_id):
            return await self._db.transaction(
                lambda conn: _claim_next(
                    conn, session_id, self._token_factory, now
                )
            )

    async def claim_delivery(
        self, session_id: str, delivery_id: int
    ) -> ClaimResult:
        now = int(self._clock())
        async with self._gates.hold(session_id):
            return await self._db.transaction(
                lambda conn: _claim_delivery(
                    conn,
                    session_id,
                    delivery_id,
                    self._token_factory,
                    now,
                )
            )

    async def ack_success(self, claim: ClaimedDelivery) -> bool:
        now = int(self._clock())
        async with self._gates.hold(claim.session_id):
            return await self._db.transaction(
                lambda conn: _ack_success(conn, claim, now)
            )

    async def release_failure(
        self, claim: ClaimedDelivery, error: BaseException | str
    ) -> bool:
        now = int(self._clock())
        error_type, message = _safe_error(error)
        async with self._gates.hold(claim.session_id):
            return await self._db.transaction(
                lambda conn: _release_failure(
                    conn, claim, now, error_type, message
                )
            )


def encode_post(post: Post) -> str:
    _validate_post(post)
    payload = {
        "version": 1,
        "post": {
            "post_id": post.post_id,
            "title": post.title,
            "summary": post.summary,
            "images": list(post.images),
            "author": post.author,
            "author_username": post.author_username,
            "url": post.url,
            "tags": list(post.tags),
            "publish_time": post.publish_time,
            "content": post.content,
            "source": post.source,
            "completeness": sorted(post.completeness),
            "provenance": dict(post.provenance),
        },
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode_post(payload_json: str) -> Post:
    if not isinstance(payload_json, str):
        raise ValueError("delivery payload must be a string")
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid delivery payload JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "post"}:
        raise ValueError("invalid delivery payload envelope")
    if type(payload["version"]) is not int or payload["version"] != 1:
        raise ValueError("unsupported delivery payload")
    if not isinstance(payload["post"], dict):
        raise ValueError("unsupported delivery payload")
    post = _post_from_payload(payload["post"])
    _validate_post(post)
    return post


def backoff_seconds(attempts: int) -> int:
    if attempts <= 0:
        raise ValueError("attempts must be positive")
    if attempts == 1:
        return 60
    if attempts == 2:
        return 300
    if attempts == 3:
        return 1800
    if attempts == 4:
        return 7200
    return 21600


def source_for_subscription(sub: SubscriptionRef) -> DeliverySource:
    if sub.role != "subscribe" or sub.state != "active":
        raise ValueError("delivery source must be an active subscribe row")
    return DeliverySource(sub.id, sub.revision, sub.type, sub.target)


def _post_from_payload(value: dict) -> Post:
    expected = {
        "post_id", "title", "summary", "images", "author",
        "author_username", "url", "tags", "publish_time", "content",
        "source", "completeness", "provenance",
    }
    if set(value) != expected:
        raise ValueError("invalid delivery post fields")
    _require_strings(value, expected - {"images", "tags", "completeness", "provenance"})
    _require_string_list(value["images"], "images")
    _require_string_list(value["tags"], "tags")
    _require_string_list(value["completeness"], "completeness")
    provenance = value["provenance"]
    if not isinstance(provenance, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in provenance.items()
    ):
        raise ValueError("invalid delivery provenance")
    return Post(
        post_id=value["post_id"], title=value["title"], summary=value["summary"],
        images=value["images"], author=value["author"],
        author_username=value["author_username"], url=value["url"],
        tags=value["tags"], publish_time=value["publish_time"],
        content=value["content"], source=value["source"],
        completeness=frozenset(value["completeness"]), provenance=provenance,
    )


def _require_strings(value: dict, fields: set[str]) -> None:
    if any(not isinstance(value[field], str) for field in fields):
        raise ValueError("invalid delivery string field")


def _require_string_list(value: object, name: str) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"invalid delivery {name}")


def _validate_post(post: Post) -> None:
    string_fields = (
        "post_id", "title", "summary", "author", "author_username",
        "url", "publish_time", "content", "source",
    )
    if any(not isinstance(getattr(post, name), str) for name in string_fields):
        raise ValueError("invalid post string field")
    _require_string_list(post.images, "images")
    _require_string_list(post.tags, "tags")
    if post.post_id != canonical_post_id(post.post_id):
        raise ValueError("non-canonical post ID")
    if not is_canonical_post_url(post.url) or post_id_from_url(post.url) != post.post_id:
        raise ValueError("invalid canonical post URL")
    if parse_publish_time(post.publish_time) is None:
        raise ValueError("invalid publish time")
    if not isinstance(post.completeness, frozenset) or not post.completeness <= POST_FIELDS:
        raise ValueError("invalid completeness")
    if not isinstance(post.provenance, dict) or not all(
        isinstance(key, str) and key in POST_FIELDS and isinstance(value, str)
        for key, value in post.provenance.items()
    ):
        raise ValueError("invalid provenance")
    validate_post_evidence((post,))


def _pollable_session_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("""
        SELECT session_id FROM subscriptions
        WHERE state='active' AND role='subscribe'
        UNION
        SELECT session_id FROM deliveries WHERE status IN ('pending','sending')
        ORDER BY session_id
    """).fetchall()
    return [row[0] for row in rows]


def _persist_discovery(
    conn: sqlite3.Connection,
    snapshot: SessionSnapshot,
    batches: list[SourceBatch],
    now: int,
) -> DiscoveryResult:
    _assert_snapshot(conn, snapshot)
    prepared, passive = _prepare_discovery(conn, snapshot, batches, now)
    capacity = _available_capacity(conn, snapshot.session_id)
    admitted = 0
    backpressured = 0
    for post, sources in prepared:
        outcome = _persist_candidate(
            conn, snapshot, post, sources, now, capacity
        )
        if outcome in {"admitted", "admitted_new"}:
            admitted += 1
        elif outcome == "backpressured":
            backpressured += 1
        elif outcome == "passive":
            passive += len(sources)
        if outcome == "admitted_new":
            capacity -= 1
    return DiscoveryResult(admitted, backpressured, passive)


def _assert_snapshot(conn: sqlite3.Connection, snapshot: SessionSnapshot) -> None:
    revisions = {
        row[0]: row[1] for row in conn.execute("""
            SELECT subscription_type,revision FROM subscription_revisions
            WHERE session_id=?
        """, (snapshot.session_id,)).fetchall()
    }
    policy_row = conn.execute(
        "SELECT policy_generation FROM session_policies WHERE session_id=?",
        (snapshot.session_id,),
    ).fetchone()
    rows = tuple(conn.execute("""
        SELECT id,type,role,target,state,revision FROM subscriptions
        WHERE session_id=? AND state='active' ORDER BY id
    """, (snapshot.session_id,)).fetchall())
    expected_rows = tuple(
        (item.id, item.type, item.role, item.target, item.state, item.revision)
        for item in snapshot.subscriptions
    )
    current = (revisions.get("tag", 0), revisions.get("blog", 0), policy_row[0] if policy_row else 0, rows)
    expected = (snapshot.type_revisions["tag"], snapshot.type_revisions["blog"], snapshot.policy_generation, expected_rows)
    if current != expected:
        raise RuntimeError("subscription snapshot changed")


def _prepare_discovery(
    conn: sqlite3.Connection,
    snapshot: SessionSnapshot,
    batches: list[SourceBatch],
    now: int,
) -> tuple[list[tuple[Post, tuple[DeliverySource, ...]]], int]:
    by_post: dict[str, Post] = {}
    sources_by_post: dict[str, list[DeliverySource]] = {}
    passive = 0
    for batch in batches:
        _validate_batch(snapshot, batch)
        eligible, count = _eligible_source_posts(conn, batch, now)
        passive += count
        for post in eligible:
            existing = by_post.get(post.post_id)
            by_post[post.post_id] = merge_post_fields(existing, post) if existing else post
            sources_by_post.setdefault(post.post_id, []).append(batch.source)
    validate_post_evidence(by_post.values())
    prepared = []
    for post_id, post in by_post.items():
        valid, count = _apply_snapshot_policy(
            conn, snapshot, post, sources_by_post[post_id], now
        )
        passive += count
        if valid:
            prepared.append((post, tuple(valid)))
    prepared.sort(key=lambda item: (_published_at(item[0]), item[0].post_id))
    return prepared, passive


def _validate_batch(snapshot: SessionSnapshot, batch: SourceBatch) -> None:
    expected = {
        (sub.id, sub.revision, sub.type, sub.target)
        for sub in snapshot.subscriptions
        if sub.role == "subscribe" and sub.state == "active"
    }
    source = batch.source
    if (source.subscription_id, source.subscription_revision, source.type, source.target) not in expected:
        raise RuntimeError("invalid discovery source")
    for post in batch.posts:
        _validate_post(post)
    validate_post_evidence(batch.posts)


def _eligible_source_posts(
    conn: sqlite3.Connection, batch: SourceBatch, now: int
) -> tuple[list[Post], int]:
    posts = list(batch.posts)
    if not posts:
        return [], 0
    posts, legacy_applied, passive = _apply_legacy_source(
        conn, batch.source, posts, now
    )
    if not legacy_applied:
        posts, count = _apply_history_source(conn, batch.source, posts, now)
        passive += count
    seen = _seen_ids(conn, batch.source.subscription_id, posts)
    return [post for post in posts if post.post_id not in seen], passive


def _apply_legacy_source(
    conn: sqlite3.Connection,
    source: DeliverySource,
    posts: list[Post],
    now: int,
) -> tuple[list[Post], bool, int]:
    row = conn.execute(
        "SELECT post_id FROM legacy_checkpoints WHERE subscription_id=?",
        (source.subscription_id,),
    ).fetchone()
    if row is not None:
        eligible, suppressed = _partition_posts([row[0]], posts)
        update_checkpoint_watermark(conn, source.subscription_id, row[0])
        _mark_seen_posts(conn, source.subscription_id, suppressed, now)
        conn.execute(
            "DELETE FROM legacy_checkpoints WHERE subscription_id=?",
            (source.subscription_id,),
        )
        return eligible, True, len(suppressed)
    floor = conn.execute("""
        SELECT legacy_post_id_floor FROM subscription_watermarks
        WHERE subscription_id=? AND legacy_post_id_floor IS NOT NULL
    """, (source.subscription_id,)).fetchone()
    if floor is None:
        return posts, False, 0
    eligible, suppressed = _partition_posts([floor[0]], posts)
    _mark_seen_posts(conn, source.subscription_id, suppressed, now)
    return eligible, True, len(suppressed)


def _partition_posts(
    checkpoints: list[str], posts: list[Post]
) -> tuple[list[Post], list[Post]]:
    eligible_ids, _ = _partition_by_checkpoint(
        checkpoints, [post.post_id for post in posts]
    )
    allowed = {canonical_post_id(post_id) for post_id in eligible_ids}
    eligible = [post for post in posts if post.post_id in allowed]
    return eligible, [post for post in posts if post.post_id not in allowed]


def _apply_history_source(
    conn: sqlite3.Connection,
    source: DeliverySource,
    posts: list[Post],
    now: int,
) -> tuple[list[Post], int]:
    count = conn.execute(
        "SELECT COUNT(*) FROM seen_posts WHERE subscription_id=?",
        (source.subscription_id,),
    ).fetchone()[0]
    if count:
        return posts, 0
    row = conn.execute(
        "SELECT history_before FROM subscription_watermarks WHERE subscription_id=?",
        (source.subscription_id,),
    ).fetchone()
    if row is None:
        return posts, 0
    suppressed = [post for post in posts if _published_at(post) <= row[0]]
    _mark_seen_posts(conn, source.subscription_id, suppressed, now)
    suppressed_ids = {post.post_id for post in suppressed}
    return [post for post in posts if post.post_id not in suppressed_ids], len(suppressed)


def _seen_ids(
    conn: sqlite3.Connection, subscription_id: int, posts: list[Post]
) -> set[str]:
    if not posts:
        return set()
    ids = [post.post_id for post in posts]
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT post_id FROM seen_posts WHERE subscription_id=? AND post_id IN ({placeholders})",
        (subscription_id, *ids),
    ).fetchall()
    return {row[0] for row in rows}


def _apply_snapshot_policy(
    conn: sqlite3.Connection,
    snapshot: SessionSnapshot,
    post: Post,
    sources: list[DeliverySource],
    now: int,
) -> tuple[list[DeliverySource], int]:
    if is_author_blocked(post, list(snapshot.author_blocks)):
        _mark_sources_seen(conn, sources, post, now)
        return [], len(sources)
    excludes = _snapshot_excludes(snapshot)
    valid = []
    passive = []
    for source in sources:
        if source.type == "tag" and not matches(post, FilterRule([], excludes)):
            passive.append(source)
        else:
            valid.append(source)
    _mark_sources_seen(conn, passive, post, now)
    return valid, len(passive)


def _snapshot_excludes(snapshot: SessionSnapshot) -> list[str]:
    return [
        sub.target for sub in snapshot.subscriptions
        if sub.type == "tag" and sub.role == "exclude"
    ]


def _available_capacity(conn: sqlite3.Connection, session_id: str) -> int:
    count = conn.execute("""
        SELECT COUNT(*) FROM deliveries
        WHERE session_id=? AND status IN ('pending','sending')
    """, (session_id,)).fetchone()[0]
    return max(0, DELIVERY_CAP - count)


def _persist_candidate(
    conn: sqlite3.Connection,
    snapshot: SessionSnapshot,
    post: Post,
    sources: tuple[DeliverySource, ...],
    now: int,
    capacity: int,
) -> str:
    row = conn.execute("""
        SELECT id,status,payload_json FROM deliveries
        WHERE session_id=? AND post_id=?
    """, (snapshot.session_id, post.post_id)).fetchone()
    if row is None:
        if capacity <= 0:
            return "backpressured"
        delivery_id = _insert_delivery(conn, snapshot.session_id, post, now)
        _upsert_delivery_sources(conn, delivery_id, sources, snapshot.policy_generation, now)
        return "admitted_new"
    delivery_id, status, payload_json = row
    _strengthen_payload(conn, delivery_id, payload_json, post, now)
    _upsert_delivery_sources(conn, delivery_id, sources, snapshot.policy_generation, now)
    if status in {"accepted", "dead"}:
        _mark_sources_seen(conn, sources, post, now)
        return "passive"
    if status == "cancelled":
        if capacity <= 0:
            return "backpressured"
        conn.execute("""
            UPDATE deliveries SET status='pending',next_attempt_at=NULL,
                last_error_type=NULL,last_error=NULL,updated_at=? WHERE id=?
        """, (now, delivery_id))
        return "admitted_new"
    return "admitted"


def _insert_delivery(
    conn: sqlite3.Connection, session_id: str, post: Post, now: int
) -> int:
    published_at = _published_at(post)
    cursor = conn.execute("""
        INSERT INTO deliveries(
            session_id,post_id,status,payload_json,published_at,sort_key,
            next_attempt_at,attempts,created_at,updated_at
        ) VALUES(?,?,'pending',?,?,?,?,0,?,?)
    """, (
        session_id, post.post_id, encode_post(post), published_at,
        f"{published_at:020d}:{post.post_id}", None, now, now,
    ))
    return cursor.lastrowid


def _strengthen_payload(
    conn: sqlite3.Connection,
    delivery_id: int,
    payload_json: str | None,
    post: Post,
    now: int,
) -> None:
    if payload_json is None:
        return
    current = decode_post(payload_json)
    merged = merge_post_fields(current, post)
    encoded = encode_post(merged)
    if encoded != payload_json:
        conn.execute(
            "UPDATE deliveries SET payload_json=?,updated_at=? WHERE id=?",
            (encoded, now, delivery_id),
        )


def _upsert_delivery_sources(
    conn: sqlite3.Connection,
    delivery_id: int,
    sources: tuple[DeliverySource, ...],
    policy_generation: int,
    now: int,
) -> None:
    conn.executemany("""
        INSERT INTO delivery_sources(
            delivery_id,subscription_id,subscription_revision,
            policy_generation,discovered_at
        ) VALUES(?,?,?,?,?)
        ON CONFLICT(delivery_id,subscription_id) DO UPDATE SET
            subscription_revision=excluded.subscription_revision,
            policy_generation=excluded.policy_generation,
            discovered_at=excluded.discovered_at
    """, [
        (delivery_id, source.subscription_id, source.subscription_revision,
         policy_generation, now)
        for source in sources
    ])


def _claim_next(
    conn: sqlite3.Connection,
    session_id: str,
    token_factory: Callable[[], str],
    now: int,
) -> ClaimResult:
    _recover_expired(conn, session_id, now)
    if _has_active_sending(conn, session_id, now):
        return ClaimResult(ClaimStatus.BUSY)
    while True:
        row = conn.execute("""
            SELECT id,status,payload_json FROM deliveries
            WHERE session_id=? AND status='pending'
              AND (next_attempt_at IS NULL OR next_attempt_at<=?)
            ORDER BY published_at ASC,post_id ASC LIMIT 1
        """, (session_id, now)).fetchone()
        if row is None:
            return ClaimResult(ClaimStatus.NO_VALID_SOURCE)
        result = _claim_row(conn, session_id, row, token_factory, now)
        if result.status is not ClaimStatus.NO_VALID_SOURCE:
            return result


def _claim_delivery(
    conn: sqlite3.Connection,
    session_id: str,
    delivery_id: int,
    token_factory: Callable[[], str],
    now: int,
) -> ClaimResult:
    _recover_expired(conn, session_id, now)
    row = conn.execute("""
        SELECT id,status,payload_json FROM deliveries
        WHERE session_id=? AND id=?
    """, (session_id, delivery_id)).fetchone()
    if row is None:
        return ClaimResult(ClaimStatus.NO_VALID_SOURCE)
    if row[1] == "accepted":
        return ClaimResult(ClaimStatus.ALREADY_ACCEPTED)
    if _has_active_sending(conn, session_id, now) and row[1] != "sending":
        return ClaimResult(ClaimStatus.BUSY)
    return _claim_row(
        conn, session_id, row, token_factory, now
    )


def _claim_row(
    conn: sqlite3.Connection,
    session_id: str,
    row: tuple,
    token_factory: Callable[[], str],
    now: int,
) -> ClaimResult:
    delivery_id, status, payload_json = row
    if status != "pending":
        return ClaimResult(ClaimStatus.NO_VALID_SOURCE)
    if payload_json is None:
        _cancel_invalid_payload(
            conn,
            delivery_id,
            now,
            "payload_incomplete",
            "delivery payload is missing",
        )
        return ClaimResult(ClaimStatus.NO_VALID_SOURCE)
    try:
        post = decode_post(payload_json)
    except ValueError as exc:
        _cancel_invalid_payload(
            conn, delivery_id, now, "payload_invalid", str(exc)
        )
        return ClaimResult(ClaimStatus.NO_VALID_SOURCE)
    try:
        sources = _current_valid_sources(conn, delivery_id, session_id, post, now)
    except SourceSchemaError as exc:
        _cancel_invalid_payload(
            conn, delivery_id, now, "payload_incomplete", str(exc)
        )
        return ClaimResult(ClaimStatus.NO_VALID_SOURCE)
    if not sources:
        _cancel_delivery(conn, delivery_id, now)
        return ClaimResult(ClaimStatus.NO_VALID_SOURCE)
    token = token_factory()
    conn.execute("""
        UPDATE deliveries SET status='sending',lease_token=?,lease_until=?,
            updated_at=? WHERE id=? AND status='pending'
    """, (token, now + LEASE_SECONDS, now, delivery_id))
    claim = ClaimedDelivery(delivery_id, session_id, token, post, tuple(sources))
    return ClaimResult(ClaimStatus.CLAIMED, claim)


def _recover_expired(
    conn: sqlite3.Connection, session_id: str, now: int
) -> None:
    rows = conn.execute("""
        SELECT id,attempts FROM deliveries
        WHERE session_id=? AND status='sending' AND lease_until<=?
    """, (session_id, now)).fetchall()
    for delivery_id, attempts in rows:
        _set_failed_state(
            conn, delivery_id, attempts + 1, now,
            "lease_expired", "delivery lease expired",
        )


def _has_active_sending(
    conn: sqlite3.Connection, session_id: str, now: int
) -> bool:
    return conn.execute("""
        SELECT 1 FROM deliveries WHERE session_id=? AND status='sending'
          AND lease_until>? LIMIT 1
    """, (session_id, now)).fetchone() is not None


def _current_valid_sources(
    conn: sqlite3.Connection,
    delivery_id: int,
    session_id: str,
    post: Post,
    now: int,
) -> list[DeliverySource]:
    rows = conn.execute("""
        SELECT s.id,ds.subscription_revision,s.type,s.target,s.revision
        FROM delivery_sources ds JOIN subscriptions s ON s.id=ds.subscription_id
        WHERE ds.delivery_id=? AND s.session_id=? AND s.role='subscribe'
          AND s.state='active'
        ORDER BY s.id
    """, (delivery_id, session_id)).fetchall()
    sources = [
        DeliverySource(row[0], row[1], row[2], row[3])
        for row in rows if row[1] == row[4]
    ]
    blocks = _current_blocks(conn, session_id)
    if is_author_blocked(post, blocks):
        _mark_sources_seen(conn, sources, post, now)
        return []
    excludes = _current_excludes(conn, session_id)
    valid = []
    for source in sources:
        if source.type == "tag" and not matches(post, FilterRule([], excludes)):
            _mark_seen_posts(conn, source.subscription_id, [post], now)
        else:
            valid.append(source)
    return valid


def _current_blocks(conn: sqlite3.Connection, session_id: str) -> list[AuthorBlock]:
    rows = conn.execute("""
        SELECT session_id,kind,value,display,created_at FROM author_blocks
        WHERE session_id=?
    """, (session_id,)).fetchall()
    return [AuthorBlock(*row) for row in rows]


def _current_excludes(conn: sqlite3.Connection, session_id: str) -> list[str]:
    return [row[0] for row in conn.execute("""
        SELECT target FROM subscriptions WHERE session_id=? AND type='tag'
          AND role='exclude' AND state='active'
    """, (session_id,)).fetchall()]


def _cancel_invalid_payload(
    conn: sqlite3.Connection,
    delivery_id: int,
    now: int,
    error_type: str,
    message: str,
) -> None:
    conn.execute("""
        UPDATE deliveries SET status='cancelled',lease_token=NULL,
            lease_until=NULL,next_attempt_at=NULL,
            last_error_type=?,last_error=?,updated_at=?
        WHERE id=?
    """, (error_type, message[:MAX_ERROR_LENGTH], now, delivery_id))


def _cancel_delivery(conn: sqlite3.Connection, delivery_id: int, now: int) -> None:
    conn.execute("""
        UPDATE deliveries SET status='cancelled',lease_token=NULL,
            lease_until=NULL,next_attempt_at=NULL,updated_at=? WHERE id=?
    """, (now, delivery_id))


def _ack_success(
    conn: sqlite3.Connection, claim: ClaimedDelivery, now: int
) -> bool:
    row = conn.execute("""
        SELECT payload_json FROM deliveries WHERE id=? AND session_id=?
          AND status='sending' AND lease_token=?
    """, (claim.delivery_id, claim.session_id, claim.lease_token)).fetchone()
    if row is None:
        return False
    post = decode_post(row[0])
    sources = _current_valid_sources(
        conn, claim.delivery_id, claim.session_id, post, now
    )
    _mark_sources_seen(conn, sources, post, now)
    cursor = conn.execute("""
        UPDATE deliveries SET status='accepted',lease_token=NULL,
            lease_until=NULL,next_attempt_at=NULL,last_error_type=NULL,
            last_error=NULL,accepted_at=?,updated_at=?
        WHERE id=? AND status='sending' AND lease_token=?
    """, (now, now, claim.delivery_id, claim.lease_token))
    return cursor.rowcount == 1


def _release_failure(
    conn: sqlite3.Connection,
    claim: ClaimedDelivery,
    now: int,
    error_type: str,
    message: str,
) -> bool:
    row = conn.execute("""
        SELECT attempts FROM deliveries WHERE id=? AND session_id=?
          AND status='sending' AND lease_token=?
    """, (claim.delivery_id, claim.session_id, claim.lease_token)).fetchone()
    if row is None:
        return False
    _set_failed_state(
        conn, claim.delivery_id, row[0] + 1, now, error_type, message
    )
    return True


def _set_failed_state(
    conn: sqlite3.Connection,
    delivery_id: int,
    attempts: int,
    now: int,
    error_type: str,
    message: str,
) -> None:
    if attempts >= 10:
        status = "dead"
        next_attempt_at = None
    else:
        status = "pending"
        next_attempt_at = now + backoff_seconds(attempts)
    conn.execute("""
        UPDATE deliveries SET status=?,lease_token=NULL,lease_until=NULL,
            next_attempt_at=?,attempts=?,last_error_type=?,last_error=?,updated_at=?
        WHERE id=?
    """, (
        status, next_attempt_at, attempts, error_type, message, now, delivery_id,
    ))


def _mark_sources_seen(
    conn: sqlite3.Connection,
    sources: list[DeliverySource] | tuple[DeliverySource, ...],
    post: Post,
    now: int,
) -> None:
    for source in sources:
        _mark_seen_posts(conn, source.subscription_id, [post], now)


def _mark_seen_posts(
    conn: sqlite3.Connection,
    subscription_id: int,
    posts: list[Post],
    now: int,
) -> None:
    conn.executemany("""
        INSERT INTO seen_posts(subscription_id,post_id,published_at,seen_at)
        VALUES(?,?,?,?) ON CONFLICT(subscription_id,post_id) DO NOTHING
    """, [
        (subscription_id, post.post_id, _published_at(post), now)
        for post in posts
    ])


def _published_at(post: Post) -> int:
    value = parse_publish_time(post.publish_time)
    if value is None:
        raise ValueError("invalid publish time")
    return value


def _safe_error(error: BaseException | str) -> tuple[str, str]:
    if isinstance(error, BaseException):
        error_type = type(error).__name__
        message = str(error)
    else:
        error_type = "send_rejected"
        message = error
    message = re.sub(
        r"(?i)(cookie|authorization|token|password)\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        message,
    )
    return error_type[:100], message[:MAX_ERROR_LENGTH]
