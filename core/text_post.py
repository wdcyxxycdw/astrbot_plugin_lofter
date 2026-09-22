"""文字贴：字数统计，以及把长文整理成 .txt 文件。"""

from pathlib import Path

from .files import safe_filename
from .formatter import DIVIDER

TEXT_DIR_NAME = "articles"
DEFAULT_FILE_THRESHOLD = 1000


def post_word_count(detail) -> int:
    """优先用 LOFTER 自己给的字数；缺失时按正文长度算，保证展示和阈值用同一个数。"""
    return detail.word_count or len(detail.post.content)


def text_filename(title: str, post_id: str) -> str:
    return safe_filename(title, post_id, ".txt")


def build_text_file(post, count: int) -> str:
    """文件正文：标题、作者、标签、字数、原链接，然后是全文。"""
    lines = [post.title or "(无标题)"]
    if post.author:
        lines.append(f"作者：{post.author}")
    if post.tags:
        lines.append(f"#{' #'.join(post.tags)}")
    lines.append(f"字数：{count}")
    lines.append(f"原文：{post.url}")
    lines.append("")
    lines.append(DIVIDER)
    lines.append("")
    lines.append(post.content)
    return "\n".join(lines)


def write_text_file(directory: Path, post, count: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / text_filename(post.title, post.post_id)
    path.write_text(build_text_file(post, count), encoding="utf-8")
    return path
