"""
真实集成测试：需要配置环境变量后运行

    export LOFTER_COOKIE="your_cookie_here"
    export LOFTER_POST_URL="https://username.lofter.com/post/xxx"
    export LOFTER_TAG="标签名"
    export LOFTER_BLOG="用户名"

    LOFTER_RUN_LIVE=1 uv run pytest tests/test_real.py -v -s
"""

import os
from contextlib import asynccontextmanager

import pytest

from core.client import LofterClient
from core.content_source import DefaultContentSource
from core.dwr_parser import parse_dwr_response
from core.parser import parse_post_page, parse_blog_posts
from core.scheduler import _enrich_blog_posts

COOKIE = os.getenv("LOFTER_COOKIE", "")
POST_URL = os.getenv("LOFTER_POST_URL", "")
IMAGE_POST_URL = os.getenv("LOFTER_IMAGE_POST_URL", "")
IMAGE_EXPECTED_COUNT = os.getenv("LOFTER_IMAGE_EXPECTED_COUNT", "")
TAG = os.getenv("LOFTER_TAG", "")
BLOG = os.getenv("LOFTER_BLOG", "")
RUN_LIVE = os.getenv("LOFTER_RUN_LIVE") == "1"

pytestmark = [
    pytest.mark.real,
    pytest.mark.skipif(not RUN_LIVE, reason="需要设置 LOFTER_RUN_LIVE=1"),
]


def skip_if_missing(*vars_):
    missing = [v for v in vars_ if not v]
    return pytest.mark.skipif(bool(missing), reason="缺少环境变量")


@asynccontextmanager
async def _live_client():
    client = LofterClient(COOKIE)
    await client.initialize()
    try:
        yield client
    finally:
        await client.close()


@asynccontextmanager
async def _live_source():
    source = DefaultContentSource()
    source.update_cookie(COOKIE)
    await source.initialize()
    try:
        yield source
    finally:
        await source.close()


@pytest.mark.asyncio
@skip_if_missing(COOKIE, POST_URL)
async def test_real_parse_post():
    async with _live_client() as client:
        html = await client.get(POST_URL, credentialed=True)
    print(f"\n[HTML 长度] {len(html)} 字符")

    post = await parse_post_page(html, POST_URL)
    assert post is not None, "解析结果为 None，可能选择器不匹配"

    print(f"[post_id]  {post.post_id}")
    print(f"[title]    {post.title}")
    print(f"[images]   {len(post.images)} 张: {post.images}")
    print(f"[summary 前100字] {post.summary[:100] if post.summary else ''}")
    assert post.post_id, "未能提取 post_id"


@pytest.mark.asyncio
@skip_if_missing(IMAGE_POST_URL)
async def test_real_default_source_image_post():
    async with _live_source() as source:
        post = await source.get_post(IMAGE_POST_URL)
    expected_count = int(IMAGE_EXPECTED_COUNT) if IMAGE_EXPECTED_COUNT else None
    print(
        f"[image source] {post.source} known={'images' in post.completeness} "
        f"count={len(post.images)}"
    )
    assert post.source == "mobile_detail"
    assert "images" in post.completeness
    assert post.images
    if expected_count is not None:
        assert len(post.images) == expected_count


@pytest.mark.asyncio
@skip_if_missing(COOKIE, TAG)
async def test_real_tag_dwr():
    """通过 DWR TagBean.search 获取标签帖子列表"""
    async with _live_client() as client:
        raw = await client.search_tag(TAG, limit=20)
    print(f"\n[DWR 响应长度] {len(raw)} 字符")

    posts = await parse_dwr_response(raw)
    print(f"[解析到帖子数] {len(posts)}")
    for p in posts[:3]:
        print(f"  - {p.post_id} | {p.url}")
        if p.summary:
            print(f"    summary 前50字: {p.summary[:50]}")

    assert len(posts) > 0, "未从 DWR 响应中解析到帖子"
    assert all(p.post_id for p in posts), "存在缺少 post_id 的帖子"
    assert all(p.url for p in posts), "存在缺少 url 的帖子"


@pytest.mark.asyncio
@skip_if_missing(COOKIE, BLOG)
async def test_real_parse_blog():
    url = f"https://{BLOG}.lofter.com"
    async with _live_client() as client:
        html = await client.get(url, credentialed=True)
    print(f"\n[博主页 HTML 长度] {len(html)} 字符")

    posts = await parse_blog_posts(html)
    print(f"[解析到帖子数] {len(posts)}")
    for p in posts[:3]:
        print(f"  - {p.post_id} | {p.title} | {p.url}")

    assert len(posts) > 0, "博主页未解析到任何帖子"


@pytest.mark.asyncio
@skip_if_missing(COOKIE, BLOG)
async def test_real_enrich_blog_posts():
    async with _live_source() as source:
        page = await source.list_blog(BLOG, None, 20)
        posts = page.items
        assert posts, "博主页未解析到任何帖子"
        enriched = await _enrich_blog_posts(posts[:1], source)
    assert len(enriched) == 1

    post = enriched[0]
    print(f"\n[post_id] {post.post_id}")
    print(f"[title]   {post.title}")
    print(f"[author]  {post.author}")
    print(f"[summary 前50字] {post.summary[:50] if post.summary else '(空)'}")
    print(f"[images]  {len(post.images)} 张")

    assert post.post_id == posts[0].post_id, "enriched 应保留原始 post_id"
