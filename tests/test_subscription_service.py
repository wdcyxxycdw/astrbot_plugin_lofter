from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace

import pytest
import pytest_asyncio

from core.db import LofterDB
from core.errors import SourceSchemaError
from core.parser import Post
from core.session_gate import SessionGateRegistry
from core.source_scan import SourcePage
from core.subscription_service import SubscriptionService

Hook = Callable[[str, str], Awaitable[None]]


class FakeSource:
    def __init__(self) -> None:
        self.tags: dict[str, list[Post]] = {}
        self.blogs: dict[str, list[Post]] = {}
        self.failures: dict[tuple[str, str], Exception] = {}
        self.calls: list[tuple[str, str]] = []
        self.detail_calls: list[str] = []
        self.hook: Hook | None = None

    async def list_tag(self, tag, cursor, limit, sort):
        return await self._page("tag", tag, self.tags.get(tag, []), sort)

    async def list_blog(self, username, cursor, limit):
        return await self._page("blog", username, self.blogs.get(username, []), "new")

    async def get_post(self, url):
        self.detail_calls.append(url)
        post = next((
            post
            for feeds in (self.tags, self.blogs)
            for posts in feeds.values()
            for post in posts
            if post.url == url
        ), None)
        if post is None:
            raise AssertionError(f"missing fake detail post: {url}")
        return replace(
            post,
            images=[
                f"https://img.example/{post.post_id}-1.jpg",
                f"https://img.example/{post.post_id}-2.jpg",
            ],
            completeness=post.completeness | {"images"},
        )

    async def _page(self, kind, target, posts, sort):
        self.calls.append((kind, target))
        if self.hook is not None:
            await self.hook(kind, target)
        failure = self.failures.get((kind, target))
        if failure is not None:
            raise failure
        return SourcePage(
            items=list(posts),
            source="fake",
            next_cursor=None,
            exhausted=True,
            sort=sort,
            mapped_count=len(posts),
            dropped_count=0,
            complete=True,
        )


def make_post(
    post_id: str,
    *,
    tags: list[str] | None = None,
    author: str = "Author",
    username: str = "user",
    publish_time: str = "2024-02-29 12:34:56",
) -> Post:
    return Post(
        post_id=post_id,
        title=post_id,
        summary="",
        tags=tags or ["visible"],
        author=author,
        author_username=username,
        url=f"https://{username}.lofter.com/post/{post_id}",
        publish_time=publish_time,
        source="fake",
    )


@pytest_asyncio.fixture
async def db(tmp_path):
    database = LofterDB(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


def service(db, source):
    return SubscriptionService(db, source, SessionGateRegistry())


async def session_rows(db, session_id="session"):
    return await db.list_subscriptions(session_id)


async def generations(db, session_id="session"):
    snapshot = await db.capture_session_snapshot(session_id)
    return snapshot[:3]


async def seen_by_target(db, session_id="session"):
    return await db.transaction(lambda conn: conn.execute("""
        SELECT s.target,sp.post_id,sp.published_at
        FROM seen_posts sp JOIN subscriptions s ON s.id=sp.subscription_id
        WHERE s.session_id=? ORDER BY s.target,sp.post_id
    """, (session_id,)).fetchall())


@pytest.mark.asyncio
async def test_tag_subscribe_fetches_before_atomic_initialization(db):
    source = FakeSource()
    source.tags = {
        "a": [make_post("a_1"), make_post("a_2")],
        "b": [make_post("a_2"), make_post("a_3")],
    }
    observed_rows = []

    async def inspect_before_fetch(kind, target):
        observed_rows.append(await session_rows(db))

    source.hook = inspect_before_fetch
    result = await service(db, source).subscribe_tags(
        "session", ["a", "b"], ["hidden"]
    )

    assert observed_rows == [[], []]
    assert result.added_subscribes == ("a", "b")
    assert result.added_excludes == ("hidden",)
    rows = await session_rows(db)
    assert [(row[3], row[4], row[6], row[7]) for row in rows] == [
        ("subscribe", "a", "active", 1),
        ("subscribe", "b", "active", 1),
        ("exclude", "hidden", "active", 1),
    ]
    assert await generations(db) == (1, 0, 1)
    assert await seen_by_target(db) == [
        ("a", "a_1", 1709210096),
        ("a", "a_2", 1709210096),
        ("b", "a_2", 1709210096),
        ("b", "a_3", 1709210096),
    ]
    assert source.detail_calls == [
        "https://user.lofter.com/post/a_1",
        "https://user.lofter.com/post/a_2",
        "https://user.lofter.com/post/a_2",
        "https://user.lofter.com/post/a_3",
    ]


@pytest.mark.asyncio
async def test_empty_feed_activates_and_duplicate_is_noop_without_fetch(db):
    source = FakeSource()
    subscriptions = service(db, source)

    first = await subscriptions.subscribe_tags("session", ["empty"], [])
    calls_after_first = list(source.calls)
    second = await subscriptions.subscribe_tags("session", ["empty"], [])

    assert first.added_subscribes == ("empty",)
    assert second.added_subscribes == ()
    assert source.calls == calls_after_first == [("tag", "empty")]
    assert (await session_rows(db))[0][6] == "active"
    assert await generations(db) == (1, 0, 1)
    assert await seen_by_target(db) == []


@pytest.mark.asyncio
async def test_fetch_failure_leaves_subscribe_exclude_and_generations_unchanged(db):
    source = FakeSource()
    source.failures[("tag", "broken")] = SourceSchemaError("tag")

    with pytest.raises(SourceSchemaError):
        await service(db, source).subscribe_tags(
            "session", ["broken"], ["hidden"]
        )

    assert await session_rows(db) == []
    assert await generations(db) == (0, 0, 0)
    counts = await db.transaction(lambda conn: conn.execute("""
        SELECT
          (SELECT COUNT(*) FROM subscription_watermarks),
          (SELECT COUNT(*) FROM seen_posts)
    """).fetchone())
    assert counts == (0, 0)


@pytest.mark.asyncio
async def test_preview_marks_actual_source_seen_before_filtering(db):
    source = FakeSource()
    subscriptions = service(db, source)
    source.tags["a"] = [make_post("a_1")]
    await subscriptions.subscribe_tags("session", ["a"], ["hidden"])
    await db.mutate_author_blocks(
        "session", [("name", "blocked", "Blocked")], True
    )
    source.tags["a"] = [
        make_post("a_2", tags=["hidden"]),
        make_post("a_3", author="Blocked"),
        make_post("a_4"),
    ]

    result = await subscriptions.subscribe_tags(
        "session", ["a"], [], preview=True
    )

    assert [post.post_id for post in result.preview_posts] == ["a_4"]
    assert result.preview_posts[0].images == [
        "https://img.example/a_4-1.jpg",
        "https://img.example/a_4-2.jpg",
    ]
    assert await seen_by_target(db) == [
        ("a", "a_1", 1709210096),
        ("a", "a_2", 1709210096),
        ("a", "a_3", 1709210096),
        ("a", "a_4", 1709210096),
    ]
    deliveries = await db.transaction(
        lambda conn: conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
    )
    assert deliveries == 0
    assert await generations(db) == (1, 0, 2)


@pytest.mark.asyncio
async def test_pure_exclude_preview_has_no_side_effect(db):
    source = FakeSource()

    with pytest.raises(ValueError, match="preview requires"):
        await service(db, source).subscribe_tags(
            "session", [], ["hidden"], preview=True
        )

    assert source.calls == []
    assert await session_rows(db) == []
    assert await generations(db) == (0, 0, 0)


@pytest.mark.asyncio
async def test_stale_fetch_snapshot_rolls_back_its_whole_mutation(db):
    source = FakeSource()
    source.tags["late"] = [make_post("a_1")]
    changed = False

    async def mutate_during_fetch(kind, target):
        nonlocal changed
        if changed:
            return
        changed = True
        tag_revision, _, policy, rows, _ = await db.capture_session_snapshot(
            "session"
        )
        await db.initialize_runtime_subscriptions(
            "session", "tag", ["other"], [], {},
            tag_revision, policy, rows,
        )

    source.hook = mutate_during_fetch
    with pytest.raises(RuntimeError, match="snapshot changed"):
        await service(db, source).subscribe_tags(
            "session", ["late"], ["hidden"]
        )

    rows = await session_rows(db)
    assert [(row[3], row[4]) for row in rows] == [("subscribe", "other")]
    assert await generations(db) == (1, 0, 1)
    assert await seen_by_target(db) == []


@pytest.mark.asyncio
async def test_repository_failure_rolls_back_revision_row_and_watermark(db):
    snapshot = await db.capture_session_snapshot("session")

    with pytest.raises(ValueError, match="post_id"):
        await db.initialize_runtime_subscriptions(
            "session",
            "tag",
            ["a"],
            [],
            {"a": [("", 1709210096)]},
            snapshot[0],
            snapshot[2],
            snapshot[3],
        )

    assert await session_rows(db) == []
    assert await generations(db) == (0, 0, 0)
    watermarks = await db.transaction(
        lambda conn: conn.execute(
            "SELECT COUNT(*) FROM subscription_watermarks"
        ).fetchone()[0]
    )
    assert watermarks == 0


@pytest.mark.asyncio
async def test_index_remove_selects_and_mutates_in_one_transaction(db):
    source = FakeSource()
    subscriptions = service(db, source)
    await subscriptions.subscribe_tags("session", ["a", "b"], ["hidden"])

    removed, count = await subscriptions.remove_by_index("session", 2)
    missing, count_after = await subscriptions.remove_by_index("session", 9)

    assert count == 3
    assert removed is not None and removed.target == "b"
    assert missing is None and count_after == 2
    assert [(row[3], row[4]) for row in await session_rows(db)] == [
        ("subscribe", "a"),
        ("exclude", "hidden"),
    ]
    assert await generations(db) == (2, 0, 2)
