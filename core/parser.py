import asyncio
import re
from dataclasses import dataclass, field
from functools import partial
from typing import Optional

from bs4 import BeautifulSoup, Tag


@dataclass
class Post:
    post_id: str
    title: str
    text: str
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


def _parse_post_sync(html: str, url: str, max_images: int) -> Optional[Post]:
    soup = _make_soup(html)

    author_tag = soup.select_one(".selfinfo h1 a")
    author = author_tag.get_text(strip=True) if author_tag else ""

    kw = soup.find("meta", attrs={"name": "Keywords"})
    tags = [t.strip() for t in kw["content"].split(",")] if kw and kw.get("content") else []

    title_tag = soup.find("title")
    raw_title = title_tag.get_text(strip=True) if title_tag else ""
    title = raw_title.rsplit("-", 1)[0].strip() if "-" in raw_title else raw_title

    text_div = soup.select_one(".postwrapper .text")
    full_text = text_div.get_text(separator="\n", strip=True) if isinstance(text_div, Tag) else ""
    summary = full_text[:300] + ("…" if len(full_text) > 300 else "")

    images = []
    for img in soup.select("div.img img"):
        src = img.get("src", "")
        if isinstance(src, str) and src:
            images.append(src.split("?")[0])
        if len(images) >= max_images:
            break

    month_tag = soup.select_one(".side .month a")
    day_tag = soup.select_one(".side .day a")
    if month_tag and day_tag:
        publish_time = f"{month_tag.get_text(strip=True)}/{day_tag.get_text(strip=True)}"
    else:
        publish_time = ""

    post_id = _extract_post_id_from_url(url)
    return Post(
        post_id=post_id,
        title=title,
        text=summary,
        images=images,
        author=author,
        url=url,
        tags=tags,
        publish_time=publish_time,
    )


async def parse_post(html: str, url: str = "", max_images: int = 9) -> Optional[Post]:
    return await asyncio.get_running_loop().run_in_executor(
        None, partial(_parse_post_sync, html, url, max_images)
    )


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
            posts.append(Post(post_id=post_id, title=a.get_text(strip=True), text="", url=href))
        return posts

    return await asyncio.get_running_loop().run_in_executor(None, _parse)
