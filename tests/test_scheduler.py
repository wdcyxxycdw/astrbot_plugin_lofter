import pytest
from unittest.mock import AsyncMock

from core.parser import Post
from core.scheduler import _enrich_blog_posts

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
