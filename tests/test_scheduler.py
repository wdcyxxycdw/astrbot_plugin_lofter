import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from core.db import LofterDB
from core.parser import Post
from core.scheduler import (
    SubscriptionScheduler,
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
)


@pytest_asyncio.fixture
async def db(tmp_path):
    d = LofterDB(str(tmp_path / "test.db"))
    await d.initialize()
    yield d
    await d.close()


def _make_sub(target: str, role: str = "subscribe", sub_type: str = "tag", session_id: str = "sess1") -> Subscription:
    return Subscription(id=1, session_id=session_id, type=sub_type, role=role, target=target)


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
@pytest.mark.parametrize("failure", ["tag", "first-blog"])
async def test_one_subscription_failure_does_not_block_later_subscriptions(db, failure):
    storage = SubscriptionStorage(db)
    await storage.add("sess1", "tag", "A")
    await storage.add("sess1", "blog", "first-blog")
    await storage.add("sess1", "blog", "second-blog")
    scheduler = SubscriptionScheduler(
        storage, AsyncMock(), db, AsyncMock(), block_storage=AuthorBlockStorage(db),
    )
    checked = []

    async def check_blog(sub, *args):
        checked.append(sub.target)
        if sub.target == failure:
            raise RuntimeError("发送失败")

    tag_check = AsyncMock(side_effect=RuntimeError("发送失败") if failure == "tag" else None)
    with patch("core.scheduler._check_tag_session", tag_check), patch("core.scheduler._check_blog_sub", check_blog):
        with pytest.raises(RuntimeError, match="发送失败"):
            await scheduler._poll_all(session_id="sess1")
    assert checked == ["first-blog", "second-blog"]


@pytest.mark.asyncio
async def test_enrich_success():
    client = AsyncMock()
    client.get.return_value = RICH_HTML

    result = await _enrich_blog_posts([BARE_POST], client)

    assert len(result) == 1
    assert result[0].title == "帖子标题"
    assert result[0].author == "作者名"
    assert result[0].summary == "这是摘要"
    assert result[0].post_id == "abc123"


@pytest.mark.asyncio
async def test_enrich_fallback_on_error():
    client = AsyncMock()
    client.get.side_effect = Exception("network error")

    result = await _enrich_blog_posts([BARE_POST], client)

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
    send = AsyncMock()
    await _push_tag_posts("sess1", [FULL_POST], TAG_RULE, send)
    text = send.call_args[0][1]
    assert "【标签「原创」有新内容】" in text


@pytest.mark.asyncio
async def test_push_blog_label():
    send = AsyncMock()
    await _push_blog_post("sess1", FULL_POST, "someuser", send)
    text = send.call_args[0][1]
    assert "【博主「someuser」有新内容】" in text


@pytest.mark.asyncio
async def test_push_includes_author_summary_tags_url():
    send = AsyncMock()
    await _push_tag_posts("sess1", [FULL_POST], TAG_RULE, send)
    text = send.call_args[0][1]
    assert "作者：作者名" in text
    assert "这是摘要" in text
    assert "#原创" in text
    assert FULL_POST.url in text


@pytest.mark.asyncio
async def test_push_includes_images():
    send = AsyncMock()
    await _push_tag_posts("sess1", [FULL_POST], TAG_RULE, send)
    images = send.call_args[0][2]
    assert images == FULL_POST.images


@pytest.mark.asyncio
async def test_push_no_title_shows_placeholder():
    send = AsyncMock()
    post = Post(post_id="p2", title="", summary="有摘要", url="https://u.lofter.com/post/p2")
    await _push_tag_posts("sess1", [post], TAG_RULE, send)
    text = send.call_args[0][1]
    assert "(无标题)" in text


@pytest.mark.asyncio
async def test_push_reversed_order():
    send = AsyncMock()
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
    send = AsyncMock()
    posts = [
        Post(post_id=f"p{i}", title=f"帖子{i}", summary="", url=f"https://u.lofter.com/post/p{i}")
        for i in range(8)
    ]
    await _push_tag_posts("sess1", posts, TAG_RULE, send)
    assert send.call_count == 5


@pytest.mark.asyncio
async def test_enrich_serial_order():
    posts = [
        Post(post_id="p1", title="", summary="", url="https://u.lofter.com/post/p1"),
        Post(post_id="p2", title="", summary="", url="https://u.lofter.com/post/p2"),
    ]
    client = AsyncMock()
    client.get.side_effect = [
        "<html><head><title>标题1-作者</title></head></html>",
        "<html><head><title>标题2-作者</title></head></html>",
    ]

    result = await _enrich_blog_posts(posts, client)

    assert result[0].title == "标题1"
    assert result[1].title == "标题2"


# ── 聚合标签轮询 ──────────────────────────────────────────────────────────────

def _make_posts(ids: list[str], tags: list[str] | None = None) -> list[Post]:
    return [
        Post(post_id=pid, title=f"帖子{pid}", summary="", url=f"https://u.lofter.com/post/{pid}", tags=tags or [])
        for pid in ids
    ]


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
    subs = [
        _make_sub("原神", "subscribe"),
        _make_sub("崩铁", "subscribe"),
        _make_sub("R18", "exclude"),
    ]

    posts_genshin = _make_posts(["g1", "g2"], tags=["原神"])
    posts_hsr = _make_posts(["h1"], tags=["崩铁"])
    posts_r18 = _make_posts(["r1"], tags=["原神", "R18"])

    async def mock_fetch(search_tags, client, **kwargs):
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
async def test_warmup_no_push(db):
    """冷启动（seen_count=0）时 mark_seen 但不推送"""
    subs = [_make_sub("原神", "subscribe")]
    posts = _make_posts(["p1", "p2", "p3"])

    async def mock_fetch(search_tags, client, **kwargs):
        return posts

    sent: list[str] = []

    async def send_func(session_id, text, images):
        sent.append(text)

    client = AsyncMock()

    with patch("core.scheduler.fetch_tag_posts", side_effect=mock_fetch):
        await _check_tag_session("sess1", subs, client, db, send_func, AuthorBlockStorage(db))

    assert sent == []
    count = await db.seen_count("sess1", "tag")
    assert count == 3


@pytest.mark.asyncio
async def test_new_post_pushed_after_warmup(db):
    """warmup 后新帖应该被推送"""
    subs = [_make_sub("原神", "subscribe")]
    old_posts = _make_posts(["p1", "p2"])
    new_post = Post(post_id="p3", title="新帖", summary="", url="https://u.lofter.com/post/p3")

    async def mock_fetch_old(search_tags, client, **kwargs):
        return old_posts

    async def mock_fetch_new(search_tags, client, **kwargs):
        return old_posts + [new_post]

    sent: list[str] = []

    async def send_func(session_id, text, images):
        sent.append(text)

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

    with patch("core.scheduler.fetch_tag_posts", return_value=posts):
        await db.mark_seen_session("sess1", "tag", ["warmup"])
        await _check_tag_session("sess1", subs, AsyncMock(), db, send_func, blocks)

    assert len(sent) == 1
    assert "可见" in sent[0]
    assert await db.filter_unseen_session("sess1", "tag", ["p1", "p2"]) == []
    assert await db.filter_unsent("sess1", ["p1", "p2"]) == ["p2"]


@pytest.mark.asyncio
async def test_tag_session_overflow_posts_remain_pending_until_next_poll(db):
    subs = [_make_sub("原神", "subscribe")]
    posts = _make_posts([f"p{i}" for i in range(8)])
    sent: list[str] = []

    async def send_func(session_id, text, images):
        sent.append(text)

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

    with patch("core.scheduler.fetch_blog_posts", return_value=posts):
        await db.mark_seen_session("sess1", "blog", ["warmup"])
        await _check_blog_sub(sub, AsyncMock(), db, send_func, blocks)

    assert sent == []
    assert await db.filter_unseen_session("sess1", "blog", ["p1"]) == []
    assert await db.filter_unsent("sess1", ["p1"]) == ["p1"]


@pytest.mark.asyncio
async def test_blog_session_overflow_posts_remain_pending_until_next_poll(db):
    sub = _make_sub("someuser", sub_type="blog", session_id="sess1")
    posts = _make_posts([f"p{i}" for i in range(8)])
    sent: list[str] = []

    async def send_func(session_id, text, images):
        sent.append(text)

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
async def test_blog_session_fills_push_slots_when_enriched_post_is_blocked(db):
    await db.add_author_block("sess1", "username", "blockeduser", "blockeduser")
    blocks = AuthorBlockStorage(db)
    sub = _make_sub("someuser", sub_type="blog", session_id="sess1")
    posts = _make_posts([f"p{i}" for i in range(8)])
    sent: list[str] = []

    async def send_func(session_id, text, images):
        sent.append(text)

    async def enrich(enrich_posts, client):
        enriched = []
        for post in enrich_posts:
            author_username = "blockeduser" if post.post_id == "p2" else ""
            enriched.append(
                Post(
                    post_id=post.post_id,
                    title=post.title,
                    summary=post.summary,
                    url=post.url,
                    author_username=author_username,
                )
            )
        return enriched

    post_ids = [p.post_id for p in posts]

    with (
        patch("core.scheduler.fetch_blog_posts", return_value=posts),
        patch("core.scheduler._enrich_blog_posts", side_effect=enrich),
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
