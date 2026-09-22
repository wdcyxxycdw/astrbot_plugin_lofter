"""文字贴：字数统计，以及把长文整理成 .txt 文件。"""

import contextlib
import time
import uuid
from pathlib import Path

from .files import safe_filename
from .formatter import DIVIDER

TEXT_DIR_NAME = "articles"
DEFAULT_FILE_THRESHOLD = 1000
KEEP_SECONDS = 3600


def post_word_count(detail) -> int:
    """优先用 LOFTER 自己给的字数；缺失时按正文长度算，保证展示和阈值用同一个数。"""
    return detail.word_count or len(detail.post.content)


def text_filename(post_id: str) -> str:
    """磁盘上按帖子 ID 存。标题会撞（《无题》《第一章》），撞了就会发出别人的正文。"""
    return f"{post_id}.txt"


def text_display_name(title: str, post_id: str) -> str:
    """接收方看到的文件名，仍旧取文章标题。"""
    return safe_filename(title, post_id, ".txt")


def prune_old_texts(directory: Path, *, keep_seconds: int = KEEP_SECONDS) -> None:
    """清掉上一轮遗留的全文文件。发送是异步的，删不掉正在占用的就跳过。"""
    if not directory.is_dir():
        return
    deadline = time.time() - keep_seconds
    for pattern in ("*.txt", "*.part"):
        _prune(directory, pattern, deadline)


def _prune(directory: Path, pattern: str, deadline: float) -> None:
    for path in directory.glob(pattern):
        with contextlib.suppress(OSError):
            if path.stat().st_mtime < deadline:
                path.unlink()


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
    """先写临时文件再原子改名，免得同一篇文章被并发解析时读到半截内容。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / text_filename(post.post_id)
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.part")
    try:
        temp.write_text(build_text_file(post, count), encoding="utf-8")
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    temp.replace(path)
    return path
