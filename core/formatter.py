from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.parser import Post

DIVIDER = "──────────────"


def format_post(post: Post, header: str = "", include_time: bool = False, body: str = "") -> str:
    blocks = []

    if header:
        blocks.append(header)

    title_line = f"▸ {post.title or '(无标题)'}"
    author_line = ""
    if post.author:
        author_line = f"作者：{post.author}"
        if include_time and post.publish_time:
            author_line += f"  {post.publish_time}"
    elif include_time and post.publish_time:
        author_line = post.publish_time

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
