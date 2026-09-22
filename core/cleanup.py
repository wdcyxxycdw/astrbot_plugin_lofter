"""定期清理生成的临时文件。

articles/ 和 videos/ 里的东西发出去就没用了。原先只在解析同类帖子时顺手清一次，
没人解析就一直堆着——视频尤其占地方。
"""

import asyncio
import contextlib
import time
from pathlib import Path

from astrbot.api import logger

from .text_post import TEXT_DIR_NAME
from .video import VIDEO_DIR_NAME

DEFAULT_TTL_HOURS = 1
SWEEP_INTERVAL_SECONDS = 600
TEMP_PATTERNS = {
    TEXT_DIR_NAME: ("*.txt", "*.part"),
    VIDEO_DIR_NAME: ("*.mp4", "*.part"),
}


def prune_directory(directory: Path, patterns: tuple[str, ...], *, keep_seconds: int) -> None:
    if not directory.is_dir():
        return
    deadline = time.time() - keep_seconds
    for pattern in patterns:
        for path in directory.glob(pattern):
            _remove_if_stale(path, deadline)


def _remove_if_stale(path: Path, deadline: float) -> None:
    """发送是异步的，删不掉正在占用的文件就跳过，下一轮再说。"""
    with contextlib.suppress(OSError):
        if path.stat().st_mtime < deadline:
            path.unlink()


def sweep(data_dir: Path, *, keep_seconds: int) -> None:
    for name, patterns in TEMP_PATTERNS.items():
        prune_directory(data_dir / name, patterns, keep_seconds=keep_seconds)


class TempFileCleaner:
    def __init__(
        self,
        data_dir: Path,
        *,
        ttl_hours: int = DEFAULT_TTL_HOURS,
        interval_seconds: float = SWEEP_INTERVAL_SECONDS,
    ):
        self._data_dir = data_dir
        # 保留期短于一轮发送耗时的话，会把刚写完还没发出去的文件删掉，所以钳到一小时。
        self._keep_seconds = max(1, ttl_hours) * 3600
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._loop())
        logger.info("Lofter 临时文件清理已启动，保留 %d 小时", self._keep_seconds // 3600)

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Lofter 临时文件清理已停止")

    async def _loop(self):
        """先扫一遍再等，这样上次退出时留下的文件重启就能清掉。"""
        while True:
            try:
                sweep(self._data_dir, keep_seconds=self._keep_seconds)
            except Exception:
                logger.exception("Lofter 临时文件清理失败，将在下一轮重试")
            await asyncio.sleep(self._interval)
