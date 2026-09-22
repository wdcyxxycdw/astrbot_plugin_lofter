import asyncio
import contextlib

import pytest
from aiohttp import web

from core.video import CHUNK_SIZE, download_video, video_filename

MB = 1 << 20


def test_video_filename_is_the_post_id():
    assert video_filename("abc_123") == "abc_123.mp4"


def test_different_posts_never_share_a_filename():
    """标题会撞（《无题》《第一章》），帖子 ID 不会——撞名会让一方发出另一方的视频。"""
    assert video_filename("abc_123") != video_filename("abc_124")


@pytest.fixture
async def video_server():
    """真实 HTTP 服务，避免用 mock 假装下载成功。"""
    state = {"body": b"", "declare_length": True, "chunk_delay": 0}

    async def handler(request):
        headers = {} if state["declare_length"] else {"Content-Type": "video/mp4"}
        if state["declare_length"]:
            return web.Response(body=state["body"], headers=headers)
        response = web.StreamResponse(headers=headers)
        await response.prepare(request)
        for start in range(0, len(state["body"]), CHUNK_SIZE):
            await response.write(state["body"][start:start + CHUNK_SIZE])
            if state["chunk_delay"]:
                await asyncio.sleep(state["chunk_delay"])
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get("/video.mp4", handler)

    async def missing(request):
        return web.Response(status=404)

    app.router.add_get("/missing.mp4", missing)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    state["url"] = f"http://127.0.0.1:{port}/video.mp4"
    state["missing_url"] = f"http://127.0.0.1:{port}/missing.mp4"
    yield state
    await runner.cleanup()


async def test_download_video_writes_the_whole_body(video_server, tmp_path):
    video_server["body"] = b"\x00\x01" * 1024
    target = tmp_path / "out.mp4"

    await download_video(video_server["url"], target, max_bytes=10 * MB)

    assert target.read_bytes() == b"\x00\x01" * 1024


async def test_download_video_creates_missing_directories(video_server, tmp_path):
    video_server["body"] = b"data"
    target = tmp_path / "videos" / "out.mp4"

    await download_video(video_server["url"], target, max_bytes=MB)

    assert target.exists()


async def test_download_video_rejects_declared_size_over_the_limit(video_server, tmp_path):
    video_server["body"] = b"x" * 4096
    target = tmp_path / "out.mp4"

    with pytest.raises(RuntimeError, match="超过上限"):
        await download_video(video_server["url"], target, max_bytes=1024)

    assert not target.exists()


async def test_download_video_aborts_mid_stream_when_length_is_not_declared(video_server, tmp_path):
    video_server["body"] = b"x" * (256 * 1024)
    video_server["declare_length"] = False
    target = tmp_path / "out.mp4"

    with pytest.raises(RuntimeError, match="超过上限"):
        await download_video(video_server["url"], target, max_bytes=1024)

    assert not target.exists()


async def test_download_video_cleans_up_after_an_http_error(video_server, tmp_path):
    target = tmp_path / "out.mp4"

    with pytest.raises(Exception):
        await download_video(video_server["missing_url"], target, max_bytes=MB)

    assert not target.exists()


async def test_failed_download_leaves_an_existing_file_alone(video_server, tmp_path):
    """失败清理只能删自己的临时文件，不能碰同名的已有成品。"""
    target = tmp_path / "out.mp4"
    target.write_bytes(b"previously downloaded")

    with pytest.raises(Exception):
        await download_video(video_server["missing_url"], target, max_bytes=MB)

    assert target.read_bytes() == b"previously downloaded"


@pytest.mark.parametrize("url_key", ["url", "missing_url"])
async def test_download_video_leaves_no_temporary_files(video_server, tmp_path, url_key):
    video_server["body"] = b"data"
    target = tmp_path / "out.mp4"

    with contextlib.suppress(Exception):
        await download_video(video_server[url_key], target, max_bytes=MB)

    assert list(tmp_path.glob("*.part")) == []


async def test_target_file_only_appears_once_the_download_finishes(video_server, tmp_path):
    """下载途中目标路径必须还不存在。

    否则并发解析时，另一个任务会读到半截文件——发出去的就是一段坏视频。
    """
    body = b"x" * (4 * CHUNK_SIZE)
    video_server["body"] = body
    video_server["declare_length"] = False
    video_server["chunk_delay"] = 0.05
    target = tmp_path / "out.mp4"

    task = asyncio.create_task(download_video(video_server["url"], target, max_bytes=MB))
    await asyncio.sleep(0.08)
    visible_midway = target.exists()
    await task

    assert not visible_midway
    assert target.read_bytes() == body
