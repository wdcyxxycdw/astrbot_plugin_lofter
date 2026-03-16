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


def _make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _extract_images(tag: Tag, max_count: int = 9) -> list[str]:
    images = []
    for img in tag.find_all("img"):
        if not isinstance(img, Tag):
            continue
        src = img.get("src") or img.get("data-src")
        if isinstance(src, str):
            src = src.split("?")[0]
            images.append(src)
        if len(images) >= max_count:
            break
    return images


def _extract_post_id_from_url(url: str) -> str:
    m = re.search(r"/post/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else ""


async def parse_post(html: str, url: str = "", max_images: int = 9) -> Optional[Post]:
    soup = await asyncio.get_running_loop().run_in_executor(
        None, partial(_make_soup, html)
    )
    content_div = soup.find("div", class_="content")
    if not isinstance(content_div, Tag):
        return None

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""

    text = content_div.get_text(separator="\n", strip=True)
    images = _extract_images(content_div, max_images)
    post_id = _extract_post_id_from_url(url)

    return Post(post_id=post_id, title=title, text=text, images=images, url=url)


async def parse_posts_list(html: str) -> list[Post]:
    """解析标签页/博主主页的帖子列表"""
    soup = await asyncio.get_running_loop().run_in_executor(
        None, partial(_make_soup, html)
    )
    posts = []
    # Lofter 列表页中每条帖子的卡片
    for card in soup.find_all("div", class_=re.compile(r"post|item")):
        if not isinstance(card, Tag):
            continue
        link = card.find("a", href=re.compile(r"\.lofter\.com/post/"))
        if not isinstance(link, Tag):
            continue
        url = str(link.get("href", ""))
        post_id = _extract_post_id_from_url(url)
        if not post_id:
            continue
        title_tag = card.find(["h2", "h3", "a"])
        title = title_tag.get_text(strip=True) if title_tag else ""
        img = card.find("img")
        cover = ""
        if isinstance(img, Tag):
            cover = str(img.get("src") or img.get("data-src") or "")
        posts.append(Post(post_id=post_id, title=title, text="", images=[cover] if cover else [], url=url))
    return posts


async def parse_search_results(html: str) -> list[Post]:
    """解析搜索结果页"""
    return await parse_posts_list(html)
