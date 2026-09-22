"""视频贴：把 LOFTER 的视频下载到本地，再作为视频消息段发出去。"""

import uuid
from pathlib import Path

import aiohttp

VIDEO_DIR_NAME = "videos"
CHUNK_SIZE = 1 << 16


def video_filename(post_id: str) -> str:
    """视频消息段不带文件名，磁盘上用帖子 ID 就够，顺带保证不同帖子不会撞名。"""
    return f"{post_id}.mp4"


async def download_video(url: str, target: Path, *, max_bytes: int, timeout: int = 300) -> None:
    """下载视频到 target。

    先写临时文件再原子改名。同一个链接同时在两个会话里被解析时，两个任务各写各的
    临时文件，不会交错写坏同一个目标文件；失败时也只删自己那份，不碰已有的成品。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.part")
    try:
        await _stream_to_file(url, temp, max_bytes=max_bytes, timeout=timeout)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    temp.replace(target)


async def _stream_to_file(url: str, target: Path, *, max_bytes: int, timeout: int) -> None:
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=client_timeout) as session, session.get(url) as response:
        response.raise_for_status()
        _reject_oversized(int(response.headers.get("Content-Length") or 0), max_bytes)
        await _write_chunks(response, target, max_bytes)


async def _write_chunks(response, target: Path, max_bytes: int) -> None:
    written = 0
    with target.open("wb") as f:
        async for chunk in response.content.iter_chunked(CHUNK_SIZE):
            written += len(chunk)
            _reject_oversized(written, max_bytes)
            f.write(chunk)


def _reject_oversized(size: int, max_bytes: int) -> None:
    if size > max_bytes:
        raise RuntimeError(f"视频体积超过上限 {max_bytes // (1 << 20)}MB")
