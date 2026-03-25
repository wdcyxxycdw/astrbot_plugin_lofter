import pytest
from unittest.mock import AsyncMock, patch

from core.parser import Post
from core.scheduler import _enrich_blog_posts, _push_posts
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


@pytest.mark.asyncio
async def test_enrich_success():
    client = AsyncMock()
    client.get.return_value = RICH_HTML

    result = await _enrich_blog_posts([BARE_POST], client)

    assert len(result) == 1
    assert result[0].title == "帖子标题"
    assert result[0].author == "作者名"
    assert result[0].summary == "这是摘要"
    assert result[0].post_id == "abc123"  # 原始 ID 应被保留，不由 URL 重新提取


@pytest.mark.asyncio
async def test_enrich_fallback_on_error():
    client = AsyncMock()
    client.get.side_effect = Exception("network error")

    result = await _enrich_blog_posts([BARE_POST], client)

    # 降级：返回原始 bare post，不抛出异常
    assert len(result) == 1
    assert result[0].post_id == "abc123"
    assert result[0].title == ""  # bare post 无标题


# ── _push_posts ───────────────────────────────────────────────────────────────

TAG_SUB = Subscription(session_id="sess1", type="tag", target="原创")
BLOG_SUB = Subscription(session_id="sess1", type="blog", target="someuser")

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


# ── _enrich_blog_posts ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enrich_serial_order():
    """验证结果顺序与输入顺序一致（串行执行的副作用，非并发性验证）。"""
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
