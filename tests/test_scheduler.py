import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from core.db import LofterDB
from lofter import LofterClient, Post
from core.scheduler import (
    SubscriptionScheduler,
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

def _client_returning(*results) -> LofterClient:
    client = LofterClient()
    client.fetch_post = AsyncMock(side_effect=list(results))
    return client


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


SUBSCRIBED_AT = 1_700_000_000
NEW_MS = (SUBSCRIBED_AT + 3600) * 1000
OLD_MS = (SUBSCRIBED_AT - 3600) * 1000


def _make_sub(
    target: str,
    role: str = "subscribe",
    sub_type: str = "tag",
    session_id: str = "sess1",
    created_at: int = SUBSCRIBED_AT,
) -> Subscription:
    return Subscription(
        id=1, session_id=session_id, type=sub_type, role=role, target=target, created_at=created_at,
    )


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
    client = _client_returning(Post(post_id="服务端ID", title="帖子标题", summary="这是摘要", author="作者名"))

    result = await _enrich_blog_posts([BARE_POST], client)

    assert len(result) == 1
    assert result[0].title == "帖子标题"
    assert result[0].author == "作者名"
    assert result[0].summary == "这是摘要"
    assert result[0].post_id == "abc123"


@pytest.mark.asyncio
async def test_enrich_fallback_on_error():
    client = _client_returning(Exception("network error"))

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
    client = _client_returning(
        Post(post_id="服务端1", title="标题1", summary="", author="作者"),
        Post(post_id="服务端2", title="标题2", summary="", author="作者"),
    )

    result = await _enrich_blog_posts(posts, client)

    assert result[0].title == "标题1"
    assert result[1].title == "标题2"
    assert [call.args[0] for call in client.fetch_post.await_args_list] == [p.url for p in posts]


# ── 聚合标签轮询 ──────────────────────────────────────────────────────────────

def _make_posts(ids: list[str], tags: list[str] | None = None, published_ms: int = NEW_MS) -> list[Post]:
    return [
        Post(
            post_id=pid,
            title=f"帖子{pid}",
            summary="",
            url=f"https://u.lofter.com/post/{pid}",
            tags=tags or [],
            publish_time_ms=published_ms,
        )
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
    new_post = Post(
        post_id="p3", title="新帖", summary="", url="https://u.lofter.com/post/p3", publish_time_ms=NEW_MS,
    )

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
        Post(
            post_id="p1", title="可见", summary="", author="可见作者",
            url="https://a.lofter.com/post/p1", publish_time_ms=NEW_MS,
        ),
        Post(
            post_id="p2", title="屏蔽", summary="", author="屏蔽作者",
            url="https://b.lofter.com/post/p2", publish_time_ms=NEW_MS,
        ),
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
            publish_time_ms=NEW_MS,
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


# ── 只推订阅之后发布的内容 ────────────────────────────────────────────────────


def _paging_client(pages: dict[int, list[Post]], requested: list[int]) -> LofterClient:
    client = LofterClient()

    async def fetch(tag, *, offset=0, **kwargs):
        requested.append(offset)
        return pages.get(offset, [])

    client.fetch_tag_posts = fetch
    return client


@pytest.mark.asyncio
async def test_tag_paging_stops_at_the_first_page_older_than_the_subscription(db):
    """新订阅一个大标签不能被一路翻到底。

    线上就是这么炸的：test 标签翻到 offset 919，把 858 条 2005-2023 年的旧帖排进了待发送队列，
    按每轮 5 条要往群里推十几个小时。
    """
    pages = {
        0: _make_posts(["n1", "n2"], published_ms=NEW_MS),
        2: _make_posts(["o1", "o2"], published_ms=OLD_MS),
        4: _make_posts(["o3", "o4"], published_ms=OLD_MS),
    }
    requested: list[int] = []
    client = _paging_client(pages, requested)
    await db.mark_seen_session("sess1", "tag", ["warmup"])

    posts = await fetch_tag_posts(
        ["原神"], client, db=db, session_id="sess1", cutoffs={"原神": SUBSCRIBED_AT * 1000},
    )

    assert [p.post_id for p in posts] == ["n1", "n2"]
    assert requested == [0, 2]
    assert [p.post_id for p in await db.pending_posts("sess1", "tag", "")] == ["n1", "n2"]


@pytest.mark.asyncio
async def test_posts_published_before_the_subscription_are_not_pushed(db):
    """订阅之前就存在的帖子不是「新内容」，哪怕这个会话从没见过它们。"""
    subs = [_make_sub("原神", "subscribe")]
    posts = _make_posts(["old1", "old2"], tags=["原神"], published_ms=OLD_MS)
    sent: list[str] = []

    async def send_func(session_id, text, images):
        sent.append(text)

    await db.mark_seen_session("sess1", "tag", ["warmup"])

    with patch("core.scheduler.fetch_tag_posts", return_value=posts):
        await _check_tag_session("sess1", subs, AsyncMock(), db, send_func, AuthorBlockStorage(db))

    assert sent == []
    assert await db.pending_posts("sess1", "tag", "") == []


@pytest.mark.asyncio
async def test_a_post_without_a_publish_time_is_treated_as_old(db):
    """时间缺失时宁可漏推：当成新帖就等于放任整段历史刷进群里。"""
    subs = [_make_sub("原神", "subscribe")]
    posts = _make_posts(["p1"], tags=["原神"], published_ms=0)
    sent: list[str] = []

    async def send_func(session_id, text, images):
        sent.append(text)

    await db.mark_seen_session("sess1", "tag", ["warmup"])

    with patch("core.scheduler.fetch_tag_posts", return_value=posts):
        await _check_tag_session("sess1", subs, AsyncMock(), db, send_func, AuthorBlockStorage(db))

    assert sent == []


@pytest.mark.asyncio
async def test_a_post_matching_two_tags_uses_the_earlier_subscription(db):
    """帖子同时命中新旧两个订阅时按早的那个算，那次订阅本来就该收到它。

    按晚的那个算会把「早订阅的标签本该收到的新内容」一起吞掉。
    """
    subs = [
        _make_sub("原神", created_at=SUBSCRIBED_AT),
        _make_sub("崩铁", created_at=SUBSCRIBED_AT + 7200),
    ]
    between = _make_posts(["between"], tags=["原神", "崩铁"], published_ms=NEW_MS)
    before_both = _make_posts(["before"], tags=["原神", "崩铁"], published_ms=OLD_MS)
    sent: list[str] = []

    async def send_func(session_id, text, images):
        sent.append(text)

    await db.mark_seen_session("sess1", "tag", ["warmup"])

    with patch("core.scheduler.fetch_tag_posts", return_value=between + before_both):
        await _check_tag_session("sess1", subs, AsyncMock(), db, send_func, AuthorBlockStorage(db))

    assert len(sent) == 1
    assert "帖子between" in sent[0]


@pytest.mark.asyncio
async def test_blog_posts_published_before_the_subscription_are_not_pushed(db):
    """博主订阅同理：只推订阅之后发的新文章，不补推博主的历史作品。"""
    sub = _make_sub("author", sub_type="blog")
    posts = _make_posts(["old1"], published_ms=OLD_MS)
    sent: list[str] = []

    async def send_func(session_id, text, images):
        sent.append(text)

    await db.mark_seen_session("sess1", "blog", ["warmup"])

    with patch("core.scheduler.fetch_blog_posts", return_value=posts):
        await _check_blog_sub(sub, AsyncMock(), db, send_func, AuthorBlockStorage(db))

    assert sent == []
    assert await db.pending_posts("sess1", "blog", "author") == []
