"""
解析 Lofter DWR (TagBean.search) 响应格式。

响应是一段 JS 赋值代码，每个帖子对象形如：
  sN.blogPageUrl="https://...lofter.com/post/xxx";
  sN.id=12345678;
  sN.title="\\uXXXX...";
  sN.content="<p ...>...</p>";
"""

import re
from dataclasses import dataclass


@dataclass
class DwrPost:
    url: str
    post_id: str      # URL 中的 permalink（如 7edb52dc_34d8e9650）
    title: str = ""
    content: str = ""


def _decode(s: str) -> str:
    """还原 DWR 字符串中的 \\uXXXX Unicode 转义。"""
    try:
        return s.encode("utf-8").decode("unicode_escape")
    except Exception:
        return s


def parse_dwr_response(body: str) -> list[DwrPost]:
    """从 DWR 响应文本中提取帖子列表，按 publishTime 降序排列。"""
    url_map: dict[str, str] = {}
    time_map: dict[str, int] = {}
    title_map: dict[str, str] = {}
    content_map: dict[str, str] = {}

    for m in re.finditer(r'(s\d+)\.blogPageUrl="([^"]+)"', body):
        url_map[m.group(1)] = m.group(2)

    for m in re.finditer(r'(s\d+)\.publishTime=(\d+)', body):
        time_map[m.group(1)] = int(m.group(2))

    for m in re.finditer(r'(s\d+)\.title="((?:[^"\\]|\\.)*)"', body):
        title_map[m.group(1)] = _decode(m.group(2))

    for m in re.finditer(r'(s\d+)\.content="((?:[^"\\]|\\.)*)"', body):
        content_map[m.group(1)] = m.group(2)  # 保留原始 HTML，调用方可按需解析

    posts = []
    for var, url in url_map.items():
        post_id = _extract_permalink(url)
        if not post_id:
            continue
        posts.append(DwrPost(
            url=url,
            post_id=post_id,
            title=title_map.get(var, ""),
            content=content_map.get(var, ""),
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
