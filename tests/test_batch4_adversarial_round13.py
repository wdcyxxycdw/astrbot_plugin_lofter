from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.errors import SourceSchemaError
from core.parser import POST_FIELDS, Post
from core.scheduler import _poll_session, _preflight_session, fetch_blog_posts
from core.source_scan import SourcePage
from core.storage import Subscription

POST_ID = "1a_2b"
TIME = "2026-01-01 00:00:00"


def _post(
    *, owner: str = "demo", publish_time: str | None = TIME
) -> Post:
    known = set(POST_FIELDS)
    if publish_time is None:
        known.remove("publish_time")
    return Post(
        post_id=POST_ID,
        title="Demo",
        summary="Summary",
        content="Content",
        images=[],
        author="Demo",
        author_username=owner,
        url=f"https://{owner}.lofter.com/post/{POST_ID}",
        tags=["A"],
        publish_time=publish_time or "",
        source="test",
        completeness=frozenset(known),
    )


def _page(
    items: list[Post], *, cursor: str | None = None,
    exhausted: bool = True, evidence: tuple[Post, ...] = (),
) -> SourcePage:
    return SourcePage(
        items=items,
        source="mobile_blog",
        next_cursor=cursor,
        exhausted=exhausted,
        sort="new",
        mapped_count=len(items),
        dropped_count=0,
        complete=True,
        evidence_items=evidence,
    )


def _sub(target: str, sub_type: str, sub_id: int) -> Subscription:
    return Subscription(
        id=sub_id,
        session_id="session",
        type=sub_type,
        role="subscribe",
        target=target,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_step", ["_prepare_tag_posts", "_filter_tag_unsent_visible"]
)
async def test_tag_processing_failure_blocks_preflighted_same_session_blog(
    failed_step,
):
    tag_post = _post()
    blog_post = _post(owner="blogger")
    blog_sub = _sub("blogger", "blog", 2)
    blocks = AsyncMock()
    blocks.list_by_session.return_value = []
    blog_check = AsyncMock(return_value=True)
    db = AsyncMock()
    send = AsyncMock(return_value=True)
    prepare_result = ([tag_post], {POST_ID: {"A"}})

    with (
        patch(
            "core.scheduler._preflight_session",
            return_value=({"A": [tag_post]}, [(blog_sub, [blog_post])], []),
        ),
        patch(
            "core.scheduler._prepare_tag_posts", return_value=prepare_result
        ) as prepare,
        patch(
            "core.scheduler._filter_tag_unsent_visible",
            return_value=[tag_post],
        ) as filter_visible,
        patch("core.scheduler._check_blog_sub", blog_check),
    ):
        (prepare if failed_step == "_prepare_tag_posts" else filter_visible).side_effect = (
            RuntimeError("ordinary tag failure")
        )
        await _poll_session(
            "session",
            {
                "tag": [_sub("A", "tag", 1)],
                "blog": [blog_sub],
            },
            AsyncMock(),
            db,
            send,
            blocks,
        )

    blog_check.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_blog_detail_failure_carries_all_raw_page_evidence():
    witness = _post(owner="alice")
    raw = _post(owner="alice", publish_time=None)
    source = AsyncMock()
    source.list_blog.return_value = _page([raw], evidence=(witness,))
    source.get_post.side_effect = RuntimeError("ordinary detail failure")

    with pytest.raises(RuntimeError) as exc_info:
        await fetch_blog_posts(_sub("alice", "blog", 1), source)

    assert getattr(exc_info.value, "evidence_items", ()) == (witness, raw)


@pytest.mark.asyncio
async def test_failed_blog_detail_retains_cross_type_identity_evidence():
    tag_post = _post(owner="bob")
    raw_blog = _post(owner="alice", publish_time=None)
    source = AsyncMock()
    source.list_blog.return_value = _page([raw_blog])
    source.get_post.side_effect = RuntimeError("ordinary detail failure")
    blocks = AsyncMock()
    blocks.list_by_session.return_value = []

    with (
        patch("core.scheduler.fetch_tag_posts", return_value=[tag_post]),
        pytest.raises(SourceSchemaError) as exc_info,
    ):
        await _preflight_session(
            "session",
            {
                "tag": [_sub("A", "tag", 1)],
                "blog": [_sub("alice", "blog", 2)],
            },
            source,
            blocks,
        )

    assert exc_info.value.location == "post.owner"


@pytest.mark.asyncio
async def test_failed_blog_later_page_retains_cross_type_identity_evidence():
    tag_post = _post(owner="bob")
    raw_blog = _post(owner="alice")
    source = AsyncMock()
    source.list_blog.side_effect = [
        _page([raw_blog], cursor="next", exhausted=False),
        RuntimeError("ordinary page failure"),
    ]
    blocks = AsyncMock()
    blocks.list_by_session.return_value = []

    with (
        patch("core.scheduler.fetch_tag_posts", return_value=[tag_post]),
        pytest.raises(SourceSchemaError) as exc_info,
    ):
        await _preflight_session(
            "session",
            {
                "tag": [_sub("A", "tag", 1)],
                "blog": [_sub("alice", "blog", 2)],
            },
            source,
            blocks,
        )

    assert exc_info.value.location == "post.owner"
