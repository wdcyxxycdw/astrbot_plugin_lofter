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
    """解析帖子详情页。内容区为 div.ct，标题在 h2.ttl，正文在 div.txtcont。"""
    soup = await asyncio.get_running_loop().run_in_executor(
        None, partial(_make_soup, html)
    )
    ct = soup.find("div", class_="ct")
    if not isinstance(ct, Tag):
        return None

    title_tag = ct.find("h2", class_="ttl")
    title = title_tag.get_text(strip=True) if isinstance(title_tag, Tag) else ""

    txtcont = ct.find("div", class_="txtcont")
    text = txtcont.get_text(separator="\n", strip=True) if isinstance(txtcont, Tag) else ""

    images = _extract_images(ct, max_count=max_images)
    post_id = _extract_post_id_from_url(url)

    return Post(post_id=post_id, title=title, text=text, images=images, url=url)


async def parse_blog_posts(html: str) -> list[Post]:
    """解析博主主页的帖子列表。通过 h2.ttl > a 获取链接，避免重复。"""
    soup = await asyncio.get_running_loop().run_in_executor(
        None, partial(_make_soup, html)
    )
    posts = []
    seen: set[str] = set()
    for h2 in soup.find_all("h2", class_="ttl"):
        if not isinstance(h2, Tag):
            continue
        a = h2.find("a", href=re.compile(r"\.lofter\.com/post/"))
        if not isinstance(a, Tag):
            continue
        url = str(a.get("href", ""))
        post_id = _extract_post_id_from_url(url)
        if not post_id or post_id in seen:
            continue
        seen.add(post_id)
        title = a.get_text(strip=True)
        posts.append(Post(post_id=post_id, title=title, text="", url=url))
    return posts


async def parse_search_results(html: str) -> list[Post]:
    """解析搜索结果页（结构与博主页相同）。"""
    return await parse_blog_posts(html)
