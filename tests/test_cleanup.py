import asyncio
import os
import time
from pathlib import Path

from core.cleanup import TempFileCleaner, prune_directory, sweep
from core.text_post import TEXT_DIR_NAME
from core.video import VIDEO_DIR_NAME


def make_stale(path: Path, age_seconds: int = 7200) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    old = time.time() - age_seconds
    os.utime(path, (old, old))
    return path


def make_fresh(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    return path


def test_prune_directory_removes_stale_files_and_keeps_fresh_ones(tmp_path):
    stale = make_stale(tmp_path / "old.txt")
    leftover = make_stale(tmp_path / "old.txt.deadbeef.part")
    fresh = make_fresh(tmp_path / "new.txt")

    prune_directory(tmp_path, ("*.txt", "*.part"), keep_seconds=3600)

    assert not stale.exists()
    assert not leftover.exists()
    assert fresh.exists()


def test_prune_directory_leaves_files_outside_its_patterns(tmp_path):
    stale_video = make_stale(tmp_path / "old.mp4")
    stale_other = make_stale(tmp_path / "lofter.db")

    prune_directory(tmp_path, ("*.mp4", "*.part"), keep_seconds=3600)

    assert not stale_video.exists()
    assert stale_other.exists()


def test_prune_directory_tolerates_missing_directory(tmp_path):
    prune_directory(tmp_path / "nope", ("*.txt",), keep_seconds=3600)


def test_sweep_covers_both_articles_and_videos(tmp_path):
    stale_text = make_stale(tmp_path / TEXT_DIR_NAME / "old.txt")
    stale_part = make_stale(tmp_path / TEXT_DIR_NAME / "old.txt.deadbeef.part")
    stale_video = make_stale(tmp_path / VIDEO_DIR_NAME / "old.mp4")
    fresh_video = make_fresh(tmp_path / VIDEO_DIR_NAME / "new.mp4")

    sweep(tmp_path, keep_seconds=3600)

    assert not stale_text.exists()
    assert not stale_part.exists()
    assert not stale_video.exists()
    assert fresh_video.exists()


async def test_the_cleaner_sweeps_without_anything_being_parsed(tmp_path):
    """定期清理的意义就在这：没人解析帖子，上次留下的文件也得清掉。"""
    stale = make_stale(tmp_path / TEXT_DIR_NAME / "old.txt")

    cleaner = TempFileCleaner(tmp_path, interval_seconds=0.01)
    cleaner.start()
    await asyncio.sleep(0.05)
    await cleaner.stop()

    assert not stale.exists()


async def test_the_cleaner_keeps_sweeping_after_the_first_round(tmp_path):
    cleaner = TempFileCleaner(tmp_path, interval_seconds=0.01)
    cleaner.start()
    await asyncio.sleep(0.05)
    stale = make_stale(tmp_path / VIDEO_DIR_NAME / "late.mp4")
    await asyncio.sleep(0.05)
    await cleaner.stop()

    assert not stale.exists()


async def test_the_cleaner_keeps_running_after_a_failed_round(tmp_path, monkeypatch):
    rounds = []

    def boom(data_dir, *, keep_seconds):
        rounds.append(data_dir)
        if len(rounds) == 1:
            raise OSError("磁盘炸了")

    monkeypatch.setattr("core.cleanup.sweep", boom)
    cleaner = TempFileCleaner(tmp_path, interval_seconds=0.01)
    cleaner.start()
    await asyncio.sleep(0.05)
    await cleaner.stop()

    assert len(rounds) > 1


async def test_the_cleaner_does_not_delete_a_file_it_just_wrote(tmp_path):
    """TTL 配成 0 会把刚写完还没发出去的文件删掉，所以下限钳在一小时。"""
    just_written = make_fresh(tmp_path / TEXT_DIR_NAME / "now.txt")

    cleaner = TempFileCleaner(tmp_path, ttl_hours=0, interval_seconds=0.01)
    cleaner.start()
    await asyncio.sleep(0.05)
    await cleaner.stop()

    assert just_written.exists()


async def test_stopping_a_cleaner_that_never_started_is_harmless(tmp_path):
    await TempFileCleaner(tmp_path).stop()
