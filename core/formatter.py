from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.parser import Post

from .parser import POST_FIELDS

DIVIDER = "──────────────"


def visible_images(post: Post) -> list[str]:
    return post.images if post.has_fields({"images"}) else []


def _title_author_block(post: Post, include_time: bool) -> str:
    title = post.title or "(无标题)" if post.has_fields({"title"}) else "(标题未知)"
    title_line = f"▸ {title}"
    author = post.author if post.has_fields({"author"}) else ""
    publish_time = (
        post.publish_time
        if include_time and post.has_fields({"publish_time"})
        else ""
    )
    if not author:
        return f"{title_line}\n{publish_time}" if publish_time else title_line
    author_line = f"作者：{author}"
    if publish_time:
        author_line += f"  {publish_time}"
    return f"{title_line}\n{author_line}"


def _diagnostic(post: Post) -> str:
    missing = sorted(POST_FIELDS - post.completeness)
    detail = f"来源：{post.source}"
    if missing:
        detail += f"；部分字段未知：{', '.join(missing)}"
    return detail


def format_post(
    post: Post,
    header: str = "",
    include_time: bool = False,
    body: str | None = None,
) -> str:
    blocks = [header] if header else []
    blocks.append(_title_author_block(post, include_time))
    if post.has_fields({"tags"}) and post.tags:
        blocks.append(f"#{' #'.join(post.tags)}")
    display_body = post.summary if body is None else body
    body_field = "summary" if body is None else "content"
    if post.has_fields({body_field}) and display_body:
        blocks.append(display_body)
    url = post.url if post.has_fields({"url"}) and post.url else "(链接未知)"
    blocks.append(f"{DIVIDER}\n{url}")
    if post.source != "unknown" or post.completeness != POST_FIELDS:
        blocks.append(_diagnostic(post))
    return "\n\n".join(blocks)
