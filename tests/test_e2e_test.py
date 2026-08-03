import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.scheduler as scheduler_module
from core.content_source import MobileTagDiagnostic
from core.e2e_test import E2ETestRunner, format_report
from core.errors import (
    DWREvidenceError,
    DWRIdentityError,
    SourceChallengeError,
    SourceSchemaError,
    SourceTimeoutError,
)
from core.parser import Post
from core.source_scan import SourcePage


def _post(
    post_id: str,
    *,
    owner: str = "private-owner",
    publish_time: str = "2026-08-01 12:00:00",
) -> Post:
    return Post(
        post_id=post_id,
        title="private-title",
        summary="private-body",
        images=["https://img.example/private.jpg"],
        author="private-author",
        author_username=owner,
        tags=["摄影"],
        url=f"https://{owner}.lofter.com/post/{post_id}",
        publish_time=publish_time,
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


def _page(
    items: list[Post],
    source: str,
    *,
    complete: bool = True,
    diagnostics: tuple[str, ...] = (),
    restarted: bool = False,
    evidence_items: tuple[Post, ...] = (),
) -> SourcePage:
    return SourcePage(
        items=items,
        source=source,
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=len(items),
        dropped_count=0 if complete else 1,
        complete=complete,
        diagnostics=diagnostics,
        restarted=restarted,
        evidence_items=evidence_items,
    )


class _OfflineSource:
    def __init__(
        self,
        mobile: list[Post],
        dwr: list[Post],
        *,
        production: list[Post] | None = None,
        mobile_reason: str | None = None,
        mobile_error: BaseException | None = None,
        dwr_error: BaseException | None = None,
        production_error: BaseException | None = None,
        detail_errors: dict[str, BaseException] | None = None,
        blog_error: BaseException | None = None,
        production_diagnostics: tuple[str, ...] = (),
    ) -> None:
        self.mobile = mobile
        self.dwr = dwr
        self.production = production if production is not None else mobile
        self.mobile_reason = mobile_reason
        self.mobile_error = mobile_error
        self.dwr_error = dwr_error
        self.production_error = production_error
        self.detail_errors = detail_errors or {}
        self.blog_error = blog_error
        self.production_diagnostics = production_diagnostics
        self.calls: list[tuple] = []
        self.posts = {
            post.url: post
            for post in [*mobile, *dwr, *self.production]
        }

    async def diagnose_mobile_tag(self, tag, limit, sort):
        self.calls.append(("mobile", tag, limit, sort))
        page = _page(
            self.mobile,
            "mobile_tag",
            complete=self.mobile_reason != "mobile_incomplete",
        )
        return MobileTagDiagnostic(
            page if self.mobile_error is None else None,
            (),
            self.mobile_reason,
            self.mobile_error,
        )

    async def list_tag(self, tag, cursor, limit, sort):
        self.calls.append(("tag", tag, cursor, limit, sort))
        if cursor == "v1:dwr:0":
            if self.dwr_error is not None:
                raise self.dwr_error
            return _page(self.dwr, "dwr")
        if cursor is None:
            if self.production_error is not None:
                raise self.production_error
            source = "dwr" if "fallback_dwr" in self.production_diagnostics else "mobile_tag"
            return _page(
                sorted(
                    self.production,
                    key=lambda post: post.publish_time,
                    reverse=True,
                ),
                source,
                diagnostics=self.production_diagnostics,
                restarted="fallback_dwr" in self.production_diagnostics,
            )
        raise SourceSchemaError("response")

    async def get_post(self, url):
        self.calls.append(("post", url))
        error = self.detail_errors.get(url)
        if error is not None:
            raise error
        post = self.posts.get(url)
        if post is None:
            raise SourceSchemaError("post.url")
        return post

    async def list_blog(self, username, cursor, limit):
        self.calls.append(("blog", username, cursor, limit))
        if self.blog_error is not None:
            raise self.blog_error
        return _page(
            sorted(
                self.posts.values(),
                key=lambda post: post.publish_time,
                reverse=True,
            ),
            "mobile_blog",
        )


async def _runner(
    source: _OfflineSource,
    *,
    send_result: bool = True,
    send_side_effect=None,
    scheduler_running: bool = True,
):
    if scheduler_running:
        scheduler_task = asyncio.create_task(asyncio.Event().wait())
    else:
        scheduler_task = asyncio.create_task(asyncio.sleep(0))
        await scheduler_task
    scheduler = SimpleNamespace(_task=scheduler_task, _interval=1800)
    send = AsyncMock(return_value=send_result)
    if send_side_effect is not None:
        send.side_effect = send_side_effect
    return E2ETestRunner(source, scheduler, send), send, scheduler_task


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _by_key(results):
    return {result.key: result for result in results}


@pytest.mark.asyncio
async def test_nine_step_health_check_uses_independent_probes_and_production_flow():
    baseline = _post("1a_1", publish_time="2026-08-01 11:00:00")
    candidate = _post("1a_2")
    source = _OfflineSource(
        [baseline],
        [candidate],
        production=[baseline, candidate],
    )
    runner, send, scheduler_task = await _runner(source)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    by_key = _by_key(results)
    assert [result.key for result in results] == list(runner.STEP_ORDER)
    assert len(results) == 9
    assert all(result.status == "pass" for result in results), [
        (result.key, result.status, result.error) for result in results
    ]
    assert by_key["fixture_detail"].facts["fixture_provider"] == "production"
    assert runner._runtime is not None
    assert not os.path.exists(runner._runtime.temporary.name)
    assert len(source.calls) == 6
    assert [call[0] for call in source.calls] == [
        "mobile",
        "tag",
        "tag",
        "post",
        "post",
        "blog",
    ]
    send.assert_awaited_once()
    session_id, sent_post, header, source_types = send.await_args.args
    assert session_id == "qq:real-session"
    assert sent_post.post_id == candidate.post_id
    assert header.startswith("【Lofter E2E 测试】")
    assert source_types == frozenset({"tag"})

    report = format_report(results)
    assert "总体状态：HEALTHY" in report
    assert "[2/9 mobile_direct]" in report
    assert "DWR：已真实验证" in report
    assert "Fixture：provider=production" in report
    assert "真实发送：尝试 1，adapter accepted=是" in report
    assert "清理：tasks=是，db=是，temp-dir=是" in report
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
async def test_mobile_rejection_does_not_block_dwr_or_production():
    baseline = _post("1a_1")
    candidate = _post("1a_2")
    source = _OfflineSource(
        [baseline],
        [candidate],
        production=[baseline, candidate],
        mobile_reason="mobile_incomplete",
    )
    runner, send, scheduler_task = await _runner(source)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    by_key = _by_key(results)
    mobile = by_key["mobile_direct"]
    assert mobile.status == "fail"
    assert mobile.facts == {
        "mobile_eligible": False,
        "mobile_fallback_reason": "mobile_incomplete",
    }
    assert by_key["dwr_direct"].status == "pass"
    assert by_key["production_orchestration"].status == "pass"
    assert by_key["claim_send_ack_seen"].status == "pass"
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_dwr_failure_is_independent_and_payload_free():
    baseline = _post("1a_1")
    candidate = _post("1a_2")
    error = DWRIdentityError("post_id_conflict", "postId", "postUrl")
    source = _OfflineSource(
        [baseline, candidate],
        [],
        production=[baseline, candidate],
        dwr_error=error,
    )
    runner, send, scheduler_task = await _runner(source)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    by_key = _by_key(results)
    assert by_key["mobile_direct"].status == "pass"
    assert by_key["dwr_direct"].status == "fail"
    assert by_key["dwr_direct"].facts == {"dwr_verified": False}
    assert by_key["production_orchestration"].status == "pass"
    assert by_key["fixture_detail"].facts["fixture_provider"] == "production"
    assert by_key["claim_send_ack_seen"].status == "pass"
    send.assert_awaited_once()
    report = format_report(results)
    assert "总体状态：DEGRADED" in report
    assert "post_id_conflict:postId+postUrl" in report
    assert "private-owner" not in report


@pytest.mark.asyncio
async def test_production_failure_uses_mobile_fixture_provider():
    baseline = _post("1a_1")
    candidate = _post("1a_2")
    source = _OfflineSource(
        [baseline, candidate],
        [candidate],
        production_error=SourceTimeoutError(),
    )
    runner, send, scheduler_task = await _runner(source)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    by_key = _by_key(results)
    assert by_key["production_orchestration"].health == "inconclusive"
    assert by_key["fixture_detail"].facts["fixture_provider"] == "mobile"
    assert by_key["claim_send_ack_seen"].status == "pass"
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_fixture_combines_sources_only_when_each_is_insufficient():
    baseline = _post("1a_1")
    candidate = _post("1a_2")
    source = _OfflineSource(
        [baseline],
        [candidate],
        production=[baseline],
    )
    runner, send, scheduler_task = await _runner(source)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    by_key = _by_key(results)
    assert by_key["fixture_detail"].status == "pass"
    assert by_key["fixture_detail"].facts["fixture_provider"] == "combined"
    assert by_key["claim_send_ack_seen"].status == "pass"
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_fixture_conflict_is_contained_and_blocks_dependents_by_root_key():
    first = _post("1a_1", owner="owner-a")
    conflict = _post("1a_1", owner="owner-b")
    source = _OfflineSource([first], [conflict], production=[first])
    runner, send, scheduler_task = await _runner(source)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    by_key = _by_key(results)
    assert by_key["fixture_detail"].status == "fail"
    assert "fixture_bundle" not in runner._artifacts
    assert by_key["blog"].blocked_by == ("fixture_detail",)
    assert by_key["warmup_pending"].blocked_by == ("fixture_detail",)
    assert by_key["claim_send_ack_seen"].blocked_by == ("fixture_detail",)
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_detail_failure_does_not_publish_partial_fixture():
    baseline = _post("1a_1")
    candidate = _post("1a_2")
    source = _OfflineSource(
        [baseline],
        [candidate],
        production=[baseline, candidate],
        detail_errors={candidate.url: SourceSchemaError("post.evidence")},
    )
    runner, send, scheduler_task = await _runner(source)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    by_key = _by_key(results)
    assert by_key["fixture_detail"].status == "fail"
    assert "fixture_bundle" not in runner._artifacts
    assert by_key["blog"].blocked_by == ("fixture_detail",)
    assert by_key["warmup_pending"].blocked_by == ("fixture_detail",)
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_blog_failure_does_not_block_tag_delivery_flow():
    baseline = _post("1a_1")
    candidate = _post("1a_2")
    source = _OfflineSource(
        [baseline],
        [candidate],
        production=[baseline, candidate],
        blog_error=SourceSchemaError("response"),
    )
    runner, send, scheduler_task = await _runner(source)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    by_key = _by_key(results)
    assert by_key["blog"].status == "fail"
    assert by_key["warmup_pending"].status == "pass"
    assert by_key["claim_send_ack_seen"].status == "pass"
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_failure_only_blocks_flow():
    baseline = _post("1a_1")
    candidate = _post("1a_2")
    source = _OfflineSource(
        [baseline],
        [candidate],
        production=[baseline, candidate],
    )
    runner, send, scheduler_task = await _runner(
        source, scheduler_running=False
    )

    results = await runner.run_all("qq:real-session")

    by_key = _by_key(results)
    assert by_key["runtime"].status == "fail"
    assert by_key["mobile_direct"].status == "pass"
    assert by_key["dwr_direct"].status == "pass"
    assert by_key["production_orchestration"].status == "pass"
    assert by_key["fixture_detail"].status == "pass"
    assert by_key["blog"].status == "pass"
    assert by_key["warmup_pending"].blocked_by == ("runtime",)
    assert by_key["claim_send_ack_seen"].blocked_by == ("runtime",)
    send.assert_not_awaited()
    assert scheduler_task.done()


@pytest.mark.asyncio
async def test_insufficient_fixture_is_inconclusive_and_uses_stable_blocker():
    only = _post("1a_1")
    source = _OfflineSource([only], [], production=[only])
    runner, send, scheduler_task = await _runner(source)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    by_key = _by_key(results)
    assert by_key["fixture_detail"].health == "inconclusive"
    assert by_key["blog"].blocked_by == ("fixture_detail",)
    assert by_key["warmup_pending"].blocked_by == ("fixture_detail",)
    assert by_key["claim_send_ack_seen"].blocked_by == ("fixture_detail",)
    send.assert_not_awaited()
    assert "总体状态：INCONCLUSIVE" in format_report(results)


@pytest.mark.asyncio
async def test_pending_assertion_failure_skips_adapter():
    baseline = _post("1a_1")
    candidate = _post("1a_2")
    source = _OfflineSource(
        [baseline], [candidate], production=[baseline, candidate]
    )
    runner, send, scheduler_task = await _runner(source)
    runner._delivery_row = AsyncMock(return_value=None)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    by_key = _by_key(results)
    assert by_key["warmup_pending"].status == "fail"
    assert by_key["warmup_pending"].facts == {"pending_verified": False}
    assert by_key["claim_send_ack_seen"].blocked_by == ("warmup_pending",)
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_rejection_is_degraded_without_seen_ack():
    baseline = _post("1a_1")
    candidate = _post("1a_2")
    source = _OfflineSource(
        [baseline], [candidate], production=[baseline, candidate]
    )
    runner, send, scheduler_task = await _runner(source, send_result=False)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    delivery = _by_key(results)["claim_send_ack_seen"]
    assert delivery.status == "fail"
    assert delivery.health == "degraded"
    assert delivery.facts == {
        "send_attempts": 1,
        "adapter_accepted": False,
    }
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduler_send_timeout_is_inconclusive(monkeypatch):
    async def never_returns(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduler_module, "SEND_TIMEOUT_SECONDS", 0.01)
    baseline = _post("1a_1")
    candidate = _post("1a_2")
    source = _OfflineSource(
        [baseline], [candidate], production=[baseline, candidate]
    )
    runner, send, scheduler_task = await _runner(
        source, send_side_effect=never_returns
    )
    runner.POLL_SECONDS = 0.2

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    delivery = _by_key(results)["claim_send_ack_seen"]
    assert delivery.health == "inconclusive"
    assert delivery.facts == {
        "send_attempts": 1,
        "adapter_accepted": "unknown",
    }
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_attempts_temp_directory_after_db_close_failure():
    baseline = _post("1a_1")
    candidate = _post("1a_2")
    source = _OfflineSource(
        [baseline], [candidate], production=[baseline, candidate]
    )
    runner, _, scheduler_task = await _runner(source)
    create_runtime = runner._create_runtime

    async def create_with_close_failure():
        runtime = await create_runtime()
        close = runtime.db.close

        async def close_then_fail():
            await close()
            raise RuntimeError("private close failure")

        runtime.db.close = close_then_fail
        return runtime

    runner._create_runtime = create_with_close_failure
    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    cleanup = _by_key(results)["cleanup"]
    assert cleanup.status == "fail"
    assert cleanup.facts == {
        "tasks_cancelled": True,
        "db_closed": False,
        "temp_dir_cleaned": True,
        "temp_db_cleaned": False,
    }
    assert runner._runtime is not None
    assert not os.path.exists(runner._runtime.temporary.name)
    assert "private close failure" not in format_report(results)


@pytest.mark.asyncio
async def test_cancellation_still_removes_temporary_database():
    baseline = _post("1a_1")
    candidate = _post("1a_2")
    source = _OfflineSource(
        [baseline], [candidate], production=[baseline, candidate]
    )
    runner, _, scheduler_task = await _runner(source)
    runner._step_02_mobile_direct = AsyncMock(side_effect=asyncio.CancelledError)

    try:
        with pytest.raises(asyncio.CancelledError):
            await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    assert runner._runtime is not None
    assert not os.path.exists(runner._runtime.temporary.name)
    assert runner._cleanup_complete is True


@pytest.mark.parametrize(
    "error, expected",
    [
        (
            DWREvidenceError(
                "content_alias_conflict",
                "dirContent.content",
                "dirContent.text",
            ),
            "DWR 证据冲突（content_alias_conflict:dirContent.content+dirContent.text）",
        ),
        (
            DWRIdentityError(
                "invalid_post_url",
                "permalink",
                value_shape="relative_post_path",
            ),
            "DWR 身份冲突（invalid_post_url:permalink;shape=relative_post_path）",
        ),
    ],
)
def test_report_keeps_typed_dwr_diagnostics_payload_free(error, expected):
    runner = E2ETestRunner(
        _OfflineSource([], []),
        SimpleNamespace(_task=None, _interval=1800),
        AsyncMock(),
    )
    result = runner._fail(
        "dwr_direct",
        "DWR 标签直连",
        1,
        error,
        [],
        facts={"dwr_verified": False},
    )

    report = format_report([result])

    assert expected in report
    for secret in (
        "private-dir-content",
        "private-content",
        "https://private-owner.lofter.com/post/1a_2b?token=private-token",
        "private-owner",
        "private-token",
    ):
        assert secret not in report


def test_report_classifies_challenge_as_inconclusive_without_raw_payload():
    runner = E2ETestRunner(
        _OfflineSource([], []),
        SimpleNamespace(_task=None, _interval=1800),
        AsyncMock(),
    )
    result = runner._fail(
        "dwr_direct",
        "DWR 标签直连",
        1,
        SourceChallengeError(),
        [],
        facts={"dwr_verified": False},
    )

    report = format_report([result])

    assert result.health == "inconclusive"
    assert "总体状态：INCONCLUSIVE" in report
    assert "private response body" not in report
