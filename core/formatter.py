from __future__ import annotations
from typing import TYPE_CHECKING

from lofter.models import POST_TYPE_PHOTO, POST_TYPE_TEXT

if TYPE_CHECKING:
    from lofter import Post

DIVIDER = "──────────────"


def is_photo_post(detail) -> bool:
    """post_type 是 LOFTER 给的权威判据；3/5/6 这些没见过的类型退回按有无图片判断。"""
    if detail.post_type in (POST_TYPE_TEXT, POST_TYPE_PHOTO):
        return detail.post_type == POST_TYPE_PHOTO
    return bool(detail.post.images)


def format_post(
    post: Post, header: str = "", include_time: bool = False, body: str = "", word_count: int = 0,
) -> str:
    blocks = []

    if header:
        blocks.append(header)

    title_line = f"▸ {post.title or '(无标题)'}"
    meta = []
    if post.author:
        meta.append(f"作者：{post.author}")
    if include_time and post.publish_time:
        meta.append(post.publish_time)
    if word_count:
        meta.append(f"{word_count} 字")
    author_line = "  ".join(meta)

    if author_line:
        blocks.append(f"{title_line}\n{author_line}")
    else:
        blocks.append(title_line)

    if post.tags:
        blocks.append(f"#{' #'.join(post.tags)}")

    display_body = body or post.summary
    if display_body:
        blocks.append(display_body)

    blocks.append(f"{DIVIDER}\n{post.url}")

    return "\n\n".join(blocks)
