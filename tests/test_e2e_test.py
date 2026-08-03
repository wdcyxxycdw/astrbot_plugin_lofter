import asyncio
from unittest.mock import AsyncMock, call

import pytest

from core.e2e_test import E2ETestRunner
from core.parser import Post
from core.source_scan import SourcePage
from core.storage import Subscription
from core.subscription_service import SubscriptionMutationResult


def _post(*, tags_known: bool = True) -> Post:
    fields = {"title", "url", "publish_time"}
    tags = []
    if tags_known:
        fields.add("tags")
        tags = ["摄影"]
    return Post(
        post_id="abc123",
        title="fixture",
        summary="",
        tags=tags,
        url="https://owner.lofter.com/post/abc123",
        publish_time="2026-08-01 12:00:00",
        completeness=frozenset(fields),
    )


def _page(post: Post, source: str = "mobile_tag") -> SourcePage:
    return SourcePage(
        items=[post],
        source=source,
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=1,
        dropped_count=0,
        complete=True,
    )


def _runner(*, send_result: bool = True) -> E2ETestRunner:
    return E2ETestRunner(
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(return_value=send_result),
    )


def test_runner_uses_unique_sessions_and_config_keys():
    first = _runner()
    second = _runner()

    assert first.TEST_SESSION != second.TEST_SESSION
    assert first.PREVIEW_SESSION != second.PREVIEW_SESSION
    assert first.TEST_CONFIG_KEY != second.TEST_CONFIG_KEY
    assert first.TEST_SESSION != first.PREVIEW_SESSION


@pytest.mark.asyncio
async def test_dynamic_fixture_is_reused_across_network_steps():
    runner = _runner()
    fixture = _post()
    runner._source.list_tag.return_value = _page(fixture)
    runner._source.get_post.return_value = fixture
    runner._source.list_blog.return_value = _page(fixture, "mobile_blog")

    results = [
        await runner._step_03_http_get(),
        await runner._step_04_dwr_search(),
        await runner._step_06_blog_fetch(),
    ]

    assert [result.status for result in results] == ["pass", "pass", "pass"]
    runner._source.list_tag.assert_awaited_once_with("摄影", None, 20, "new")
    runner._source.get_post.assert_awaited_once_with(fixture.url)
    runner._source.list_blog.assert_awaited_once_with("owner", None, 20)


@pytest.mark.asyncio
async def test_filter_step_enriches_unknown_tags():
    runner = _runner()
    sparse = _post(tags_known=False)
    runner._artifacts["tag_posts"] = [sparse]
    runner._source.get_post.return_value = _post()

    result = await runner._step_10_filter()

    assert result.status == "pass"
    runner._source.get_post.assert_awaited_once_with(sparse.url)


@pytest.mark.asyncio
async def test_subscription_steps_use_service_and_preview_session():
    runner = _runner()
    fixture = _post()
    exclude = "摄影_unlikely_excl"
    runner._source.list_tag.return_value = _page(fixture)
    runner._db.seen_count.return_value = 1
    runner._storage.list_by_session.return_value = [
        Subscription(1, runner.TEST_SESSION, "tag", "subscribe", "摄影"),
        Subscription(2, runner.TEST_SESSION, "tag", "exclude", exclude),
    ]
    runner._subscriptions.subscribe_tags.side_effect = [
        SubscriptionMutationResult(("摄影",), (exclude,)),
        SubscriptionMutationResult(("摄影",), (exclude,), (fixture,)),
    ]
    runner._subscriptions.subscribe_blog.return_value = SubscriptionMutationResult(
        ("owner",), ()
    )

    results = [
        await runner._step_14_subtag_full(),
        await runner._step_15_subblog_full(),
        await runner._step_16_subtagpreview("real-session"),
    ]

    assert [result.status for result in results] == ["pass", "pass", "pass"]
    assert runner._subscriptions.subscribe_tags.await_args_list == [
        call(runner.TEST_SESSION, ["摄影"], [exclude]),
        call(runner.PREVIEW_SESSION, ["摄影"], [exclude], preview=True),
    ]
    runner._subscriptions.subscribe_blog.assert_awaited_once_with(
        runner.TEST_SESSION, "owner"
    )
    runner._send_push.assert_awaited_once_with(
        "real-session",
        fixture,
        "【标签「摄影」有新内容】",
        frozenset({"tag"}),
    )


@pytest.mark.asyncio
async def test_preview_send_rejection_fails_step():
    runner = _runner(send_result=False)
    runner._subscriptions.subscribe_tags.return_value = SubscriptionMutationResult(
        ("摄影",), (), (_post(),)
    )

    result = await runner._step_16_subtagpreview("real-session")

    assert result.status == "fail"
    assert "adapter 未接受" in (result.error or "")


@pytest.mark.asyncio
async def test_blog_send_rejection_fails_step():
    runner = _runner(send_result=False)
    fixture = _post()
    runner._source.list_tag.return_value = _page(fixture)
    runner._artifacts["blog_posts"] = [fixture]

    result = await runner._step_20_push_blog("real-session")

    assert result.status == "fail"
    assert "adapter 未接受" in (result.error or "")


@pytest.mark.asyncio
async def test_manual_poll_only_targets_test_session():
    runner = _runner()
    runner._db.seen_count.side_effect = [1, 1]

    result = await runner._step_19_manual_poll()

    assert result.status == "pass"
    runner._scheduler._poll_single_session.assert_awaited_once_with(
        runner.TEST_SESSION
    )


@pytest.mark.asyncio
async def test_run_all_cancellation_still_cleans_both_sessions():
    runner = _runner()
    runner._step_01_config_rw = AsyncMock(side_effect=asyncio.CancelledError)
    runner._storage.list_by_session.side_effect = [[], []]

    with pytest.raises(asyncio.CancelledError):
        await runner.run_all("real-session")

    assert runner._storage.list_by_session.await_args_list == [
        call(runner.TEST_SESSION),
        call(runner.PREVIEW_SESSION),
    ]
    assert runner._db.clear_session.await_args_list == [
        call(runner.TEST_SESSION),
        call(runner.PREVIEW_SESSION),
    ]
    runner._db.delete_config.assert_awaited_once_with(runner.TEST_CONFIG_KEY)
