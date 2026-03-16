import asyncio
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag


@dataclass
class Post:
    post_id: str
    title: str
    summary: str
    images: list[str] = field(default_factory=list)
    author: str = ""
    url: str = ""
    tags: list[str] = field(default_factory=list)
    publish_time: str = ""


def _make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _extract_post_id_from_url(url: str) -> str:
    m = re.search(r"/post/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else ""


async def parse_blog_posts(html: str) -> list[Post]:
    """解析博主主页的帖子列表。"""
    def _parse():
        soup = _make_soup(html)
        posts = []
        seen: set[str] = set()
        for a in soup.select("a[href*='.lofter.com/post/']"):
            if not isinstance(a, Tag):
                continue
            href = str(a.get("href", ""))
            post_id = _extract_post_id_from_url(href)
            if not post_id or post_id in seen:
                continue
            seen.add(post_id)
            posts.append(Post(post_id=post_id, title=a.get_text(strip=True), summary="", url=href))
        return posts

    return await asyncio.get_running_loop().run_in_executor(None, _parse)
