import asyncio
import os
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.scheduler as scheduler_module
from core.content_source import MobileTagDiagnostic
from core.e2e_test import E2ETestRunner, format_report
from core.errors import (
    DWREvidenceError,
    DWRIdentityError,
    PostEvidenceError,
    SourceChallengeError,
    SourcePartialError,
    SourceSchemaError,
    SourceTimeoutError,
    attach_source_evidence,
)
from core.parser import Post
from core.send_result import PushSendResult
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
        detail_posts: dict[str, Post] | None = None,
        blog_error: BaseException | None = None,
        production_diagnostics: tuple[str, ...] = (),
        mobile_counts: tuple[int, int, int, int, int] = (0, 0, 0, 0, 0),
    ) -> None:
        self.mobile = mobile
        self.dwr = dwr
        self.production = production if production is not None else mobile
        self.mobile_reason = mobile_reason
        self.mobile_error = mobile_error
        self.dwr_error = dwr_error
        self.production_error = production_error
        self.detail_errors = detail_errors or {}
        self.detail_posts = detail_posts or {}
        self.blog_error = blog_error
        self.production_diagnostics = production_diagnostics
        self.mobile_counts = mobile_counts
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
            *self.mobile_counts,
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
        post = self.detail_posts.get(url, self.posts.get(url))
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
    send_result: PushSendResult | None = None,
    send_side_effect=None,
    scheduler_running: bool = True,
):
    if scheduler_running:
        scheduler_task = asyncio.create_task(asyncio.Event().wait())
    else:
        scheduler_task = asyncio.create_task(asyncio.sleep(0))
        await scheduler_task
    scheduler = SimpleNamespace(_task=scheduler_task, _interval=1800)
    if send_result is None:
        send_result = PushSendResult("accepted", "primary_send")
    send = AsyncMock(return_value=send_result)
    if send_side_effect is not None:
        send.side_effect = send_side_effect
    return E2ETestRunner(source, scheduler, send), send, scheduler_task


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _by_key(results):
    return {result.key: result for result in results}


def _expected_send_facts(
    *,
    send_attempts: int = 1,
    primary_outcome: str = "accepted",
    primary_stage: str = "primary_send",
    primary_error_type: str = "none",
    primary_error_retcode: int | str = "none",
    media_outcome: str = "not_applicable",
    media_stage: str = "none",
    media_error_type: str = "none",
    media_error_retcode: int | str = "none",
    delivery_accepted: bool | str = True,
    seen_written: bool | str = True,
):
    return {
        "send_attempts": send_attempts,
        "primary_outcome": primary_outcome,
        "primary_stage": primary_stage,
        "primary_error_type": primary_error_type,
        "primary_error_retcode": primary_error_retcode,
        "media_outcome": media_outcome,
        "media_stage": media_stage,
        "media_error_type": media_error_type,
        "media_error_retcode": media_error_retcode,
        "delivery_accepted": delivery_accepted,
        "seen_written": seen_written,
    }


@pytest.mark.asyncio
async def test_nine_step_health_check_uses_independent_probes_and_production_flow():
    baseline = _post("1a_1", publish_time="2026-08-01 11:00:00")
    candidate = _post("1a_2")
    source = _OfflineSource(
        [baseline],
        [candidate],
        production=[baseline, candidate],
    )
    runner, send, scheduler_task = await _runner(
        source,
        send_result=PushSendResult(
            "accepted",
            "primary_send",
            media_outcome="accepted",
            media_stage="media_send",
        ),
    )

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
    assert (
        "真实发送：尝试 1，primary=accepted/primary_send，"
        "media=accepted/media_send，delivery accepted=是，seen=是"
        in report
    )
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
async def test_zero_photo_list_evidence_survives_unknown_detail_in_full_flow():
    baseline = _post("1a_1", publish_time="2026-08-01 11:00:00")
    candidate = replace(
        _post("1a_2"),
        images=[],
        source="mobile_tag",
        provenance={
            **_post("1a_2").provenance,
            "images": "mobile_tag",
        },
    )
    detail = replace(
        candidate,
        source="mobile_detail",
        completeness=candidate.completeness - {"images"},
        provenance={
            name: value
            for name, value in candidate.provenance.items()
            if name != "images"
        },
    )
    source = _OfflineSource(
        [baseline],
        [candidate],
        production=[baseline, candidate],
        detail_posts={candidate.url: detail},
    )
    runner, send, scheduler_task = await _runner(source)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    by_key = _by_key(results)
    assert by_key["fixture_detail"].status == "pass"
    assert by_key["fixture_detail"].facts["fixture_provider"] == "production"
    assert by_key["blog"].status == "pass"
    assert by_key["warmup_pending"].status == "pass"
    assert by_key["claim_send_ack_seen"].status == "pass"
    send.assert_awaited_once()
    assert send.await_args.args[1].images == []
    assert "images" in send.await_args.args[1].completeness


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
        "mobile_item_count": 0,
        "mobile_time_count": 0,
        "mobile_regression_count": 0,
        "mobile_equal_count": 0,
        "mobile_first_regression_pair_ordinal": 0,
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
async def test_dwr_fixture_accepts_distinct_detail_summaries():
    first = replace(
        _post("1a_1", publish_time="2026-08-01 11:00:00"),
        summary="private-list-summary-a",
        source="dwr",
        provenance={**_post("1a_1").provenance, "summary": "dwr"},
    )
    second = replace(
        _post("1a_2"),
        summary="private-list-summary-b",
        source="dwr",
        provenance={**_post("1a_2").provenance, "summary": "dwr"},
    )
    details = {
        first.url: replace(
            first,
            summary="private-detail-summary-a",
            source="mobile_detail",
            provenance={**first.provenance, "summary": "mobile_detail"},
        ),
        second.url: replace(
            second,
            summary="private-detail-summary-b",
            source="mobile_detail",
            provenance={**second.provenance, "summary": "mobile_detail"},
        ),
    }
    source = _OfflineSource([], [first, second], production=[])
    source.posts = details
    runner, send, scheduler_task = await _runner(source)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    by_key = _by_key(results)
    assert by_key["fixture_detail"].status == "pass"
    assert by_key["fixture_detail"].facts["fixture_provider"] == "dwr"
    assert by_key["claim_send_ack_seen"].status == "pass"
    send.assert_awaited_once()
    report = format_report(results)
    for secret in (
        "private-list-summary-a",
        "private-list-summary-b",
        "private-detail-summary-a",
        "private-detail-summary-b",
    ):
        assert secret not in report


@pytest.mark.asyncio
async def test_dwr_fixture_preserves_list_summary_when_detail_summary_is_empty():
    first = replace(
        _post("1a_1", publish_time="2026-08-01 11:00:00"),
        summary="private-list-summary-a",
        source="dwr",
        provenance={**_post("1a_1").provenance, "summary": "dwr"},
    )
    second = replace(
        _post("1a_2"),
        summary="private-list-summary-b",
        source="dwr",
        provenance={**_post("1a_2").provenance, "summary": "dwr"},
    )
    details = {
        post.url: replace(
            post,
            summary="",
            source="mobile_detail",
            provenance={**post.provenance, "summary": "mobile_detail"},
        )
        for post in (first, second)
    }
    source = _OfflineSource(
        [],
        [first, second],
        production=[],
        detail_posts=details,
    )
    runner, send, scheduler_task = await _runner(source)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    by_key = _by_key(results)
    assert by_key["fixture_detail"].status == "pass"
    assert by_key["fixture_detail"].facts["fixture_provider"] == "dwr"
    assert by_key["claim_send_ack_seen"].status == "pass"
    send.assert_awaited_once()
    assert send.await_args.args[1].summary == "private-list-summary-b"
    report = format_report(results)
    assert "private-list-summary-a" not in report
    assert "private-list-summary-b" not in report


@pytest.mark.asyncio
async def test_dwr_fixture_still_rejects_detail_title_conflict():
    first = replace(
        _post("1a_1", publish_time="2026-08-01 11:00:00"),
        summary="private-list-summary-a",
        source="dwr",
        provenance={**_post("1a_1").provenance, "summary": "dwr"},
    )
    second = replace(
        _post("1a_2"),
        summary="private-list-summary-b",
        source="dwr",
        provenance={**_post("1a_2").provenance, "summary": "dwr"},
    )
    details = {
        first.url: replace(
            first,
            title="private-conflicting-title",
            summary="private-detail-summary-a",
            source="mobile_detail",
            provenance={
                **first.provenance,
                "title": "mobile_detail",
                "summary": "mobile_detail",
            },
        ),
        second.url: replace(
            second,
            summary="private-detail-summary-b",
            source="mobile_detail",
            provenance={**second.provenance, "summary": "mobile_detail"},
        ),
    }
    source = _OfflineSource([], [first, second], production=[])
    source.posts = details
    runner, send, scheduler_task = await _runner(source)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    fixture = _by_key(results)["fixture_detail"]
    assert fixture.status == "fail"
    assert fixture.facts["fixture_provider"] == "dwr"
    assert fixture.facts["fixture_phase"] == "list_detail_merge"
    assert fixture.facts["fixture_candidate_ordinal"] == 1
    assert "帖子证据冲突（field_conflict:title:post_ledger）" in format_report(results)
    send.assert_not_awaited()


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
async def test_primary_rejection_is_degraded_without_seen_ack():
    baseline = _post("1a_1")
    candidate = _post("1a_2")
    source = _OfflineSource(
        [baseline], [candidate], production=[baseline, candidate]
    )
    runner, send, scheduler_task = await _runner(
        source,
        send_result=PushSendResult("rejected", "primary_send"),
    )

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    delivery = _by_key(results)["claim_send_ack_seen"]
    assert delivery.status == "fail"
    assert delivery.health == "degraded"
    assert delivery.error == "推送阶段失败（primary_send:rejected）"
    assert delivery.facts == _expected_send_facts(
        primary_outcome="rejected",
        delivery_accepted=False,
        seen_written=False,
    )
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_primary_error_reports_safe_type_without_seen_ack():
    baseline = _post("1a_1")
    candidate = _post("1a_2")
    source = _OfflineSource(
        [baseline], [candidate], production=[baseline, candidate]
    )
    runner, send, scheduler_task = await _runner(
        source,
        send_result=PushSendResult(
            "error",
            "primary_send",
            primary_error_type="RuntimeError",
        ),
    )

    try:
        results = await runner.run_all("qq:private-session-canary")
    finally:
        await _stop(scheduler_task)

    delivery = _by_key(results)["claim_send_ack_seen"]
    assert delivery.status == "fail"
    assert delivery.health == "degraded"
    assert delivery.error == "推送阶段失败（primary_send:error:RuntimeError）"
    assert delivery.facts == _expected_send_facts(
        primary_outcome="error",
        primary_error_type="RuntimeError",
        delivery_accepted=False,
        seen_written=False,
    )
    report = format_report(results)
    for secret in (
        "private-session-canary",
        candidate.post_id,
        candidate.url,
        candidate.author_username,
        candidate.title,
        candidate.summary,
        candidate.images[0],
    ):
        assert secret not in report
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_primary_action_failed_reports_retcode_without_payload():
    baseline = _post("1a_1")
    candidate = _post("1a_2")
    source = _OfflineSource(
        [baseline], [candidate], production=[baseline, candidate]
    )
    runner, send, scheduler_task = await _runner(
        source,
        send_result=PushSendResult(
            "error",
            "primary_send",
            primary_error_type="ActionFailed",
            primary_error_retcode=1404,
        ),
    )

    try:
        results = await runner.run_all("qq:private-action-session-canary")
    finally:
        await _stop(scheduler_task)

    delivery = _by_key(results)["claim_send_ack_seen"]
    assert delivery.status == "fail"
    assert delivery.health == "degraded"
    assert delivery.error == (
        "推送阶段失败（primary_send:error:ActionFailed:retcode=1404）"
    )
    assert delivery.facts == _expected_send_facts(
        primary_outcome="error",
        primary_error_type="ActionFailed",
        primary_error_retcode=1404,
        delivery_accepted=False,
        seen_written=False,
    )
    report = format_report(results)
    assert "primary_send:error:ActionFailed:retcode=1404" in report
    for secret in (
        "private-action-session-canary",
        "private-action-msg-canary",
        "private-action-wording-canary",
        "private-action-payload-canary",
        candidate.post_id,
        candidate.url,
        candidate.author_username,
        candidate.title,
        candidate.summary,
        candidate.images[0],
    ):
        assert secret not in report
    send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("send_result", "expected_error", "expected_facts"),
    [
        (
            PushSendResult(
                "accepted",
                "primary_send",
                media_outcome="rejected",
                media_stage="media_send",
            ),
            "推送阶段失败（media_send:rejected）",
            _expected_send_facts(
                media_outcome="rejected",
                media_stage="media_send",
            ),
        ),
        (
            PushSendResult(
                "accepted",
                "primary_send",
                media_outcome="error",
                media_stage="media_send",
                media_error_type="RuntimeError",
            ),
            "推送阶段失败（media_send:error:RuntimeError）",
            _expected_send_facts(
                media_outcome="error",
                media_stage="media_send",
                media_error_type="RuntimeError",
            ),
        ),
        (
            PushSendResult(
                "accepted",
                "primary_send",
                media_outcome="error",
                media_stage="media_send",
                media_error_type="ActionFailed",
                media_error_retcode=1405,
            ),
            "推送阶段失败（media_send:error:ActionFailed:retcode=1405）",
            _expected_send_facts(
                media_outcome="error",
                media_stage="media_send",
                media_error_type="ActionFailed",
                media_error_retcode=1405,
            ),
        ),
    ],
)
async def test_media_failure_is_degraded_after_delivery_ack(
    send_result, expected_error, expected_facts
):
    baseline = _post("1a_1")
    candidate = _post("1a_2")
    source = _OfflineSource(
        [baseline], [candidate], production=[baseline, candidate]
    )
    runner, send, scheduler_task = await _runner(
        source, send_result=send_result
    )

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    delivery = _by_key(results)["claim_send_ack_seen"]
    assert delivery.status == "fail"
    assert delivery.health == "degraded"
    assert delivery.error == expected_error
    assert delivery.facts == expected_facts
    assert "图片转发失败，主要消息不会重试" in delivery.details
    assert "production ack_success 已写入 accepted" in delivery.details
    assert "candidate 已写入 subscription-level seen" in delivery.details
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
    assert delivery.facts == _expected_send_facts(
        primary_outcome="unknown",
        primary_stage="unknown",
        primary_error_type="unknown",
        primary_error_retcode="unknown",
        media_outcome="unknown",
        media_stage="unknown",
        media_error_type="unknown",
        media_error_retcode="unknown",
        delivery_accepted="unknown",
        seen_written="unknown",
    )
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


@pytest.mark.asyncio
async def test_mobile_order_failure_reports_only_safe_scalars():
    secret_time = "2099-12-31 23:59:59"
    baseline = _post("1a_1", publish_time=secret_time)
    candidate = _post("1a_2")
    source = _OfflineSource(
        [baseline, candidate],
        [candidate],
        production=[baseline, candidate],
        mobile_reason="mobile_order_regressed",
        mobile_counts=(20, 20, 2, 1, 7),
    )
    runner, _, scheduler_task = await _runner(source)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    mobile = _by_key(results)["mobile_direct"]
    assert mobile.status == "fail"
    assert mobile.facts["mobile_regression_count"] == 2
    assert mobile.details == [
        "items=20",
        "times=20",
        "regressions=2",
        "equals=1",
        "first-regression-pair=7",
        "fallback=mobile_order_regressed",
    ]
    report = format_report(results)
    assert "regressions=2" in report
    assert "first-regression-pair=7" in report
    assert secret_time not in report
    assert baseline.post_id not in report
    assert baseline.url not in report


@pytest.mark.asyncio
async def test_production_partial_reports_typed_safe_context():
    baseline = _post("1a_1")
    candidate = _post("1a_2")
    error = SourcePartialError(
        20,
        0,
        reason="evidence_shortfall",
        source="dwr",
        restarted=True,
        page_count=2,
        unique_count=20,
    )
    attach_source_evidence(error, (baseline, candidate))
    source = _OfflineSource(
        [baseline, candidate],
        [candidate],
        production_error=error,
    )
    runner, _, scheduler_task = await _runner(source)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    production = _by_key(results)["production_orchestration"]
    assert production.status == "fail"
    assert production.health == "degraded"
    assert production.facts == {
        "production_source": "dwr",
        "production_restarted": True,
        "production_fallback_reason": "无",
        "production_partial_reason": "evidence_shortfall",
        "production_page_count": 2,
        "production_unique_count": 20,
        "production_evidence_count": 2,
    }
    assert production.details == [
        "partial=evidence_shortfall",
        "source=dwr",
        "restarted=yes",
        "pages=2",
        "unique=20",
        "evidence=2",
    ]
    report = format_report(results)
    assert (
        "生产标签编排：source=dwr，restarted=是，fallback=无，"
        "partial=evidence_shortfall，pages=2，unique=20，evidence=2"
        in report
    )
    assert baseline.post_id not in report
    assert baseline.url not in report
    assert baseline.title not in report


@pytest.mark.asyncio
async def test_production_non_partial_error_uses_unknown_context():
    baseline = _post("1a_1")
    candidate = _post("1a_2")
    source = _OfflineSource(
        [baseline, candidate],
        [candidate],
        production_error=SourceSchemaError("response"),
    )
    runner, _, scheduler_task = await _runner(source)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    production = _by_key(results)["production_orchestration"]
    assert production.facts["production_source"] == "unknown"
    assert production.facts["production_restarted"] == "unknown"
    assert production.facts["production_partial_reason"] == "unknown"
    assert production.details == []


@pytest.mark.asyncio
async def test_fixture_failure_reports_phase_candidate_and_typed_evidence():
    baseline = _post("1a_1")
    candidate = _post("1a_2")
    error = PostEvidenceError("field_conflict", "summary", "post_ledger")
    source = _OfflineSource(
        [baseline],
        [candidate],
        production=[baseline, candidate],
        detail_errors={candidate.url: error},
    )
    runner, send, scheduler_task = await _runner(source)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    fixture = _by_key(results)["fixture_detail"]
    assert fixture.status == "fail"
    assert fixture.facts == {
        "fixture_ready": False,
        "fixture_provider": "production",
        "fixture_phase": "detail_fetch",
        "fixture_candidate_ordinal": 2,
        "fixture_candidate_total": 2,
    }
    assert fixture.details == ["phase=detail_fetch", "candidate=2/2"]
    report = format_report(results)
    assert "帖子证据冲突（field_conflict:summary:post_ledger）" in report
    assert candidate.post_id not in report
    assert candidate.url not in report
    assert candidate.summary not in report
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_fixture_selection_failure_clears_candidate_ordinal():
    first = _post("1a_1", owner="owner-a")
    conflict = _post("1a_1", owner="owner-b")
    source = _OfflineSource([first], [conflict], production=[first])
    runner, _, scheduler_task = await _runner(source)

    try:
        results = await runner.run_all("qq:real-session")
    finally:
        await _stop(scheduler_task)

    fixture = _by_key(results)["fixture_detail"]
    assert fixture.facts["fixture_phase"] == "candidate_selection"
    assert fixture.facts["fixture_candidate_ordinal"] == 0
    assert fixture.details == ["phase=candidate_selection"]


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
