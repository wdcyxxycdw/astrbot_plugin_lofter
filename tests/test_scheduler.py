import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from core.db import LofterDB
from core.errors import SourceSchemaError
from core.parser import Post
from core.source_scan import SourcePage
from core.scheduler import (
    SubscriptionScheduler,
    fetch_blog_posts,
    fetch_tag_posts,
    _check_tag_session,
    _check_blog_sub,
    _enrich_blog_posts,
    _push_tag_posts,
    _push_blog_post,
    _build_tag_rule,
)
from core.author_block import AuthorBlockStorage
from core.filter import FilterRule
from core.storage import Subscription, SubscriptionStorage

RICH_HTML = """\
<!DOCTYPE html><html><head>
<title>帖子标题-作者名</title>
<meta name="Description" content="这是摘要"/>
</head><body></body></html>"""

BARE_POST = Post(
    post_id="abc123",
    title="",
    summary="",
    url="https://user.lofter.com/post/abc123",
    publish_time="2026-07-29 05:00:00",
)


@pytest_asyncio.fixture
async def db(tmp_path):
    d = LofterDB(str(tmp_path / "test.db"))
    await d.initialize()
    yield d
    await d.close()


def _make_sub(target: str, role: str = "subscribe", sub_type: str = "tag", session_id: str = "sess1") -> Subscription:
    return Subscription(id=1, session_id=session_id, type=sub_type, role=role, target=target)


async def _enrich_with_blocked_p2(posts, _client):
    return [
        Post(
            post_id=post.post_id,
            title=post.title,
            summary=post.summary,
            url=post.url,
            author_username=(
                "blockeduser" if post.post_id == "p2" else ""
            ),
            completeness=frozenset({
                "title", "summary", "url", "author_username",
            }),
        )
        for post in posts
    ]


def test_scheduler_requires_explicit_block_storage(db):
    storage = AsyncMock()
    client = AsyncMock()
    send_func = AsyncMock()

    with pytest.raises(TypeError):
        SubscriptionScheduler(storage, client, db, send_func)

    with pytest.raises(TypeError):
        SubscriptionScheduler(storage, client, db, send_func, 5)

    scheduler = SubscriptionScheduler(
        storage,
        client,
        db,
        send_func,
        block_storage=AuthorBlockStorage(db),
        interval_minutes=5,
    )
    assert scheduler._block_storage is not None


@pytest.mark.asyncio
async def test_enrich_success():
    source = AsyncMock()
    source.get_post.return_value = Post(
        post_id="abc123",
        title="帖子标题",
        author="作者名",
        summary="这是摘要",
        url=BARE_POST.url,
    )

    result = await _enrich_blog_posts([BARE_POST], source)

    assert len(result) == 1
    assert result[0].title == "帖子标题"
    assert result[0].author == "作者名"
    assert result[0].summary == "这是摘要"
    assert result[0].post_id == "abc123"


@pytest.mark.asyncio
async def test_enrich_preserves_base_url_time_and_provenance():
    base = Post(
        post_id="abc123",
        title="",
        summary="",
        url="https://user.lofter.com/post/abc123",
        publish_time="2026-07-29 05:00:00",
        source="mobile_blog",
        completeness=frozenset({"url", "publish_time"}),
        provenance={
            "url": "mobile_blog",
            "publish_time": "mobile_blog",
        },
    )
    detail = Post(
        post_id="abc123",
        title="详情标题",
        summary="",
        url=base.url,
        publish_time=base.publish_time,
        source="embedded_json",
        completeness=frozenset({"title", "url", "publish_time"}),
        provenance={
            "title": "embedded_json",
            "url": "embedded_json",
            "publish_time": "embedded_json",
        },
    )
    source = AsyncMock()
    source.get_post.return_value = detail

    result = await _enrich_blog_posts([base], source)

    assert result[0].title == "详情标题"
    assert result[0].url == base.url
    assert result[0].publish_time == base.publish_time
    assert result[0].provenance["url"] == "mobile_blog"
    assert result[0].provenance["publish_time"] == "mobile_blog"


@pytest.mark.asyncio
async def test_enrich_post_identity_mismatch_is_not_masked():
    source = AsyncMock()
    source.get_post.return_value = Post(
        post_id="different", title="wrong", summary="", url=BARE_POST.url
    )
    with pytest.raises(SourceSchemaError):
        await _enrich_blog_posts([BARE_POST], source)


@pytest.mark.asyncio
async def test_enrich_parser_identity_mismatch_is_not_masked():
    source = AsyncMock()
    source.get_post.side_effect = SourceSchemaError("embedded.post.id")
    with pytest.raises(SourceSchemaError):
        await _enrich_blog_posts([BARE_POST], source)


@pytest.mark.asyncio
async def test_enrich_fallback_on_error():
    source = AsyncMock()
    source.get_post.side_effect = Exception("source error")

    result = await _enrich_blog_posts([BARE_POST], source)

    assert len(result) == 1
    assert result[0].post_id == "abc123"
    assert result[0].title == ""


# ── _push_tag_posts / _push_blog_post ────────────────────────────────────────

TAG_RULE = FilterRule(search_tags=["原创"])

FULL_POST = Post(
    post_id="p1",
    title="帖子标题",
    author="作者名",
    summary="这是摘要",
    tags=["原创", "tag2"],
    images=["https://img1.jpg", "https://img2.jpg"],
    url="https://user.lofter.com/post/p1",
)


@pytest.mark.asyncio
async def test_push_tag_label():
    send = AsyncMock(return_value=True)
    await _push_tag_posts("sess1", [FULL_POST], TAG_RULE, send)
    text = send.call_args[0][1]
    assert "【标签「原创」有新内容】" in text


@pytest.mark.asyncio
async def test_push_tag_label_uses_actual_target_source():
    send = AsyncMock(return_value=True)
    rule = FilterRule(search_tags=["A", "B"])

    await _push_tag_posts(
        "sess1", [FULL_POST], rule, send,
        sources={FULL_POST.post_id: {"B"}},
    )

    text = send.call_args[0][1]
    assert "【标签「B」有新内容】" in text
    assert "【标签「A」有新内容】" not in text


@pytest.mark.asyncio
async def test_push_blog_label():
    send = AsyncMock(return_value=True)
    await _push_blog_post("sess1", FULL_POST, "someuser", send)
    text = send.call_args[0][1]
    assert "【博主「someuser」有新内容】" in text


@pytest.mark.asyncio
async def test_push_includes_author_summary_tags_url():
    send = AsyncMock(return_value=True)
    await _push_tag_posts("sess1", [FULL_POST], TAG_RULE, send)
    text = send.call_args[0][1]
    assert "作者：作者名" in text
    assert "这是摘要" in text
    assert "#原创" in text
    assert FULL_POST.url in text


@pytest.mark.asyncio
async def test_push_includes_images():
    send = AsyncMock(return_value=True)
    await _push_tag_posts("sess1", [FULL_POST], TAG_RULE, send)
    images = send.call_args[0][2]
    assert images == FULL_POST.images


@pytest.mark.asyncio
async def test_push_does_not_send_unknown_images():
    send = AsyncMock(return_value=True)
    post = Post(
        post_id="p-hidden-image",
        title="Demo",
        summary="",
        images=["https://secret.example/image.jpg"],
        url="https://u.lofter.com/post/p-hidden-image",
        completeness=frozenset({"title", "url"}),
    )

    await _push_tag_posts("sess1", [post], TAG_RULE, send)

    assert send.call_args[0][2] == []


@pytest.mark.asyncio
async def test_push_no_title_shows_placeholder():
    send = AsyncMock(return_value=True)
    post = Post(
        post_id="p2",
        title="",
        summary="有摘要",
        url="https://u.lofter.com/post/p2",
        completeness=frozenset({"title", "summary", "url"}),
    )
    await _push_tag_posts("sess1", [post], TAG_RULE, send)
    text = send.call_args[0][1]
    assert "(无标题)" in text


@pytest.mark.asyncio
async def test_push_reversed_order():
    send = AsyncMock(return_value=True)
    posts = [
        Post(post_id=f"p{i}", title=f"帖子{i}", summary="", url=f"https://u.lofter.com/post/p{i}")
        for i in range(3)
    ]
    await _push_tag_posts("sess1", posts, TAG_RULE, send)
    calls = send.call_args_list
    titles = [c[0][1] for c in calls]
    assert "帖子2" in titles[0]
    assert "帖子0" in titles[2]


@pytest.mark.asyncio
async def test_push_max_5_posts():
    send = AsyncMock(return_value=True)
    posts = [
        Post(post_id=f"p{i}", title=f"帖子{i}", summary="", url=f"https://u.lofter.com/post/p{i}")
        for i in range(8)
    ]
    await _push_tag_posts("sess1", posts, TAG_RULE, send)
    assert send.call_count == 5


@pytest.mark.asyncio
async def test_enrich_serial_order():
    posts = [
        Post(
            post_id="p1", title="", summary="",
            url="https://u.lofter.com/post/p1",
            publish_time="2026-07-29 05:00:00",
        ),
        Post(
            post_id="p2", title="", summary="",
            url="https://u.lofter.com/post/p2",
            publish_time="2026-07-29 05:00:00",
        ),
    ]
    source = AsyncMock()
    source.get_post.side_effect = [
        Post(post_id="p1", title="标题1", summary="", url=posts[0].url),
        Post(post_id="p2", title="标题2", summary="", url=posts[1].url),
    ]

    result = await _enrich_blog_posts(posts, source)

    assert result[0].title == "标题1"
    assert result[1].title == "标题2"
    assert [call.args[0] for call in source.get_post.await_args_list] == [
        posts[0].url, posts[1].url,
    ]


def _source_page(
    ids: list[str], cursor: str | None = None, *, source="mobile_tag", exhausted=False
) -> SourcePage:
    posts = _make_posts(ids)
    return SourcePage(
        items=posts,
        source=source,
        next_cursor=cursor,
        exhausted=exhausted,
        sort="new",
        mapped_count=len(posts),
        dropped_count=0,
        complete=True,
    )


@pytest.mark.asyncio
async def test_fetch_tag_posts_follows_cursor_and_dedupes():
    source = AsyncMock()
    source.list_tag.side_effect = [
        _source_page(["p1", "p2"], "next"),
        _source_page(["p2", "p3"], exhausted=True),
    ]

    posts = await fetch_tag_posts(["A"], source, limit=3)

    assert [post.post_id for post in posts] == ["p1", "p2", "p3"]
    assert [call.args[1] for call in source.list_tag.await_args_list] == [None, "next"]


@pytest.mark.asyncio
async def test_fetch_blog_posts_follows_cursor_and_dedupes():
    source = AsyncMock()
    source.list_blog.side_effect = [
        _source_page(["p1", "p2"], "next", source="mobile_blog"),
        _source_page(["p2", "p3"], exhausted=True, source="mobile_blog"),
    ]

    posts = await fetch_blog_posts(
        _make_sub("someuser", sub_type="blog"), source, limit=3
    )

    assert [post.post_id for post in posts] == ["p1", "p2", "p3"]
    assert [call.args[1] for call in source.list_blog.await_args_list] == [None, "next"]


# ── 聚合标签轮询 ──────────────────────────────────────────────────────────────

def _make_posts(ids: list[str], tags: list[str] | None = None) -> list[Post]:
    return [
        Post(
            post_id=pid,
            title=f"帖子{pid}",
            summary="",
            url=f"https://u.lofter.com/post/{pid}",
            tags=tags or [],
            publish_time="2026-07-29 05:00:00",
        )
        for pid in ids
    ]


@pytest.mark.asyncio
async def test_tag_target_skips_unknown_tags_without_excludes():
    partial = Post(
        post_id="p1",
        title="Demo",
        summary="",
        url="https://u.lofter.com/post/p1",
        publish_time="2026-07-29 05:00:00",
        source="mobile_tag",
        completeness=frozenset({"title", "url", "publish_time"}),
    )
    source = AsyncMock()
    with patch("core.scheduler.fetch_tag_posts", return_value=[partial]):
        from core.scheduler import _fetch_all_tag_targets
        result = await _fetch_all_tag_targets(
            FilterRule(search_tags=["A"]), source
        )

    assert result["A"] == [partial]
    source.get_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_tag_rule():
    subs = [
        _make_sub("原神", "subscribe"),
        _make_sub("崩铁", "subscribe"),
        _make_sub("R18", "exclude"),
    ]
    rule = _build_tag_rule(subs)
    assert set(rule.search_tags) == {"原神", "崩铁"}
    assert rule.exclude_tags == ["R18"]


@pytest.mark.asyncio
async def test_aggregate_tag_session(db):
    """两个 subscribe + 一个 exclude，合并拉取，exclude 正确过滤"""
    await db.add_subscription("sess1", "tag", "原神")
    await db.add_subscription("sess1", "tag", "崩铁")
    subs = [
        _make_sub("原神", "subscribe"),
        _make_sub("崩铁", "subscribe"),
        _make_sub("R18", "exclude"),
    ]

    posts_genshin = _make_posts(["g1", "g2"], tags=["原神"])
    posts_hsr = _make_posts(["h1"], tags=["崩铁"])
    posts_r18 = _make_posts(["r1"], tags=["原神", "R18"])

    async def mock_fetch(search_tags, client):
        result = []
        for tag in search_tags:
            if tag == "原神":
                result.extend(posts_genshin + posts_r18)
            elif tag == "崩铁":
                result.extend(posts_hsr)
        seen = set()
        deduped = []
        for p in result:
            if p.post_id not in seen:
                seen.add(p.post_id)
                deduped.append(p)
        return deduped

    sent: list[str] = []

    async def send_func(session_id, text, images):
        sent.append(text)
        return True

    client = AsyncMock()

    with patch("core.scheduler.fetch_tag_posts", side_effect=mock_fetch):
        await db.mark_seen_session("sess1", "tag", ["warmup"])
        await _check_tag_session("sess1", subs, client, db, send_func, AuthorBlockStorage(db))

    pushed_ids = await db.filter_unsent("sess1", ["g1", "g2", "h1", "r1"])
    assert "r1" in pushed_ids
    assert "g1" not in pushed_ids
    assert "g2" not in pushed_ids
    assert "h1" not in pushed_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, RuntimeError("send failed")], ids=["false", "exception"])
async def test_poll_all_stops_same_session_blog_after_tag_send_failure(db, failure):
    storage = SubscriptionStorage(db)
    await storage.add("same", "tag", "tag")
    await storage.add("same", "blog", "same-blog")
    await storage.add("other", "blog", "other-blog")
    await db.mark_seen_session("same", "tag", ["warmup"])
    await db.mark_seen_session("same", "blog", ["warmup"])
    await db.mark_seen_session("other", "blog", ["warmup"])
    same_blog_fetches = 0
    same_send_texts: list[str] = []

    async def fetch_blog(sub, client):
        nonlocal same_blog_fetches
        same_blog_fetches += sub.session_id == "same"
        return _make_posts([f"{sub.session_id}-blog"])

    async def send(session_id, text, images):
        if session_id == "same":
            same_send_texts.append(text)
            if isinstance(failure, Exception):
                raise failure
            return failure
        return True

    scheduler = SubscriptionScheduler(
        storage, AsyncMock(), db, send,
        block_storage=AuthorBlockStorage(db),
    )
    with (
        patch("core.scheduler.fetch_tag_posts", return_value=_make_posts(["same-tag"])),
        patch("core.scheduler.fetch_blog_posts", side_effect=fetch_blog),
        patch("core.scheduler._enrich_blog_posts", side_effect=lambda items, client: items),
    ):
        await scheduler._poll_all()

    assert same_blog_fetches == 1
    assert len(same_send_texts) == 1
    assert "same-blog" in same_send_texts[0]
    assert "same-tag" not in same_send_texts[0]
    assert await db.filter_unseen_session(
        "same", "tag", ["same-tag"]
    ) == ["same-tag"]
    assert await db.filter_unseen_session(
        "same", "blog", ["same-blog"]
    ) == ["same-blog"]
    assert await db.filter_unsent("same", ["same-tag", "same-blog"]) == [
        "same-tag", "same-blog",
    ]
    assert await db.filter_unsent("other", ["other-blog"]) == []


@pytest.mark.asyncio
async def test_tag_checkpoint_isolated_by_actual_target_provenance(db):
    await db.add_subscription("sess1", "tag", "A")
    await db.add_subscription("sess1", "tag", "B")
    await db.import_legacy_checkpoint("sess1", "tag", "A", "100")
    posts_by_target = {
        "A": _make_posts(["101"], tags=["A"]),
        "B": _make_posts(["50"], tags=["B"]),
    }
    sent: list[str] = []

    async def fetch(search_tags, client):
        return posts_by_target[search_tags[0]]

    async def send(session_id, text, images):
        sent.append(text)
        return True

    with patch("core.scheduler.fetch_tag_posts", side_effect=fetch):
        result = await _check_tag_session(
            "sess1", [_make_sub("A"), _make_sub("B")], AsyncMock(), db,
            send, AuthorBlockStorage(db),
        )

    rows = await db.transaction(lambda conn: conn.execute("""
        SELECT s.target,sp.post_id FROM seen_posts sp
        JOIN subscriptions s ON s.id=sp.subscription_id
        ORDER BY s.target,sp.post_id
    """).fetchall())
    assert result is True
    assert len(sent) == 2
    assert rows == [("A", "101"), ("B", "50")]
    assert await db.filter_unseen_targets(
        "sess1", "tag", {"A": ["101"], "B": ["50"]}
    ) == []


@pytest.mark.asyncio
async def test_tag_same_post_sends_once_and_marks_all_actual_sources(db):
    await db.add_subscription("sess1", "tag", "A")
    await db.add_subscription("sess1", "tag", "B")
    await db.mark_seen_targets("sess1", "tag", {"A": ["warmup"], "B": ["warmup"]})
    first = _make_posts(["00A_000B"], tags=["A", "B"])
    second = _make_posts(["a_b"], tags=["B", "A"])
    second[0].title = first[0].title
    posts_by_target = {"A": first, "B": second}
    send = AsyncMock(return_value=True)

    async def fetch(search_tags, client):
        return posts_by_target[search_tags[0]]

    with patch("core.scheduler.fetch_tag_posts", side_effect=fetch):
        await _check_tag_session(
            "sess1", [_make_sub("A"), _make_sub("B")], AsyncMock(), db,
            send, AuthorBlockStorage(db),
        )

    rows = await db.transaction(lambda conn: conn.execute("""
        SELECT s.target,sp.post_id FROM seen_posts sp
        JOIN subscriptions s ON s.id=sp.subscription_id
        WHERE sp.post_id='a_b' ORDER BY s.target
    """).fetchall())
    send.assert_awaited_once()
    assert rows == [("A", "a_b"), ("B", "a_b")]


async def _assert_failed_tag_poll_state(db, send, blog_fetch):
    checkpoints = await db.transaction(lambda conn: conn.execute(
        "SELECT post_id FROM legacy_checkpoints ORDER BY post_id"
    ).fetchall())
    tag_seen = await db.transaction(lambda conn: conn.execute("""
        SELECT COUNT(*) FROM seen_posts sp
        JOIN subscriptions s ON s.id=sp.subscription_id
        WHERE s.session_id='sess1' AND s.type='tag'
    """).fetchone()[0])
    deliveries = await db.transaction(lambda conn: conn.execute(
        "SELECT COUNT(*) FROM deliveries WHERE session_id='sess1'"
    ).fetchone()[0])
    assert checkpoints == [("100",), ("40",)]
    assert tag_seen == 0
    assert deliveries == 0
    send.assert_not_awaited()
    blog_fetch.assert_not_awaited()


async def _assert_retried_tag_poll_state(db, send, blog_fetch):
    assert send.call_count == 2
    blog_fetch.assert_awaited_once()
    assert await db.transaction(lambda conn: conn.execute(
        "SELECT COUNT(*) FROM legacy_checkpoints"
    ).fetchone()[0]) == 0
    rows = await db.transaction(lambda conn: conn.execute("""
        SELECT s.target,sp.post_id FROM seen_posts sp
        JOIN subscriptions s ON s.id=sp.subscription_id
        WHERE s.session_id='sess1' AND s.type='tag'
        ORDER BY s.target,sp.post_id
    """).fetchall())
    assert rows == [("A", "101"), ("B", "41")]
    assert await db.filter_unseen_targets(
        "sess1", "tag", {"A": ["101"], "B": ["41"]}
    ) == []
    assert await db.filter_unsent("sess1", ["101", "41"]) == []


@pytest.mark.asyncio
async def test_tag_second_page_failure_has_zero_db_or_send_side_effects(db):
    await db.add_subscription("sess1", "tag", "A")
    await db.import_legacy_checkpoint("sess1", "tag", "A", "100")
    source = AsyncMock()
    source.list_tag.side_effect = [
        _source_page(["101"], "next"),
        SourceSchemaError("response"),
    ]
    send = AsyncMock(return_value=True)

    await _check_tag_session(
        "sess1", [_make_sub("A")], source, db, send, AuthorBlockStorage(db)
    )

    assert await db.transaction(lambda conn: conn.execute(
        "SELECT post_id FROM legacy_checkpoints"
    ).fetchall()) == [("100",)]
    assert await db.seen_count("sess1", "tag") == 0
    assert await db.filter_unsent("sess1", ["101"]) == ["101"]
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_later_target_second_page_failure_keeps_all_targets_side_effect_free(db):
    await db.add_subscription("sess1", "tag", "A")
    await db.add_subscription("sess1", "tag", "B")
    await db.import_legacy_checkpoint("sess1", "tag", "A", "100")
    await db.import_legacy_checkpoint("sess1", "tag", "B", "40")
    source = AsyncMock()
    source.list_tag.side_effect = [
        _source_page(["101"], exhausted=True),
        _source_page(["41"], "b-next"),
        SourceSchemaError("response"),
    ]
    send = AsyncMock(return_value=True)

    await _check_tag_session(
        "sess1", [_make_sub("A"), _make_sub("B")], source, db, send,
        AuthorBlockStorage(db),
    )

    checkpoints = await db.transaction(lambda conn: conn.execute(
        "SELECT post_id FROM legacy_checkpoints ORDER BY post_id"
    ).fetchall())
    assert checkpoints == [("100",), ("40",)]
    assert await db.seen_count("sess1", "tag") == 0
    assert await db.filter_unsent("sess1", ["101", "41"]) == ["101", "41"]
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_tag_fetch_failure_preserves_all_checkpoints_and_retries(db):
    storage = SubscriptionStorage(db)
    await storage.add("sess1", "tag", "A")
    await storage.add("sess1", "tag", "B")
    await storage.add("sess1", "blog", "blog")
    await db.import_legacy_checkpoint("sess1", "tag", "A", "100")
    await db.import_legacy_checkpoint("sess1", "tag", "B", "40")
    send = AsyncMock(return_value=True)
    scheduler = SubscriptionScheduler(
        storage, AsyncMock(), db, send,
        block_storage=AuthorBlockStorage(db),
    )
    blog_fetch = AsyncMock(return_value=[])
    posts_by_target = {
        "A": _make_posts(["101"], tags=["A"]),
        "B": _make_posts(["41"], tags=["B"]),
    }
    failed = False

    async def fetch(search_tags, client):
        nonlocal failed
        target = search_tags[0]
        if target == "B" and not failed:
            failed = True
            raise RuntimeError("B failed")
        return posts_by_target[target]

    with (
        patch("core.scheduler.fetch_tag_posts", side_effect=fetch),
        patch("core.scheduler.fetch_blog_posts", blog_fetch),
    ):
        await scheduler._poll_all()
        await _assert_failed_tag_poll_state(db, send, blog_fetch)
        await scheduler._poll_all()

    await _assert_retried_tag_poll_state(db, send, blog_fetch)


@pytest.mark.asyncio
async def test_warmup_no_push(db):
    """冷启动（seen_count=0）时 mark_seen 但不推送"""
    await db.add_subscription("sess1", "tag", "原神")
    subs = [_make_sub("原神", "subscribe")]
    posts = _make_posts(["p1", "p2", "p3"])

    async def mock_fetch(search_tags, client):
        return posts

    sent: list[str] = []

    async def send_func(session_id, text, images):
        sent.append(text)
        return True

    client = AsyncMock()

    with patch("core.scheduler.fetch_tag_posts", side_effect=mock_fetch):
        await _check_tag_session("sess1", subs, client, db, send_func, AuthorBlockStorage(db))

    assert sent == []
    count = await db.seen_count("sess1", "tag")
    assert count == 3


@pytest.mark.asyncio
async def test_new_post_pushed_after_warmup(db):
    """warmup 后新帖应该被推送"""
    await db.add_subscription("sess1", "tag", "原神")
    subs = [_make_sub("原神", "subscribe")]
    old_posts = _make_posts(["p1", "p2"])
    new_post = Post(post_id="p3", title="新帖", summary="", url="https://u.lofter.com/post/p3")

    async def mock_fetch_old(search_tags, client):
        return old_posts

    async def mock_fetch_new(search_tags, client):
        return old_posts + [new_post]

    sent: list[str] = []

    async def send_func(session_id, text, images):
        sent.append(text)
        return True

    client = AsyncMock()

    with patch("core.scheduler.fetch_tag_posts", side_effect=mock_fetch_old):
        await _check_tag_session("sess1", subs, client, db, send_func, AuthorBlockStorage(db))

    assert sent == []

    with patch("core.scheduler.fetch_tag_posts", side_effect=mock_fetch_new):
        await _check_tag_session("sess1", subs, client, db, send_func, AuthorBlockStorage(db))

    assert len(sent) == 1
    assert "新帖" in sent[0]


@pytest.mark.asyncio
async def test_tag_session_blocks_author_but_marks_seen(db):
    await db.add_subscription("sess1", "tag", "原神")
    await db.add_author_block("sess1", "name", "屏蔽作者", "屏蔽作者")
    blocks = AuthorBlockStorage(db)
    subs = [_make_sub("原神", "subscribe")]
    posts = [
        Post(post_id="p1", title="可见", summary="", author="可见作者", url="https://a.lofter.com/post/p1"),
        Post(post_id="p2", title="屏蔽", summary="", author="屏蔽作者", url="https://b.lofter.com/post/p2"),
    ]
    sent: list[str] = []

    async def send_func(session_id, text, images):
        sent.append(text)
        return True

    with patch("core.scheduler.fetch_tag_posts", return_value=posts):
        await db.mark_seen_session("sess1", "tag", ["warmup"])
        await _check_tag_session("sess1", subs, AsyncMock(), db, send_func, blocks)

    assert len(sent) == 1
    assert "可见" in sent[0]
    assert await db.filter_unseen_session("sess1", "tag", ["p1", "p2"]) == []
    assert await db.filter_unsent("sess1", ["p1", "p2"]) == ["p2"]


@pytest.mark.asyncio
async def test_tag_session_overflow_posts_remain_pending_until_next_poll(db):
    await db.add_subscription("sess1", "tag", "原神")
    subs = [_make_sub("原神", "subscribe")]
    posts = _make_posts([f"p{i}" for i in range(8)])
    sent: list[str] = []

    async def send_func(session_id, text, images):
        sent.append(text)
        return True

    blocks = AuthorBlockStorage(db)

    with patch("core.scheduler.fetch_tag_posts", return_value=posts):
        await db.mark_seen_session("sess1", "tag", ["warmup"])
        await _check_tag_session("sess1", subs, AsyncMock(), db, send_func, blocks)

        assert len(sent) == 5
        assert await db.filter_unseen_session("sess1", "tag", [p.post_id for p in posts]) == ["p5", "p6", "p7"]
        assert await db.filter_unsent("sess1", [p.post_id for p in posts]) == ["p5", "p6", "p7"]

        await _check_tag_session("sess1", subs, AsyncMock(), db, send_func, blocks)

    assert len(sent) == 8
    assert await db.filter_unseen_session("sess1", "tag", [p.post_id for p in posts]) == []
    assert await db.filter_unsent("sess1", [p.post_id for p in posts]) == []


@pytest.mark.asyncio
async def test_blog_session_blocks_username_before_push(db):
    await db.add_subscription("sess1", "blog", "blockeduser")
    await db.add_author_block("sess1", "username", "blockeduser", "blockeduser")
    blocks = AuthorBlockStorage(db)
    sub = _make_sub("blockeduser", sub_type="blog", session_id="sess1")
    posts = [
        Post(
            post_id="p1",
            title="屏蔽",
            summary="",
            author_username="blockeduser",
            url="https://blockeduser.lofter.com/post/p1",
        )
    ]
    sent: list[str] = []

    async def send_func(session_id, text, images):
        sent.append(text)
        return True

    with patch("core.scheduler.fetch_blog_posts", return_value=posts):
        await db.mark_seen_session("sess1", "blog", ["warmup"])
        await _check_blog_sub(sub, AsyncMock(), db, send_func, blocks)

    assert sent == []
    assert await db.filter_unseen_session("sess1", "blog", ["p1"]) == []
    assert await db.filter_unsent("sess1", ["p1"]) == ["p1"]


@pytest.mark.asyncio
async def test_blog_session_overflow_posts_remain_pending_until_next_poll(db):
    await db.add_subscription("sess1", "blog", "someuser")
    sub = _make_sub("someuser", sub_type="blog", session_id="sess1")
    posts = _make_posts([f"p{i}" for i in range(8)])
    sent: list[str] = []

    async def send_func(session_id, text, images):
        sent.append(text)
        return True

    blocks = AuthorBlockStorage(db)

    with (
        patch("core.scheduler.fetch_blog_posts", return_value=posts),
        patch("core.scheduler._enrich_blog_posts", side_effect=lambda posts, client: posts),
    ):
        await db.mark_seen_session("sess1", "blog", ["warmup"])
        await _check_blog_sub(sub, AsyncMock(), db, send_func, blocks)

        assert len(sent) == 5
        assert await db.filter_unseen_session("sess1", "blog", [p.post_id for p in posts]) == ["p5", "p6", "p7"]
        assert await db.filter_unsent("sess1", [p.post_id for p in posts]) == ["p5", "p6", "p7"]

        await _check_blog_sub(sub, AsyncMock(), db, send_func, blocks)

    assert len(sent) == 8
    assert await db.filter_unseen_session("sess1", "blog", [p.post_id for p in posts]) == []
    assert await db.filter_unsent("sess1", [p.post_id for p in posts]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [False, None])
async def test_tag_send_false_or_none_does_not_ack(db, result):
    await db.add_subscription("sess1", "tag", "tag")
    await db.mark_seen_session("sess1", "tag", ["warmup"])
    posts = _make_posts(["p1", "p2"])
    send = AsyncMock(return_value=result)
    with patch("core.scheduler.fetch_tag_posts", return_value=posts):
        await _check_tag_session(
            "sess1", [_make_sub("tag")], AsyncMock(), db, send, AuthorBlockStorage(db)
        )
    assert send.call_count == 1
    assert await db.filter_unseen_session("sess1", "tag", ["p1", "p2"]) == ["p1", "p2"]
    assert await db.filter_unsent("sess1", ["p1", "p2"]) == ["p1", "p2"]


@pytest.mark.asyncio
async def test_tag_send_exception_and_partial_success_stop_in_order(db):
    await db.add_subscription("sess1", "tag", "tag")
    await db.mark_seen_session("sess1", "tag", ["warmup"])
    posts = _make_posts(["p0", "p1", "p2"])
    send = AsyncMock(side_effect=[True, RuntimeError("send failed")])
    with patch("core.scheduler.fetch_tag_posts", return_value=posts):
        await _check_tag_session(
            "sess1", [_make_sub("tag")], AsyncMock(), db, send, AuthorBlockStorage(db)
        )
    assert send.call_count == 2
    assert await db.filter_unseen_session("sess1", "tag", ["p0", "p1", "p2"]) == ["p0", "p1"]
    assert await db.filter_unsent("sess1", ["p0", "p1", "p2"]) == ["p0", "p1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "side_effect",
    [False, None, RuntimeError("send failed")],
    ids=["false", "none", "exception"],
)
async def test_blog_send_failure_does_not_ack_and_stops(db, side_effect):
    await db.add_subscription("sess1", "blog", "someuser")
    await db.mark_seen_session("sess1", "blog", ["warmup"])
    posts = _make_posts(["p0", "p1"])
    if isinstance(side_effect, Exception):
        send = AsyncMock(side_effect=side_effect)
    else:
        send = AsyncMock(return_value=side_effect)
    with (
        patch("core.scheduler.fetch_blog_posts", return_value=posts),
        patch("core.scheduler._enrich_blog_posts", side_effect=lambda items, client: items),
    ):
        await _check_blog_sub(
            _make_sub("someuser", sub_type="blog"), AsyncMock(), db, send,
            AuthorBlockStorage(db),
        )
    assert send.call_count == 1
    assert await db.filter_unseen_session("sess1", "blog", ["p0", "p1"]) == ["p0", "p1"]
    assert await db.filter_unsent("sess1", ["p0", "p1"]) == ["p0", "p1"]


@pytest.mark.asyncio
async def test_legacy_numeric_tag_checkpoint_enforces_floor_once(db):
    await db.add_subscription("sess1", "tag", "tag")
    await db.import_legacy_checkpoint("sess1", "tag", "tag", "10")
    posts = _make_posts(["9", "11", "12"])
    send = AsyncMock(return_value=True)
    with patch("core.scheduler.fetch_tag_posts", return_value=posts):
        await _check_tag_session(
            "sess1", [_make_sub("tag")], AsyncMock(), db, send, AuthorBlockStorage(db)
        )
    assert send.call_count == 2
    assert await db.filter_unseen_session("sess1", "tag", ["9", "11", "12"]) == []
    assert await db.transaction(lambda conn: conn.execute(
        "SELECT COUNT(*) FROM legacy_checkpoints"
    ).fetchone()[0]) == 0
    send.reset_mock()
    rolled = _make_posts(["8", "9"])
    with patch("core.scheduler.fetch_tag_posts", return_value=rolled):
        await _check_tag_session(
            "sess1", [_make_sub("tag")], AsyncMock(), db, send, AuthorBlockStorage(db)
        )
    send.assert_not_awaited()
    assert await db.filter_unseen_session("sess1", "tag", ["8", "9"]) == []


@pytest.mark.asyncio
async def test_checkpoint_send_failure_keeps_eligible_tag_retryable(db):
    await db.add_subscription("sess1", "tag", "tag")
    await db.import_legacy_checkpoint("sess1", "tag", "tag", "10")
    posts = _make_posts(["9", "11"])
    send = AsyncMock(return_value=False)
    blocks = AuthorBlockStorage(db)
    with patch("core.scheduler.fetch_tag_posts", return_value=posts):
        await _check_tag_session(
            "sess1", [_make_sub("tag")], AsyncMock(), db, send, blocks
        )
        assert await db.filter_unseen_session(
            "sess1", "tag", ["9", "11"]
        ) == ["11"]
        assert await db.filter_unsent("sess1", ["9", "11"]) == ["9", "11"]
        assert await db.transaction(lambda conn: conn.execute(
            "SELECT COUNT(*) FROM legacy_checkpoints"
        ).fetchone()[0]) == 0

        send.reset_mock()
        send.return_value = True
        await _check_tag_session(
            "sess1", [_make_sub("tag")], AsyncMock(), db, send, blocks
        )

    send.assert_awaited_once()
    assert await db.filter_unseen_session("sess1", "tag", ["9", "11"]) == []
    assert await db.filter_unsent("sess1", ["9", "11"]) == ["9"]


@pytest.mark.asyncio
async def test_empty_feed_preserves_checkpoint_for_first_observable_batch(db):
    await db.add_subscription("sess1", "tag", "tag")
    await db.import_legacy_checkpoint("sess1", "tag", "tag", "10")
    with patch("core.scheduler.fetch_tag_posts", return_value=[]):
        await _check_tag_session(
            "sess1", [_make_sub("tag")], AsyncMock(), db,
            AsyncMock(return_value=True), AuthorBlockStorage(db),
        )
    assert await db.transaction(lambda conn: conn.execute(
        "SELECT post_id FROM legacy_checkpoints"
    ).fetchall()) == [("10",)]
    assert await db.seen_count("sess1", "tag") == 0


@pytest.mark.asyncio
async def test_legacy_opaque_checkpoint_suppresses_full_blog_fetch_once(db):
    await db.add_subscription("sess1", "blog", "someuser")
    await db.import_legacy_checkpoint(
        "sess1", "blog", "someuser", "opaque-floor"
    )
    posts = _make_posts(["opaque-new", "opaque-old"])
    send = AsyncMock(return_value=True)
    with patch("core.scheduler.fetch_blog_posts", return_value=posts):
        await _check_blog_sub(
            _make_sub("someuser", sub_type="blog"), AsyncMock(), db, send,
            AuthorBlockStorage(db),
        )
    send.assert_not_awaited()
    assert await db.filter_unseen_session(
        "sess1", "blog", ["opaque-new", "opaque-old"]
    ) == []


@pytest.mark.asyncio
async def test_blog_session_fills_push_slots_when_enriched_post_is_blocked(db):
    await db.add_subscription("sess1", "blog", "someuser")
    await db.add_author_block("sess1", "username", "blockeduser", "blockeduser")
    blocks = AuthorBlockStorage(db)
    sub = _make_sub("someuser", sub_type="blog", session_id="sess1")
    posts = _make_posts([f"p{i}" for i in range(8)])
    for post in posts:
        post.completeness |= {"author_username"}
    sent: list[str] = []

    async def send_func(session_id, text, images):
        sent.append(text)
        return True

    post_ids = [p.post_id for p in posts]

    with (
        patch("core.scheduler.fetch_blog_posts", return_value=posts),
        patch(
            "core.scheduler._enrich_blog_posts",
            side_effect=_enrich_with_blocked_p2,
        ),
    ):
        await db.mark_seen_session("sess1", "blog", ["warmup"])
        await _check_blog_sub(sub, AsyncMock(), db, send_func, blocks)

        assert len(sent) == 5
        assert "帖子p2" not in "\n".join(sent)
        assert "帖子p5" in "\n".join(sent)
        assert await db.filter_unseen_session("sess1", "blog", post_ids) == ["p6", "p7"]
        assert await db.filter_unsent("sess1", post_ids) == ["p2", "p6", "p7"]

        await _check_blog_sub(sub, AsyncMock(), db, send_func, blocks)

    assert len(sent) == 7
    assert await db.filter_unseen_session("sess1", "blog", post_ids) == []
    assert await db.filter_unsent("sess1", post_ids) == ["p2"]
