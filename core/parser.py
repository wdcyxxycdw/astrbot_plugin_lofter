import asyncio
import html as html_module
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
    content: str = ""


_IMG_CDN_RE = re.compile(
    r'(https://imglf\d+\.lf127\.net/img/[A-Za-z0-9/_=.+%-]+\.(?:jpg|png|gif|webp))'
    r'\?[^"\'<>\s]*quality='
)


_BODY_SELECTORS = ["div.txtcont", "div.ct"]
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def _extract_body_text(soup: BeautifulSoup) -> str:
    paragraphs = soup.find_all("p", id=lambda x: x and x.startswith("p_"))
    if paragraphs:
        lines = [p.get_text() for p in paragraphs]
        raw = "\n".join(lines)
        return _MULTI_NEWLINE_RE.sub("\n\n", raw).strip()
    for sel in _BODY_SELECTORS:
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 50:
            raw = el.get_text(separator="\n").strip()
            return _MULTI_NEWLINE_RE.sub("\n\n", raw)
    return ""


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


async def parse_post_page(html: str, url: str) -> Post:
    """从单篇帖子页面 HTML 提取结构化内容。跨主题稳定，依赖 <meta> 标签。"""
    def _parse() -> Post:
        soup = _make_soup(html)

        raw_title = soup.title.string.strip() if soup.title and soup.title.string else ""
        if "-" in raw_title:
            title, author = raw_title.rsplit("-", 1)
            title, author = title.strip(), author.strip()
        else:
            title, author = raw_title.strip(), ""

        summary = ""
        tags: list[str] = []
        for meta in soup.find_all("meta"):
            name = (meta.get("name") or "").lower()
            content = meta.get("content") or ""
            if name == "description":
                text = html_module.unescape(content).strip()
                summary = text[:300] + ("…" if len(text) > 300 else "")
            elif name == "keywords":
                tags = [t.strip() for t in content.split(",") if t.strip()]

        seen: set[str] = set()
        images: list[str] = []
        for base_url in _IMG_CDN_RE.findall(html):
            if base_url not in seen:
                seen.add(base_url)
                images.append(base_url)

        content = _extract_body_text(soup)

        return Post(
            post_id=_extract_post_id_from_url(url),
            title=title,
            author=author,
            summary=summary,
            tags=tags,
            images=images,
            url=url,
            content=content,
        )

    return await asyncio.get_running_loop().run_in_executor(None, _parse)
