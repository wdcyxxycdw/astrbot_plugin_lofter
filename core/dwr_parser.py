"""
解析 Lofter DWR (TagBean.search) 响应格式。
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class DwrPost:
    url: str
    post_id: str
    title: str = ""
    summary: str = ""
    author: str = ""
    tags: list[str] = field(default_factory=list)
    publish_time: str = ""
    images: list[str] = field(default_factory=list)


def _decode(s: str) -> str:
    """还原 DWR 字符串中的 \\uXXXX Unicode 转义。"""
    return re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), s)


def _str_fields(body: str, field_name: str) -> dict[str, str]:
    pattern = rf'(s\d+)\.{field_name}="((?:[^"\\]|\\.)*)"'
    return {m.group(1): _decode(m.group(2)) for m in re.finditer(pattern, body)}


def _ref_fields(body: str, field_name: str) -> dict[str, str]:
    pattern = rf'(s\d+)\.{field_name}=(s\d+)'
    return {m.group(1): m.group(2) for m in re.finditer(pattern, body)}


def _int_fields(body: str, field_name: str) -> dict[str, int]:
    pattern = rf'(s\d+)\.{field_name}=(\d+)'
    return {m.group(1): int(m.group(2)) for m in re.finditer(pattern, body)}


def _parse_image_urls(raw: str) -> list[str]:
    """将 DWR 中的 firstImageUrl JSON 数组字符串解析为 URL 列表。"""
    try:
        urls = json.loads(raw.replace('\\"', '"'))
        if isinstance(urls, list):
            return [u.split("?")[0] for u in urls if isinstance(u, str)]
    except Exception:
        pass
    if raw.startswith("http"):
        return [raw.split("?")[0]]
    return []


def _fmt_time(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M")


def parse_dwr_response(body: str) -> list[DwrPost]:
    url_map = _str_fields(body, "blogPageUrl")
    title_map = _str_fields(body, "title")
    dir_map = _str_fields(body, "dirContent")
    tag_map = _str_fields(body, "tag")
    time_map = _int_fields(body, "publishTime")
    blog_ref = _ref_fields(body, "blogInfo")
    nick_map = _str_fields(body, "blogNickName")
    img_map = _str_fields(body, "firstImageUrl")

    posts = []
    for var, url in url_map.items():
        post_id = _extract_permalink(url)
        if not post_id:
            continue

        blog_var = blog_ref.get(var, "")
        author = nick_map.get(blog_var, "")

        raw_summary = dir_map.get(var, "").strip()
        summary = raw_summary[:300] + ("…" if len(raw_summary) > 300 else "")

        raw_tags = tag_map.get(var, "")
        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

        ts = time_map.get(var, 0)
        publish_time = _fmt_time(ts) if ts else ""

        images = _parse_image_urls(img_map.get(var, ""))

        posts.append(DwrPost(
            url=url,
            post_id=post_id,
            title=title_map.get(var, ""),
            summary=summary,
            author=author,
            tags=tags,
            publish_time=publish_time,
            images=images,
        ))

    posts.sort(key=lambda p: time_map.get(_var_for_url(url_map, p.url), 0), reverse=True)
    return posts


def _extract_permalink(url: str) -> str:
    m = re.search(r"/post/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else ""


def _var_for_url(url_map: dict[str, str], url: str) -> str:
    for var, u in url_map.items():
        if u == url:
            return var
    return ""
