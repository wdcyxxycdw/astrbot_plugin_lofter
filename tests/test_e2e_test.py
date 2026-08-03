import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.scheduler as scheduler_module
from core.e2e_test import E2ETestRunner, format_report
from core.errors import DWRIdentityError, SourceChallengeError
from core.parser import Post
from core.source_scan import SourcePage


def _post(post_id: str, *, owner: str = "private-owner") -> Post:
    return Post(
        post_id=post_id,
        title="private-title",
        summary="private-body",
        images=["https://img.example/private.jpg"],
        author="private-author",
        author_username=owner,
        tags=["摄影"],
        url=f"https://{owner}.lofter.com/post/{post_id}",
        publish_time="2026-08-01 12:00:00",
        source="test",
        completeness=frozenset({
            "title",
            "summary",
            "images",
            "author",
            "author_username",
            "tags",
            "url",
            "publish_time",
        }),
        provenance={
            "title": "test",
            "summary": "test",
            "images": "test",
            "author": "test",
            "author_username": "test",
            "tags": "test",
            "url": "test",
            "publish_time": "test",
        },
    )


def _page(items: list[Post], source: str) -> SourcePage:
    return SourcePage(
        items=items,
        source=source,
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=len(items),
        dropped_count=0,
        complete=True,
    )


class _OfflineSource:
    def __init__(
        self,
        normal: list[Post],
        dwr: list[Post],
        *,
        dwr_error: BaseException | None = None,
    ) -> None:
        self.normal = normal
        self.dwr = dwr
        self.dwr_error = dwr_error
        self.calls: list[tuple] = []
        self.posts = {post.url: post for post in [*normal, *dwr]}

    async def list_tag(self, tag, cursor, limit, sort):
        self.calls.append(("tag", tag, cursor, limit, sort))
        if cursor == "v1:dwr:0":
            if self.dwr_error is not None:
                raise self.dwr_error
            return _page(self.dwr, "dwr")
        return _page(self.normal, "mobile_tag")

    async def get_post(self, url):
        self.calls.append(("post", url))
        return self.posts[url]

    async def list_blog(self, username, cursor, limit):
        self.calls.append(("blog", username, cursor, limit))
        return _page(list(self.posts.values()), "mobile_blog")


async def _runner(
    source: _OfflineSource,
    *,
    send_result: bool = True,
    send_side_effect=None,
):
    scheduler_task = asyncio.create_task(asyncio.Event().wait())
    scheduler = SimpleNamespace(_task=scheduler_task, _interval=1800)
    send = AsyncMock(return_value=send_result)
    if send_side_effect is not None:
        send.side_effect = send_side_effect
    return E2ETestRunner(source, scheduler, send), send, scheduler_task


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_live_health_check_uses_temp_db_and_production_delivery_chain():
    baseline = _post("1a_1")
    candidate = _post("1a_2")
    source = _OfflineSource([baseline], [candidate])
    runner, send, scheduler_task = await _runner(source)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    assert len(results) == 7
    assert all(result.status == "pass" for result in results)
    assert runner._runtime is not None
    assert not os.path.exists(runner._runtime.temporary.name)
    send.assert_awaited_once()
    session_id, sent_post, header, source_types = send.await_args.args
    assert session_id == "qq:real-session"
    assert sent_post.post_id == candidate.post_id
    assert header.startswith("【Lofter E2E 测试】")
    assert source_types == frozenset({"tag"})
    assert ("tag", "摄影", None, 20, "new") in source.calls
    assert ("tag", "摄影", "v1:dwr:0", 20, "new") in source.calls

    report = format_report(results)
    assert "总体状态：HEALTHY" in report
    assert "DWR：已真实验证" in report
    assert "真实发送：尝试 1，adapter accepted=是" in report
    for secret in (
        baseline.post_id,
        candidate.post_id,
        baseline.url,
        baseline.author_username,
        baseline.title,
        baseline.summary,
    ):
        assert secret not in report


@pytest.mark.asyncio
async def test_forced_dwr_failure_is_independent_and_payload_free():
    secret_url = "https://private-owner.lofter.com/post/1a_2"
    error = DWRIdentityError("post_id_conflict", "postId", "postUrl")
    source = _OfflineSource([_post("1a_1")], [], dwr_error=error)
    runner, send, scheduler_task = await _runner(source)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    assert results[1].status == "pass"
    assert results[2].status == "fail"
    assert results[2].facts == {"dwr_verified": False}
    assert results[3].health == "inconclusive"
    assert results[4].status == results[5].status == "skip"
    send.assert_not_awaited()
    report = format_report(results)
    assert "总体状态：DEGRADED" in report
    assert "post_id_conflict:postId+postUrl" in report
    assert secret_url not in report
    assert "private-owner" not in report


@pytest.mark.asyncio
async def test_insufficient_fixture_is_inconclusive_and_sends_nothing():
    source = _OfflineSource([_post("1a_1")], [])
    runner, send, scheduler_task = await _runner(source)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    assert results[3].health == "inconclusive"
    assert results[4].status == results[5].status == "skip"
    send.assert_not_awaited()
    assert "总体状态：INCONCLUSIVE" in format_report(results)


@pytest.mark.asyncio
async def test_pending_assertion_failure_skips_adapter():
    source = _OfflineSource([_post("1a_1")], [_post("1a_2")])
    runner, send, scheduler_task = await _runner(source)
    runner._delivery_row = AsyncMock(return_value=None)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    assert results[4].status == "fail"
    assert results[4].facts == {"pending_verified": False}
    assert results[5].status == "skip"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_rejection_is_degraded_without_seen_ack():
    source = _OfflineSource([_post("1a_1")], [_post("1a_2")])
    runner, send, scheduler_task = await _runner(source, send_result=False)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    delivery = results[5]
    assert delivery.status == "fail"
    assert delivery.health == "degraded"
    assert delivery.facts == {
        "send_attempts": 1,
        "adapter_accepted": False,
    }
    send.assert_awaited_once()
    assert "总体状态：DEGRADED" in format_report(results)


@pytest.mark.asyncio
async def test_scheduler_send_timeout_is_inconclusive(monkeypatch):
    async def never_returns(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduler_module, "SEND_TIMEOUT_SECONDS", 0.01)
    source = _OfflineSource([_post("1a_1")], [_post("1a_2")])
    runner, send, scheduler_task = await _runner(
        source, send_side_effect=never_returns
    )
    runner.POLL_SECONDS = 0.2

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    assert results[5].health == "inconclusive"
    assert results[5].facts == {
        "send_attempts": 1,
        "adapter_accepted": "unknown",
    }
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_adapter_timeout_is_inconclusive_and_cleanup_cancels_bridge():
    async def never_returns(*args):
        await asyncio.Event().wait()

    source = _OfflineSource([_post("1a_1")], [_post("1a_2")])
    runner, send, scheduler_task = await _runner(
        source, send_side_effect=never_returns
    )
    runner.POLL_SECONDS = 0.02

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    assert results[5].health == "inconclusive"
    assert results[5].facts == {
        "send_attempts": 1,
        "adapter_accepted": "unknown",
    }
    assert results[-1].facts == {"temp_db_cleaned": True}
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_still_removes_temporary_database():
    source = _OfflineSource([_post("1a_1")], [_post("1a_2")])
    runner, _, scheduler_task = await _runner(source)
    runner._step_02_normal_tag = AsyncMock(side_effect=asyncio.CancelledError)

    try:
        with pytest.raises(asyncio.CancelledError):
            await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    assert runner._runtime is not None
    assert not os.path.exists(runner._runtime.temporary.name)
    assert runner._cleanup_complete is True


def test_report_classifies_challenge_as_inconclusive_without_raw_payload():
    source = _OfflineSource([], [])
    runner = E2ETestRunner(
        source,
        SimpleNamespace(_task=None, _interval=1800),
        AsyncMock(),
    )
    raw = "private response body"
    result = runner._fail(
        "真实 DWR fallback",
        1,
        SourceChallengeError(),
        [],
        facts={"dwr_verified": False},
    )

    report = format_report([result])

    assert result.health == "inconclusive"
    assert "总体状态：INCONCLUSIVE" in report
    assert raw not in report
