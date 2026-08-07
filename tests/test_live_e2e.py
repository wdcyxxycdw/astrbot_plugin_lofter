from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from core.content_source import DefaultContentSource
from core.e2e_test import E2ETestRunner, StepResult, format_report
from core.send_result import PushSendResult

RUN_LIVE = os.getenv("LOFTER_RUN_LIVE") == "1"
_ROOT_KEYS = (
    "mobile_direct",
    "dwr_direct",
    "production_orchestration",
)


def _by_key(results):
    return {result.key: result for result in results}


def _require_step(condition: bool, key: str) -> None:
    if not condition:
        pytest.fail(key)


def _terminal_skip_key(
    results: list[StepResult], send_count: int
) -> str | None:
    by_key = _by_key(results)
    for key in _ROOT_KEYS:
        result = by_key[key]
        if result.health == "degraded" or (
            result.status == "fail" and result.health != "inconclusive"
        ):
            pytest.fail(key)

    _require_step(by_key["cleanup"].status == "pass", "cleanup")

    fixture = by_key["fixture_detail"]
    fixture_inconclusive = fixture.health == "inconclusive"
    if fixture.status != "pass" and not fixture_inconclusive:
        pytest.fail("fixture_detail")

    blog = by_key["blog"]
    blog_inconclusive = blog.health == "inconclusive"
    if (blog.status != "pass" or blog.health != "healthy") and not blog_inconclusive:
        pytest.fail("blog")

    if fixture_inconclusive:
        return "fixture_detail"

    _require_step(by_key["warmup_pending"].status == "pass", "warmup_pending")
    claim = by_key["claim_send_ack_seen"]
    _require_step(claim.status == "pass", "claim_send_ack_seen")
    _require_step(send_count == 1, "claim_send_ack_seen")
    _require_step(
        claim.facts.get("delivery_accepted") is True,
        "claim_send_ack_seen",
    )
    _require_step(claim.facts.get("seen_written") is True, "claim_send_ack_seen")

    return "blog" if blog_inconclusive else None


def _results(
    *roots: StepResult,
    fixture: StepResult,
    blog: StepResult | None = None,
    warmup: StepResult | None = None,
    claim: StepResult | None = None,
    cleanup: StepResult | None = None,
) -> list[StepResult]:
    return [
        *roots,
        fixture,
        blog or _root("blog"),
        warmup or _root("warmup_pending"),
        claim
        or _root(
            "claim_send_ack_seen",
            facts={"delivery_accepted": True, "seen_written": True},
        ),
        cleanup or _root("cleanup"),
    ]


def _safe_step_diagnostics(results: list[StepResult]) -> tuple[str, ...]:
    safe_fact_keys = (
        "production_source",
        "production_restarted",
        "production_fallback_reason",
        "production_partial_reason",
        "production_page_count",
        "production_unique_count",
        "production_evidence_count",
    )
    return tuple(
        "step="
        f"{result.key} status={result.status} health={result.health} "
        f"diagnostic={result.error or 'none'}"
        + " ".join(
            f" {key}={result.facts[key]}"
            for key in safe_fact_keys
            if key in result.facts
        )
        for result in results
    )


def _report_contains_sensitive(runner: E2ETestRunner, report: str) -> bool:
    bundle = runner._artifacts.get("fixture_bundle")
    posts = (
        getattr(bundle, "baseline", None),
        getattr(bundle, "candidate", None),
    )
    values: list[str] = []
    for post in posts:
        if post is None:
            continue
        for field in (
            "post_id",
            "url",
            "author",
            "author_username",
            "title",
            "summary",
            "content",
        ):
            value = getattr(post, field, None)
            if isinstance(value, str) and value:
                values.append(value)
        for field in ("images", "tags"):
            value = getattr(post, field, ())
            if isinstance(value, (list, tuple)):
                values.extend(item for item in value if isinstance(item, str) and item)
    return any(value in report for value in values)


def _root(
    key: str,
    *,
    status: str = "pass",
    health: str = "healthy",
    facts: dict[str, str | int | bool] | None = None,
) -> StepResult:
    return StepResult(key, status, key=key, health=health, facts=facts or {})


def test_degraded_root_with_skipped_fixture_fails() -> None:
    results = _results(
        _root("mobile_direct", health="degraded"),
        _root("dwr_direct"),
        _root("production_orchestration"),
        fixture=_root("fixture_detail", status="skip", health="inconclusive"),
    )

    with pytest.raises(pytest.fail.Exception, match="mobile_direct"):
        _terminal_skip_key(results, 1)


def test_degraded_root_with_passing_fixture_fails() -> None:
    results = _results(
        _root("mobile_direct"),
        _root("dwr_direct", health="degraded"),
        _root("production_orchestration"),
        fixture=_root("fixture_detail"),
    )

    with pytest.raises(pytest.fail.Exception, match="dwr_direct"):
        _terminal_skip_key(results, 1)


def test_fixture_inconclusive_requires_cleanup_before_skip() -> None:
    results = _results(
        _root("mobile_direct", status="fail", health="inconclusive"),
        _root("dwr_direct", status="fail", health="inconclusive"),
        _root("production_orchestration", status="fail", health="inconclusive"),
        fixture=_root("fixture_detail", status="skip", health="inconclusive"),
        cleanup=_root("cleanup", status="fail", health="degraded"),
    )

    with pytest.raises(pytest.fail.Exception, match="cleanup"):
        _terminal_skip_key(results, 1)


def test_fixture_inconclusive_skips_after_cleanup() -> None:
    results = _results(
        _root("mobile_direct", status="fail", health="inconclusive"),
        _root("dwr_direct", status="fail", health="inconclusive"),
        _root("production_orchestration", status="fail", health="inconclusive"),
        fixture=_root("fixture_detail", status="skip", health="inconclusive"),
    )

    assert _terminal_skip_key(results, 0) == "fixture_detail"


def test_degraded_blog_with_other_steps_passing_fails() -> None:
    results = _results(
        _root("mobile_direct"),
        _root("dwr_direct"),
        _root("production_orchestration"),
        fixture=_root("fixture_detail"),
        blog=_root("blog", status="fail", health="degraded"),
    )

    with pytest.raises(pytest.fail.Exception, match="blog"):
        _terminal_skip_key(results, 1)


def test_inconclusive_blog_checks_tag_flow_before_skip() -> None:
    results = _results(
        _root("mobile_direct"),
        _root("dwr_direct"),
        _root("production_orchestration"),
        fixture=_root("fixture_detail"),
        blog=_root("blog", status="fail", health="inconclusive"),
        warmup=_root("warmup_pending", status="fail", health="degraded"),
    )

    with pytest.raises(pytest.fail.Exception, match="warmup_pending"):
        _terminal_skip_key(results, 1)


def test_inconclusive_blog_checks_claim_before_skip() -> None:
    results = _results(
        _root("mobile_direct"),
        _root("dwr_direct"),
        _root("production_orchestration"),
        fixture=_root("fixture_detail"),
        blog=_root("blog", status="fail", health="inconclusive"),
        claim=_root("claim_send_ack_seen", status="fail", health="degraded"),
    )

    with pytest.raises(pytest.fail.Exception, match="claim_send_ack_seen"):
        _terminal_skip_key(results, 1)


def test_inconclusive_blog_checks_cleanup_before_skip() -> None:
    results = _results(
        _root("mobile_direct"),
        _root("dwr_direct"),
        _root("production_orchestration"),
        fixture=_root("fixture_detail"),
        blog=_root("blog", status="fail", health="inconclusive"),
        cleanup=_root("cleanup", status="fail", health="degraded"),
    )

    with pytest.raises(pytest.fail.Exception, match="cleanup"):
        _terminal_skip_key(results, 1)


def test_inconclusive_blog_skips_after_healthy_tag_flow() -> None:
    results = _results(
        _root("mobile_direct"),
        _root("dwr_direct"),
        _root("production_orchestration"),
        fixture=_root("fixture_detail"),
        blog=_root("blog", status="fail", health="inconclusive"),
    )

    assert _terminal_skip_key(results, 1) == "blog"


def test_report_sensitive_detector_uses_fixture_artifacts_only() -> None:
    post = SimpleNamespace(
        post_id="synthetic-private-id",
        url="https://synthetic.example/post/private",
        author="synthetic-author",
        author_username="synthetic-user",
        title="synthetic-title",
        summary="synthetic-summary",
        content="synthetic-content",
        images=["https://synthetic.example/private.jpg"],
        tags=["synthetic-tag"],
    )
    runner = SimpleNamespace(
        _artifacts={"fixture_bundle": SimpleNamespace(baseline=post, candidate=post)}
    )
    report = format_report([_root("runtime")])

    assert _report_contains_sensitive(runner, report) is False
    assert _report_contains_sensitive(runner, f"{report}\nsynthetic-title") is True


@pytest.mark.asyncio
@pytest.mark.real
@pytest.mark.skipif(not RUN_LIVE, reason="需要设置 LOFTER_RUN_LIVE=1")
async def test_real_nine_step_health_uses_local_fake_send_only():
    source = DefaultContentSource()
    source.update_cookie(os.getenv("LOFTER_COOKIE", ""))
    await source.initialize()
    scheduler_task = asyncio.create_task(asyncio.Event().wait())
    production_scheduler = SimpleNamespace(
        _task=scheduler_task,
        _interval=1800,
    )
    send_count = 0

    async def fake_send(session_id, post, header, source_types):
        nonlocal send_count
        send_count += 1
        return PushSendResult(
            "accepted",
            "primary_send",
            media_outcome="accepted" if post.images else "not_applicable",
            media_stage="media_send" if post.images else None,
        )

    runner = E2ETestRunner(source, production_scheduler, fake_send)
    try:
        results = await runner.run_all("__lofter_live_fake_send__")
    finally:
        scheduler_task.cancel()
        await asyncio.gather(scheduler_task, return_exceptions=True)
        await source.close()

    _require_step(
        [result.key for result in results] == list(runner.STEP_ORDER),
        "runtime",
    )
    _require_step(len(results) == 9, "runtime")
    report = format_report(results)
    _require_step(
        not _report_contains_sensitive(runner, report),
        "runtime",
    )
    _require_step("mobile_image_aliases" not in report, "runtime")
    _require_step("alias_value_conflict:images" not in report, "runtime")
    for diagnostic in _safe_step_diagnostics(results):
        print(diagnostic)
    skip_key = _terminal_skip_key(results, send_count)
    if skip_key is not None:
        pytest.skip(skip_key)
