"""
真实集成测试：需要配置环境变量后运行

    export LOFTER_COOKIE="your_cookie_here"
    export LOFTER_POST_URL="https://username.lofter.com/post/xxx"
    export LOFTER_TAG="标签名"
    export LOFTER_BLOG="用户名"

    uv run pytest tests/test_real.py -v -s
"""

import os
import pytest

from core.client import LofterClient
from core.parser import parse_post, parse_blog_posts

COOKIE = os.getenv("LOFTER_COOKIE", "")
POST_URL = os.getenv("LOFTER_POST_URL", "")
TAG = os.getenv("LOFTER_TAG", "")
BLOG = os.getenv("LOFTER_BLOG", "")


def skip_if_missing(*vars_):
    missing = [v for v in vars_ if not v]
    return pytest.mark.skipif(bool(missing), reason=f"缺少环境变量")


@pytest.mark.asyncio
@skip_if_missing(COOKIE, POST_URL)
async def test_real_parse_post():
    client = LofterClient(COOKIE)
    html = await client.get(POST_URL)

    print(f"\n[HTML 长度] {len(html)} 字符")

    post = await parse_post(html, POST_URL, max_images=9)
    assert post is not None, "解析结果为 None，可能 div.content 选择器不匹配，请检查实际 HTML"

    print(f"[post_id]  {post.post_id}")
    print(f"[title]    {post.title}")
    print(f"[images]   {len(post.images)} 张: {post.images}")
    print(f"[text 前100字] {post.text[:100]}")

    assert post.post_id, "未能提取 post_id"


@pytest.mark.asyncio
@skip_if_missing(COOKIE, TAG)
async def test_real_parse_tag():
    client = LofterClient(COOKIE)
    url = f"https://www.lofter.com/tag/{TAG}"
    html = await client.get(url)

    print(f"\n[标签页 HTML 长度] {len(html)} 字符")

    posts = await parse_blog_posts(html)
    print(f"[解析到帖子数] {len(posts)}")
    for p in posts[:3]:
        print(f"  - {p.post_id} | {p.title} | {p.url}")

    assert len(posts) > 0, "标签页未解析到任何帖子（标签页为 JS 渲染，此项可能无法通过 HTML 解析）"


@pytest.mark.asyncio
@skip_if_missing(COOKIE, BLOG)
async def test_real_parse_blog():
    client = LofterClient(COOKIE)
    url = f"https://{BLOG}.lofter.com"
    html = await client.get(url)

    print(f"\n[博主页 HTML 长度] {len(html)} 字符")

    posts = await parse_blog_posts(html)
    print(f"[解析到帖子数] {len(posts)}")
    for p in posts[:3]:
        print(f"  - {p.post_id} | {p.title} | {p.url}")

    assert len(posts) > 0, "博主页未解析到任何帖子，请检查 parse_blog_posts 中的选择器"
