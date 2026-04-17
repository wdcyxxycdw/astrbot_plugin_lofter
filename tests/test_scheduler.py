import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from core.db import LofterDB
from core.parser import Post
from core.scheduler import (
    _check_tag_session,
    _enrich_blog_posts,
    _push_posts,
    _build_tag_rule,
)
from core.storage import Subscription

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


# ── _push_posts ───────────────────────────────────────────────────────────────

TAG_SUB = Subscription(id=1, session_id="sess1", type="tag", role="subscribe", target="原创")
BLOG_SUB = Subscription(id=2, session_id="sess1", type="blog", role="subscribe", target="someuser")

FULL_POST = Post(
    post_id="p1",
    title="帖子标题",
    author="作者名",
    summary="这是摘要",
    tags=["tag1", "tag2"],
    images=["https://img1.jpg", "https://img2.jpg"],
    url="https://user.lofter.com/post/p1",
)


@pytest.mark.asyncio
async def test_push_tag_label():
    send = AsyncMock()
    await _push_posts([FULL_POST], TAG_SUB, send)
    text = send.call_args[0][1]
    assert "【标签「原创」有新内容】" in text


@pytest.mark.asyncio
async def test_push_blog_label():
    send = AsyncMock()
    await _push_posts([FULL_POST], BLOG_SUB, send)
    text = send.call_args[0][1]
    assert "【博主「someuser」有新内容】" in text


@pytest.mark.asyncio
async def test_push_includes_author_summary_tags_url():
    send = AsyncMock()
    await _push_posts([FULL_POST], TAG_SUB, send)
    text = send.call_args[0][1]
    assert "作者：作者名" in text
    assert "这是摘要" in text
    assert "#tag1" in text
    assert FULL_POST.url in text


@pytest.mark.asyncio
async def test_push_includes_images():
    send = AsyncMock()
    await _push_posts([FULL_POST], TAG_SUB, send)
    images = send.call_args[0][2]
    assert images == FULL_POST.images


@pytest.mark.asyncio
async def test_push_no_title_shows_placeholder():
    send = AsyncMock()
    post = Post(post_id="p2", title="", summary="有摘要", url="https://u.lofter.com/post/p2")
    await _push_posts([post], TAG_SUB, send)
    text = send.call_args[0][1]
    assert "(无标题)" in text


@pytest.mark.asyncio
async def test_push_reversed_order():
    send = AsyncMock()
    posts = [
        Post(post_id=f"p{i}", title=f"帖子{i}", summary="", url=f"https://u.lofter.com/post/p{i}")
        for i in range(3)
    ]
    await _push_posts(posts, TAG_SUB, send)
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
    await _push_posts(posts, TAG_SUB, send)
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

    client = AsyncMock()

    with patch("core.scheduler.fetch_tag_posts", side_effect=mock_fetch):
        await db.mark_seen_session("sess1", "tag", ["warmup"])
        await _check_tag_session("sess1", subs, client, db, send_func)

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

    async def mock_fetch(search_tags, client):
        return posts

    sent: list[str] = []

    async def send_func(session_id, text, images):
        sent.append(text)

    client = AsyncMock()

    with patch("core.scheduler.fetch_tag_posts", side_effect=mock_fetch):
        await _check_tag_session("sess1", subs, client, db, send_func)

    assert sent == []
    count = await db.seen_count("sess1", "tag")
    assert count == 3


@pytest.mark.asyncio
async def test_new_post_pushed_after_warmup(db):
    """warmup 后新帖应该被推送"""
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

    client = AsyncMock()

    with patch("core.scheduler.fetch_tag_posts", side_effect=mock_fetch_old):
        await _check_tag_session("sess1", subs, client, db, send_func)

    assert sent == []

    with patch("core.scheduler.fetch_tag_posts", side_effect=mock_fetch_new):
        await _check_tag_session("sess1", subs, client, db, send_func)

    assert len(sent) == 1
    assert "新帖" in sent[0]
