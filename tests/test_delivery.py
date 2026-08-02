from __future__ import annotations

import json
from dataclasses import replace

import pytest
import pytest_asyncio

import core.delivery as delivery_module
from core.db import LofterDB
from core.delivery import (
    ClaimStatus,
    DeliveryQueue,
    SourceBatch,
    backoff_seconds,
    decode_post,
    encode_post,
    source_for_subscription,
)
from core.parser import Post
from core.session_gate import SessionGateRegistry
from core.subscription_service import SubscriptionService


class Clock:
    def __init__(self, value: int = 1_000) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += seconds


def make_post(
    post_id: str,
    *,
    publish_time: str = "2024-02-29 12:34:56",
    tags: list[str] | None = None,
    content: str = "",
) -> Post:
    return Post(
        post_id=post_id,
        title=post_id,
        summary="summary",
        images=[f"https://img.example/{post_id}.jpg"],
        author="Author",
        author_username="user",
        url=f"https://user.lofter.com/post/{post_id}",
        tags=tags or ["visible"],
        publish_time=publish_time,
        content=content,
        source="fake",
    )


@pytest_asyncio.fixture
async def db(tmp_path):
    database = LofterDB(str(tmp_path / "delivery.db"))
    await database.initialize()
    yield database
    await database.close()


async def add_subscriptions(db, *entries):
    def insert(conn):
        now = 100
        types = {entry[0] for entry in entries}
        for sub_type in types:
            conn.execute(
                """
                INSERT INTO subscription_revisions(
                    session_id,subscription_type,revision,updated_at
                ) VALUES('session',?,1,?)
                """,
                (sub_type, now),
            )
        conn.execute(
            """
            INSERT INTO session_policies(
                session_id,policy_generation,updated_at
            ) VALUES('session',1,?)
            """,
            (now,),
        )
        ids = []
        for sub_type, role, target in entries:
            cursor = conn.execute(
                """
                INSERT INTO subscriptions(
                    session_id,type,role,target,state,revision,
                    initialized_at,created_at,updated_at
                ) VALUES('session',?,?,?,'active',1,?,?,?)
                """,
                (sub_type, role, target, now, now, now),
            )
            ids.append(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO subscription_watermarks(
                    subscription_id,history_before,updated_at
                ) VALUES(?,0,?)
                """,
                (cursor.lastrowid, now),
            )
        return ids

    return await db.transaction(insert)


async def snapshot_for(db, gates):
    service = SubscriptionService(db, object(), gates)
    return await service.capture_snapshot("session")


def queue_for(db, gates, clock, tokens=None):
    token_values = iter(tokens or [f"token-{index}" for index in range(20)])
    return DeliveryQueue(
        db,
        gates,
        clock=clock,
        token_factory=lambda: next(token_values),
    )


async def delivery_rows(db):
    return await db.transaction(
        lambda conn: conn.execute(
            """
            SELECT post_id,status,payload_json,published_at,sort_key,
                   lease_token,lease_until,next_attempt_at,attempts,
                   last_error_type,last_error,accepted_at
            FROM deliveries ORDER BY published_at,post_id
            """
        ).fetchall()
    )


async def seen_rows(db):
    return await db.transaction(
        lambda conn: conn.execute(
            """
            SELECT s.target,sp.post_id,sp.published_at
            FROM seen_posts sp
            JOIN subscriptions s ON s.id=sp.subscription_id
            ORDER BY s.target,sp.post_id
            """
        ).fetchall()
    )


def test_payload_round_trip_and_strict_version_type():
    post = make_post("a_1", content="body")

    encoded = encode_post(post)
    decoded = decode_post(encoded)

    assert decoded == post
    payload = json.loads(encoded)
    payload["version"] = True
    with pytest.raises(ValueError, match="unsupported"):
        decode_post(json.dumps(payload))


@pytest.mark.parametrize(
    ("attempts", "seconds"),
    [(1, 60), (2, 300), (3, 1800), (4, 7200), (5, 21600), (9, 21600)],
)
def test_backoff_schedule(attempts, seconds):
    assert backoff_seconds(attempts) == seconds


@pytest.mark.asyncio
async def test_discovery_claim_and_ack_use_stable_order_and_actual_source(db):
    gates = SessionGateRegistry()
    clock = Clock()
    await add_subscriptions(
        db,
        ("tag", "subscribe", "A"),
        ("tag", "subscribe", "B"),
    )
    snapshot = await snapshot_for(db, gates)
    source_a, source_b = (
        source_for_subscription(item)
        for item in snapshot.subscriptions
    )
    older = make_post("a_1", publish_time="2024-02-28 12:00:00", tags=["B"])
    newer = make_post("a_2", publish_time="2024-02-29 12:00:00")
    queue = queue_for(db, gates, clock)

    result = await queue.persist_discovery(
        snapshot,
        [SourceBatch(source_a, (newer, older))],
    )

    assert result.admitted == 2
    assert result.backpressured == 0
    source_rows = await db.transaction(
        lambda conn: conn.execute(
            """
            SELECT d.post_id,s.target FROM delivery_sources ds
            JOIN deliveries d ON d.id=ds.delivery_id
            JOIN subscriptions s ON s.id=ds.subscription_id
            ORDER BY d.post_id,s.target
            """
        ).fetchall()
    )
    assert source_rows == [("a_1", "A"), ("a_2", "A")]
    assert source_b.target == "B"

    first = await queue.claim_next("session")
    busy = await queue.claim_next("session")
    assert first.status is ClaimStatus.CLAIMED
    assert first.delivery is not None
    assert first.delivery.post.post_id == "a_1"
    assert busy.status is ClaimStatus.BUSY
    assert await queue.ack_success(first.delivery) is True
    assert await seen_rows(db) == [("A", "a_1", 1709121600)]

    second = await queue.claim_next("session")
    assert second.status is ClaimStatus.CLAIMED
    assert second.delivery is not None
    assert second.delivery.post.post_id == "a_2"


@pytest.mark.asyncio
async def test_stale_snapshot_rolls_back_delivery_and_seen(db):
    gates = SessionGateRegistry()
    clock = Clock()
    await add_subscriptions(db, ("tag", "subscribe", "A"))
    snapshot = await snapshot_for(db, gates)
    source = source_for_subscription(snapshot.subscriptions[0])
    await db.mutate_author_blocks(
        "session", [("name", "other", "Other")], True
    )

    with pytest.raises(RuntimeError, match="snapshot changed"):
        await queue_for(db, gates, clock).persist_discovery(
            snapshot, [SourceBatch(source, (make_post("a_1"),))]
        )

    assert await delivery_rows(db) == []
    assert await seen_rows(db) == []


@pytest.mark.asyncio
async def test_admission_backpressure_keeps_overflow_unseen(db, monkeypatch):
    monkeypatch.setattr(delivery_module, "DELIVERY_CAP", 2)
    gates = SessionGateRegistry()
    clock = Clock()
    await add_subscriptions(db, ("tag", "subscribe", "A"))
    snapshot = await snapshot_for(db, gates)
    source = source_for_subscription(snapshot.subscriptions[0])
    posts = (
        make_post("a_3", publish_time="2024-03-03 00:00:00"),
        make_post("a_1", publish_time="2024-03-01 00:00:00"),
        make_post("a_2", publish_time="2024-03-02 00:00:00"),
    )

    result = await queue_for(db, gates, clock).persist_discovery(
        snapshot, [SourceBatch(source, posts)]
    )

    assert result.admitted == 2
    assert result.backpressured == 1
    assert [row[0] for row in await delivery_rows(db)] == ["a_1", "a_2"]
    assert await seen_rows(db) == []


@pytest.mark.asyncio
async def test_failure_backoff_and_stale_token_are_side_effect_free(db):
    gates = SessionGateRegistry()
    clock = Clock()
    await add_subscriptions(db, ("tag", "subscribe", "A"))
    snapshot = await snapshot_for(db, gates)
    source = source_for_subscription(snapshot.subscriptions[0])
    queue = queue_for(db, gates, clock, ["real", "retry"])
    await queue.persist_discovery(
        snapshot, [SourceBatch(source, (make_post("a_1"),))]
    )
    claimed = await queue.claim_next("session")
    assert claimed.delivery is not None
    stale = replace(claimed.delivery, lease_token="stale")

    assert await queue.ack_success(stale) is False
    assert await queue.release_failure(stale, "ignored") is False
    row = (await delivery_rows(db))[0]
    assert row[1] == "sending"
    assert row[8] == 0
    assert await seen_rows(db) == []

    assert await queue.release_failure(
        claimed.delivery,
        RuntimeError("cookie=secret authorization:bearer"),
    ) is True
    row = (await delivery_rows(db))[0]
    assert row[1] == "pending"
    assert row[7] == 1060
    assert row[8] == 1
    assert "secret" not in row[10]
    assert await queue.claim_next("session") == delivery_module.ClaimResult(
        ClaimStatus.NO_VALID_SOURCE
    )

    clock.advance(60)
    retried = await queue.claim_next("session")
    assert retried.status is ClaimStatus.CLAIMED
    assert retried.delivery is not None
    assert retried.delivery.lease_token == "retry"


@pytest.mark.asyncio
async def test_expired_lease_recovers_with_backoff(db):
    gates = SessionGateRegistry()
    clock = Clock()
    await add_subscriptions(db, ("tag", "subscribe", "A"))
    snapshot = await snapshot_for(db, gates)
    source = source_for_subscription(snapshot.subscriptions[0])
    queue = queue_for(db, gates, clock, ["first", "second"])
    await queue.persist_discovery(
        snapshot, [SourceBatch(source, (make_post("a_1"),))]
    )
    first = await queue.claim_next("session")
    assert first.status is ClaimStatus.CLAIMED

    clock.advance(301)
    assert (await queue.claim_next("session")).status is ClaimStatus.NO_VALID_SOURCE
    row = (await delivery_rows(db))[0]
    assert row[1] == "pending"
    assert row[7] == 1361
    assert row[8] == 1

    clock.advance(60)
    second = await queue.claim_next("session")
    assert second.status is ClaimStatus.CLAIMED
    assert second.delivery is not None
    assert second.delivery.lease_token == "second"


@pytest.mark.asyncio
async def test_tenth_failure_moves_delivery_to_dead(db):
    gates = SessionGateRegistry()
    clock = Clock()
    await add_subscriptions(db, ("tag", "subscribe", "A"))
    snapshot = await snapshot_for(db, gates)
    source = source_for_subscription(snapshot.subscriptions[0])
    queue = queue_for(db, gates, clock)
    await queue.persist_discovery(
        snapshot, [SourceBatch(source, (make_post("a_1"),))]
    )
    await db.transaction(
        lambda conn: conn.execute(
            "UPDATE deliveries SET attempts=9 WHERE post_id='a_1'"
        )
    )
    claim = await queue.claim_next("session")
    assert claim.delivery is not None

    assert await queue.release_failure(claim.delivery, "rejected") is True
    row = (await delivery_rows(db))[0]
    assert row[1] == "dead"
    assert row[7] is None
    assert row[8] == 10


@pytest.mark.asyncio
async def test_accepted_null_payload_is_never_reclaimed_and_marks_new_source_seen(db):
    gates = SessionGateRegistry()
    clock = Clock()
    sub_ids = await add_subscriptions(db, ("tag", "subscribe", "A"))
    await db.transaction(
        lambda conn: conn.execute(
            """
            INSERT INTO deliveries(
                session_id,post_id,status,payload_json,published_at,sort_key,
                attempts,created_at,updated_at,accepted_at
            ) VALUES('session','a_1','accepted',NULL,1,'x',0,1,1,1)
            """
        )
    )
    snapshot = await snapshot_for(db, gates)
    source = source_for_subscription(snapshot.subscriptions[0])
    queue = queue_for(db, gates, clock)

    result = await queue.persist_discovery(
        snapshot, [SourceBatch(source, (make_post("a_1"),))]
    )

    assert result.admitted == 0
    assert result.passive_seen == 1
    row = (await delivery_rows(db))[0]
    assert row[1] == "accepted"
    assert row[2] is None
    assert await seen_rows(db) == [("A", "a_1", 1709210096)]
    delivery_id = await db.transaction(
        lambda conn: conn.execute(
            "SELECT id FROM deliveries WHERE post_id='a_1'"
        ).fetchone()[0]
    )
    assert (
        await queue.claim_delivery("session", delivery_id)
    ).status is ClaimStatus.ALREADY_ACCEPTED
    assert (await queue.claim_next("session")).status is ClaimStatus.NO_VALID_SOURCE
    assert sub_ids


@pytest.mark.asyncio
async def test_deleted_source_cancels_pending_delivery(db):
    gates = SessionGateRegistry()
    clock = Clock()
    await add_subscriptions(db, ("tag", "subscribe", "A"))
    snapshot = await snapshot_for(db, gates)
    source = source_for_subscription(snapshot.subscriptions[0])
    queue = queue_for(db, gates, clock)
    await queue.persist_discovery(
        snapshot, [SourceBatch(source, (make_post("a_1"),))]
    )
    await db.remove_runtime_subscription("session", "tag", "A", "subscribe")

    result = await queue.claim_next("session")

    assert result.status is ClaimStatus.NO_VALID_SOURCE
    assert (await delivery_rows(db))[0][1] == "cancelled"
    assert await seen_rows(db) == []


@pytest.mark.asyncio
async def test_claim_next_cancels_invalid_payload_and_claims_next_due(db):
    gates = SessionGateRegistry()
    clock = Clock()
    await add_subscriptions(db, ("tag", "subscribe", "A"))
    snapshot = await snapshot_for(db, gates)
    source = source_for_subscription(snapshot.subscriptions[0])
    queue = queue_for(db, gates, clock, ["valid-token"])
    await queue.persist_discovery(
        snapshot,
        [
            SourceBatch(
                source,
                (
                    make_post("a_1", publish_time="2024-03-01 00:00:00"),
                    make_post("a_2", publish_time="2024-03-02 00:00:00"),
                ),
            )
        ],
    )
    await db.transaction(
        lambda conn: conn.execute(
            "UPDATE deliveries SET payload_json='not-json' WHERE post_id='a_1'"
        )
    )

    result = await queue.claim_next("session")

    assert result.status is ClaimStatus.CLAIMED
    assert result.delivery is not None
    assert result.delivery.post.post_id == "a_2"
    assert result.delivery.lease_token == "valid-token"
    rows = await delivery_rows(db)
    assert rows[0][1] == "cancelled"
    assert rows[0][9] == "payload_invalid"
    assert rows[1][1] == "sending"
