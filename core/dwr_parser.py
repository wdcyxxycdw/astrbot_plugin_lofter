"""
解析 Lofter DWR 响应，基于 dwr_engine 真正执行 JS，完整展开对象引用。
"""

import html
import json
import re
from datetime import datetime
from typing import Optional

from .dwr_engine import execute_dwr
from .parser import Post


def _extract_post_id(url: str) -> str:
    m = re.search(r"/post/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else ""


def _fmt_time(ts_ms) -> str:
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _strip_html(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _extract_summary(value) -> str:
    if isinstance(value, str):
        text = _strip_html(value).strip()
    elif isinstance(value, dict):
        text = _strip_html(value.get("content") or value.get("text") or "").strip()
    else:
        return ""
    return text[:300] + ("…" if len(text) > 300 else "")


def _extract_images(value) -> list[str]:
    if isinstance(value, list):
        return [u.split("?")[0] for u in value if isinstance(u, str)]
    if isinstance(value, str):
        try:
            urls = json.loads(value)
            if isinstance(urls, list):
                return [u.split("?")[0] for u in urls if isinstance(u, str)]
        except Exception:
            pass
        if value.startswith("http"):
            return [value.split("?")[0]]
    return []


def _map_post(item: dict) -> Optional[Post]:
    post = item.get("post")
    if not isinstance(post, dict):
        return None

    url = post.get("blogPageUrl", "")
    post_id = _extract_post_id(url)
    if not post_id:
        return None

    blog = post.get("blogInfo") or {}
    author = blog.get("blogNickName") or blog.get("blogName") or "" if isinstance(blog, dict) else ""

    raw_tags = post.get("tag") or ""
    tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

    return Post(
        post_id=post_id,
        url=url,
        title=post.get("title") or "",
        summary=_extract_summary(post.get("dirContent") or post.get("content") or ""),
        author=author,
        tags=tags,
        publish_time=_fmt_time(post.get("publishTime") or 0),
        images=_extract_images(post.get("firstImageUrl")),
    )


async def parse_dwr_response(body: str) -> list[Post]:
    """执行 DWR 响应，返回 Post 列表，按发布时间倒序。"""
    items = await execute_dwr(body)
    posts = []
    for item in items:
        p = _map_post(item)
        if p:
            posts.append(p)
    posts.sort(key=lambda p: p.publish_time, reverse=True)
    return posts
