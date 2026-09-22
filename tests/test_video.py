import time

import pytest
from aiohttp import web

from core.video import download_video, prune_old_videos, video_filename

MB = 1 << 20


def test_video_filename_uses_the_article_title():
    assert video_filename("我的作品", "abc_123") == "我的作品.mp4"


@pytest.mark.parametrize("title", ["a/b", "a\\b", "a:b", "a*b", "a?b", 'a"b', "a<b", "a>b", "a|b", "a\nb"])
def test_video_filename_drops_characters_the_filesystem_rejects(title):
    name = video_filename(title, "abc_123")
    assert name == "ab.mp4"


def test_video_filename_truncates_very_long_titles():
    assert video_filename("标" * 200, "abc_123") == "标" * 80 + ".mp4"


@pytest.mark.parametrize("title", ["", "   ", "///", "..."])
def test_video_filename_falls_back_to_post_id_when_title_is_unusable(title):
    assert video_filename(title, "abc_123") == "abc_123.mp4"


def test_prune_old_videos_removes_stale_files_and_keeps_fresh_ones(tmp_path):
    stale = tmp_path / "old.mp4"
    fresh = tmp_path / "new.mp4"
    other = tmp_path / "keep.txt"
    for path in (stale, fresh, other):
        path.write_bytes(b"x")
    old_time = time.time() - 7200
    import os

    os.utime(stale, (old_time, old_time))

    prune_old_videos(tmp_path, keep_seconds=3600)

    assert not stale.exists()
    assert fresh.exists()
    assert other.exists()


def test_prune_old_videos_tolerates_missing_directory(tmp_path):
    prune_old_videos(tmp_path / "nope")


@pytest.fixture
async def video_server():
    """真实 HTTP 服务，避免用 mock 假装下载成功。"""
    state = {"body": b"", "declare_length": True}

    async def handler(request):
        headers = {} if state["declare_length"] else {"Content-Type": "video/mp4"}
        if state["declare_length"]:
            return web.Response(body=state["body"], headers=headers)
        response = web.StreamResponse(headers=headers)
        await response.prepare(request)
        await response.write(state["body"])
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
