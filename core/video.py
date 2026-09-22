"""视频贴：把 LOFTER 的视频下载到本地，再作为文件发出去。"""

import contextlib
import re
import time
from pathlib import Path

import aiohttp

VIDEO_DIR_NAME = "videos"
KEEP_SECONDS = 3600
CHUNK_SIZE = 1 << 16

_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def video_filename(title: str, post_id: str) -> str:
    """用文章标题当文件名。去掉文件系统不接受的字符，标题为空时退回帖子 ID。"""
    name = _UNSAFE_CHARS.sub("", title).strip().rstrip(".")
    return f"{name[:80] or post_id}.mp4"


def prune_old_videos(directory: Path, *, keep_seconds: int = KEEP_SECONDS) -> None:
    """清掉上一轮遗留的视频。发送是异步的，删不掉正在占用的文件就跳过。"""
    if not directory.is_dir():
        return
    deadline = time.time() - keep_seconds
    for path in directory.glob("*.mp4"):
        with contextlib.suppress(OSError):
            if path.stat().st_mtime < deadline:
                path.unlink()


async def download_video(url: str, target: Path, *, max_bytes: int, timeout: int = 300) -> None:
    """下载视频到 target；失败时清掉半截文件再把原因抛出去。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        await _stream_to_file(url, target, max_bytes=max_bytes, timeout=timeout)
    except Exception:
        target.unlink(missing_ok=True)
        raise


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
