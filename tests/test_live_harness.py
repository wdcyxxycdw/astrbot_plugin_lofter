from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import tests.test_live_hybrid as live_hybrid
from core.parser import POST_FIELDS, Post
from tests.test_live_hybrid import (
    _assert_accepted,
    _assert_pending,
    _assert_sending,
    _block_outbound,
    _cleanup_runtime,
    _create_runtime,
    _install_safe_scheduler_logger,
    _validate_candidate,
    SESSION_ID,
)


def _candidate() -> Post:
    return Post(
        post_id="1a_2",
        title="synthetic",
        summary="synthetic",
        images=[f"https://img.example/{index}.jpg" for index in range(5)],
        author="Synthetic",
        author_username="demo",
        url="https://demo.lofter.com/post/1a_2",
        tags=["synthetic"],
        publish_time="2099-01-01 00:00:00",
        content="synthetic",
        source="mobile_detail",
        completeness=frozenset(POST_FIELDS),
        provenance={field: "mobile_detail" for field in POST_FIELDS},
    )


def _require(condition: bool, message: str) -> None:
    if not condition:
        pytest.fail(message)


def _require_no_scheduler_sensitive_values(
    messages: list[str], candidate: Post
) -> None:
    captured = "\n".join(messages)
    sensitive = (
        candidate.post_id,
        candidate.url,
        candidate.title,
        candidate.summary,
        candidate.content,
        *candidate.images,
        *candidate.tags,
    )
    if any(value in captured for value in sensitive):
        pytest.fail("scheduler logger captured sensitive candidate data")


def test_safe_scheduler_logger_rejects_dynamic_values(monkeypatch) -> None:
    candidate = _candidate()
    safe_logger = _install_safe_scheduler_logger(monkeypatch)

    safe_logger.warning("获取博主帖子详情失败 %s: %s")
    safe_logger.error("发送订阅推送超时 session=%s post=%s")
    safe_logger.error("发送订阅推送失败 session=%s post=%s: %s")

    with pytest.raises(AssertionError, match="dynamic_args"):
        safe_logger.warning(
            "获取博主帖子详情失败 %s: %s",
            candidate.url,
            candidate.content,
        )
    with pytest.raises(AssertionError, match="dynamic_args"):
        safe_logger.error(
            "发送订阅推送超时 session=%s post=%s",
            SESSION_ID,
            candidate.post_id,
        )
    with pytest.raises(AssertionError, match="log_key"):
        safe_logger.error("unexpected log key")

    _require_no_scheduler_sensitive_values(safe_logger.messages, candidate)


@pytest.mark.asyncio
async def test_safe_scheduler_logger_blocks_send_timeout_values(monkeypatch) -> None:
    candidate = _candidate()
    safe_logger = _install_safe_scheduler_logger(monkeypatch)
    claim = SimpleNamespace(
        session_id=SESSION_ID,
        post=candidate,
        sources=(SimpleNamespace(type="blog", target="demo"),),
    )

    async def immediate_timeout(*args, **kwargs):
        raise asyncio.TimeoutError

    async def successful_send(session_id, post, header, source_types):
        return True

    monkeypatch.setattr(
        live_hybrid.scheduler_module.asyncio, "wait_for", immediate_timeout
    )
    with pytest.raises(AssertionError, match="dynamic_args"):
        await live_hybrid.scheduler_module._send_claim(claim, successful_send)

    _require_no_scheduler_sensitive_values(safe_logger.messages, candidate)


@pytest.mark.asyncio
async def test_safe_scheduler_logger_blocks_send_exception_values(monkeypatch) -> None:
    candidate = _candidate()
    safe_logger = _install_safe_scheduler_logger(monkeypatch)
    claim = SimpleNamespace(
        session_id=SESSION_ID,
        post=candidate,
        sources=(SimpleNamespace(type="blog", target="demo"),),
    )

    class _Queue:
        async def claim_next(self, session_id):
            return SimpleNamespace(
                status=live_hybrid.scheduler_module.ClaimStatus.CLAIMED,
                delivery=claim,
            )

        async def release_failure(self, delivery, error):
            pytest.fail("scheduler logger should reject values before queue release")

    async def fail_send(session_id, post, header, source_types):
        raise RuntimeError(candidate.content)

    with pytest.raises(AssertionError, match="dynamic_args"):
        await live_hybrid.scheduler_module._drain_session_queue(
            SESSION_ID, _Queue(), fail_send
        )

    _require_no_scheduler_sensitive_values(safe_logger.messages, candidate)


@pytest.mark.asyncio
async def test_hybrid_harness_runs_full_production_flow_offline(
    tmp_path, monkeypatch
):
    candidate = _candidate()
    original = _validate_candidate(candidate)
    _block_outbound(monkeypatch)
    runtime = await _create_runtime(tmp_path, candidate, monkeypatch)
    try:
        await _assert_pending(runtime, candidate, original)
        await _assert_sending(runtime)
        await _assert_accepted(runtime, candidate, original)
    finally:
        await _cleanup_runtime(runtime)


@pytest.mark.asyncio
async def test_create_runtime_removes_files_when_build_fails(tmp_path, monkeypatch):
    async def fail_build(*args, **kwargs):
        raise RuntimeError("build failed")

    monkeypatch.setattr(live_hybrid, "_build_scheduler", fail_build)
    with pytest.raises(RuntimeError, match="build failed"):
        await _create_runtime(tmp_path, _candidate(), monkeypatch)
    _require(not list(tmp_path.iterdir()), "构建失败后遗留数据库文件")


@pytest.mark.asyncio
async def test_cleanup_accumulates_failures_and_attempts_every_phase(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "live-hybrid.db"
    for suffix in ("", "-wal", "-shm", ".lock"):
        db_path.with_name(db_path.name + suffix).touch()
    task = asyncio.create_task(asyncio.Event().wait())
    attempts: list[str] = []

    async def fail_close():
        attempts.append("db_close")
        raise RuntimeError("close failed")

    async def fail_gather(*args, **kwargs):
        attempts.append("task_gather")
        raise RuntimeError("task failed")

    native_unlink = type(db_path).unlink

    def fail_one_unlink(path, *args, **kwargs):
        attempts.append(f"unlink:{path.name}")
        if path.name.endswith("-wal"):
            raise OSError("unlink failed")
        return native_unlink(path, *args, **kwargs)

    native_gather = asyncio.gather
    monkeypatch.setattr(live_hybrid.asyncio, "gather", fail_gather)
    monkeypatch.setattr(type(db_path), "unlink", fail_one_unlink)
    runtime = SimpleNamespace(
        queue=SimpleNamespace(release_claim=asyncio.Event()),
        release_send=asyncio.Event(),
        poll_task=task,
        db=SimpleNamespace(close=fail_close),
        db_path=db_path,
    )
    try:
        with pytest.raises(RuntimeError) as raised:
            await _cleanup_runtime(runtime)
    finally:
        task.cancel()
        await native_gather(task, return_exceptions=True)

    assert str(raised.value) == (
        "live_hybrid_cleanup:task_gather,db_close,sidecar_unlink_wal,"
        "sidecar_residual_wal"
    )
    assert attempts == [
        "task_gather",
        "db_close",
        "unlink:live-hybrid.db",
        "unlink:live-hybrid.db-wal",
        "unlink:live-hybrid.db-shm",
        "unlink:live-hybrid.db.lock",
    ]
    _require(
        (tmp_path / "live-hybrid.db-wal").exists(),
        "unlink 失败的 sidecar 未保留给残留检查",
    )


@pytest.mark.asyncio
async def test_cleanup_removes_runtime_files_on_normal_path(tmp_path):
    db_path = tmp_path / "live-hybrid.db"
    for suffix in ("", "-wal", "-shm", ".lock"):
        db_path.with_name(db_path.name + suffix).touch()
    runtime = SimpleNamespace(
        queue=SimpleNamespace(release_claim=asyncio.Event()),
        release_send=asyncio.Event(),
        poll_task=None,
        db=SimpleNamespace(close=AsyncMock()),
        db_path=db_path,
    )

    await _cleanup_runtime(runtime)

    _require(not list(tmp_path.iterdir()), "正常 cleanup 后遗留数据库文件")
