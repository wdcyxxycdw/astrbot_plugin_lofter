from core.parser import Post

DIVIDER = "──────────────"


def format_post(post: Post, header: str = "", include_time: bool = False, body: str = "") -> str:
    """格式化帖子为统一的消息文本格式。

    Args:
        post: Post 数据对象
        header: 可选的消息头
        include_time: 是否在作者信息中显示发布时间
        body: 可选的正文内容覆盖（优先于 post.summary）

    Returns:
        格式化后的消息文本
    """
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
