from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

import core.scheduler as scheduler_module
from core.author_block import AuthorBlockStorage
from core.db import LofterDB
from core.delivery import DeliveryQueue, SourceBatch, source_for_subscription
from core.parser import Post
from core.formatter import format_post
from core.scheduler import SubscriptionScheduler
from core.session_gate import SessionGateRegistry
from core.storage import SubscriptionStorage
from core.subscription_service import SubscriptionService


@pytest_asyncio.fixture
async def db(tmp_path):
    database = LofterDB(str(tmp_path / "scheduler-delivery.db"))
    await database.initialize()
    yield database
    await database.close()


def _post(index: int, *, second: int | None = None) -> Post:
    post_id = f"1a_{32 + index:x}"
    value = index if second is None else second
    return Post(
        post_id=post_id,
        title=f"post-{index}",
        summary="summary",
        content="content",
        images=[],
        author="Author",
        author_username="user",
        url=f"https://user.lofter.com/post/{post_id}",
        tags=["tag"],
        publish_time=f"2099-01-01 00:00:{value:02d}",
        source="fake",
    )


async def _scheduler(db, send_func, *subscriptions):
    storage = SubscriptionStorage(db)
    for sub_type, target in subscriptions:
        await storage.add("session", sub_type, target)
    gates = SessionGateRegistry()
    source = AsyncMock()
    service = SubscriptionService(db, source, gates)
    queue = DeliveryQueue(db, gates)
    scheduler = SubscriptionScheduler(
        storage,
        source,
        db,
        send_func,
        block_storage=AuthorBlockStorage(db, gates),
        gates=gates,
        subscription_service=service,
        delivery_queue=queue,
    )
    snapshot = await service.capture_snapshot("session")
    return scheduler, snapshot


async def _delivery_states(db):
    return await db.transaction(
        lambda conn: conn.execute(
            """
            SELECT post_id,status,attempts FROM deliveries
            ORDER BY published_at,post_id
            """
        ).fetchall()
    )


@pytest.mark.asyncio
async def test_poll_all_persists_eight_then_drains_five_and_three(db):
    send = AsyncMock(return_value=True)
    scheduler, snapshot = await _scheduler(db, send, ("tag", "tag"))
    source = source_for_subscription(snapshot.subscriptions[0])
    posts = tuple(_post(index) for index in range(8))
    fetch = AsyncMock(side_effect=[
        [SourceBatch(source, posts)],
        [],
    ])

    with patch("core.scheduler._fetch_snapshot_batches", fetch):
        await scheduler._poll_all()
        assert await _delivery_states(db) == [
            *((post.post_id, "accepted", 0) for post in posts[:5]),
            *((post.post_id, "pending", 0) for post in posts[5:]),
        ]
        assert send.await_count == 5

        await scheduler._poll_all()

    assert send.await_count == 8
    assert await _delivery_states(db) == [
        (post.post_id, "accepted", 0) for post in posts
    ]


@pytest.mark.asyncio
async def test_poll_all_shares_five_send_slots_between_tag_and_blog(db):
    send = AsyncMock(return_value=True)
    scheduler, snapshot = await _scheduler(
        db, send, ("tag", "tag"), ("blog", "user")
    )
    sources = {sub.type: source_for_subscription(sub) for sub in snapshot.subscriptions}
    tag_posts = tuple(_post(index) for index in range(3))
    blog_posts = tuple(_post(index + 3) for index in range(3))

    with patch(
        "core.scheduler._fetch_snapshot_batches",
        AsyncMock(return_value=[
            SourceBatch(sources["tag"], tag_posts),
            SourceBatch(sources["blog"], blog_posts),
        ]),
    ):
        await scheduler._poll_all()

    assert send.await_count == 5
    assert [row[1] for row in await _delivery_states(db)] == [
        "accepted", "accepted", "accepted", "accepted", "accepted", "pending",
    ]
    sent_text = "\n".join(
        format_post(call.args[1], header=call.args[2])
        for call in send.await_args_list
    )
    assert sent_text.count("【标签") == 3
    assert sent_text.count("【博主") == 2
    assert [call.args[3] for call in send.await_args_list] == [
        frozenset({"tag"}),
        frozenset({"tag"}),
        frozenset({"tag"}),
        frozenset({"blog"}),
        frozenset({"blog"}),
    ]


@pytest.mark.asyncio
async def test_poll_all_passes_full_post_header_and_all_source_types(db):
    send = AsyncMock(return_value=True)
    scheduler, snapshot = await _scheduler(
        db, send, ("tag", "tag"), ("blog", "user")
    )
    sources = {sub.type: source_for_subscription(sub) for sub in snapshot.subscriptions}
    post = _post(0)

    with patch(
        "core.scheduler._fetch_snapshot_batches",
        AsyncMock(return_value=[
            SourceBatch(sources["tag"], (post,)),
            SourceBatch(sources["blog"], (post,)),
        ]),
    ):
        await scheduler._poll_all()

    send.assert_awaited_once()
    session_id, sent_post, header, source_types = send.await_args.args
    assert session_id == "session"
    assert isinstance(sent_post, Post)
    assert sent_post.post_id == post.post_id
    assert sent_post.title == post.title
    assert sent_post.summary == post.summary
    assert sent_post.content == post.content
    assert sent_post.images == post.images
    assert sent_post.author == post.author
    assert sent_post.author_username == post.author_username
    assert sent_post.url == post.url
    assert sent_post.tags == post.tags
    assert sent_post.publish_time == post.publish_time
    assert header == "【标签「tag」有新内容】"
    assert source_types == frozenset({"tag", "blog"})
    assert isinstance(source_types, frozenset)


@pytest.mark.asyncio
async def test_poll_all_acks_each_post_and_stops_after_failure(db):
    send = AsyncMock(side_effect=[True, False])
    scheduler, snapshot = await _scheduler(db, send, ("tag", "tag"))
    source = source_for_subscription(snapshot.subscriptions[0])
    posts = tuple(_post(index) for index in range(4))

    with patch(
        "core.scheduler._fetch_snapshot_batches",
        AsyncMock(return_value=[SourceBatch(source, posts)]),
    ):
        await scheduler._poll_all()

    assert send.await_count == 2
    assert await _delivery_states(db) == [
        (posts[0].post_id, "accepted", 0),
        (posts[1].post_id, "pending", 1),
        (posts[2].post_id, "pending", 0),
        (posts[3].post_id, "pending", 0),
    ]
    assert await db.filter_unseen_session(
        "session", "tag", [post.post_id for post in posts]
    ) == [post.post_id for post in posts[1:]]


@pytest.mark.asyncio
async def test_poll_all_timeout_keeps_sending_lease(db, monkeypatch):
    release = asyncio.Event()

    async def send_func(session_id, post, header, source_types):
        await release.wait()
        return True

    scheduler, snapshot = await _scheduler(db, send_func, ("tag", "tag"))
    source = source_for_subscription(snapshot.subscriptions[0])
    monkeypatch.setattr(scheduler_module, "SEND_TIMEOUT_SECONDS", 0)

    with patch(
        "core.scheduler._fetch_snapshot_batches",
        AsyncMock(return_value=[SourceBatch(source, (_post(0),))]),
    ):
        await scheduler._poll_all()

    assert (await _delivery_states(db))[0][1:] == ("sending", 0)
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_poll_all_cancellation_keeps_sending_and_propagates(db):
    async def send_func(session_id, post, header, source_types):
        raise asyncio.CancelledError

    scheduler, snapshot = await _scheduler(db, send_func, ("tag", "tag"))
    source = source_for_subscription(snapshot.subscriptions[0])

    with patch(
        "core.scheduler._fetch_snapshot_batches",
        AsyncMock(return_value=[SourceBatch(source, (_post(0),))]),
    ):
        with pytest.raises(asyncio.CancelledError):
            await scheduler._poll_all()

    assert (await _delivery_states(db))[0][1:] == ("sending", 0)


@pytest.mark.asyncio
async def test_poll_all_stale_snapshot_has_no_discovery_side_effects(db):
    send = AsyncMock(return_value=True)
    scheduler, snapshot = await _scheduler(db, send, ("tag", "tag"))
    source = source_for_subscription(snapshot.subscriptions[0])

    async def stale_fetch(captured, content_source):
        await db.mutate_author_blocks(
            "session", [("name", "other", "Other")], True
        )
        return [SourceBatch(source, (_post(0),))]

    with patch("core.scheduler._fetch_snapshot_batches", side_effect=stale_fetch):
        await scheduler._poll_all()

    send.assert_not_awaited()
    assert await _delivery_states(db) == []
    assert await db.filter_unseen_session("session", "tag", [_post(0).post_id]) == [
        _post(0).post_id
    ]
