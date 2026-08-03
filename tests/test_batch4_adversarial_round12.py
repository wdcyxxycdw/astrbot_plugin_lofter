from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core.author_block import AuthorBlock
from core.errors import SourceError, SourceSchemaError
from core.mobile_parser import parse_mobile_post_detail
from core.parser import POST_FIELDS, Post
from core.scheduler import (
    _merge_eligible_tag_posts,
    _preflight_session,
)
from core.source_scan import SourcePage, collect_pages
from core.storage import Subscription
from core.tag_count import count_posts

FIXTURES = Path(__file__).parent / "fixtures" / "lofter"
TIME = "2026-01-03 00:00:00"


def _post(
    post_id: str = "1a_2b",
    *,
    owner: str = "demo",
    title: str = "Demo",
    tags: list[str] | None = None,
    publish_time: str = TIME,
    fields: set[str] | None = None,
) -> Post:
    known = set(POST_FIELDS) if fields is None else set(fields)
    host = f"{owner}.lofter.com" if owner else "lofter.com"
    return Post(
        post_id=post_id,
        title=title if "title" in known else "",
        summary="Summary" if "summary" in known else "",
        content="Content" if "content" in known else "",
        images=["https://example.invalid/a.jpg"] if "images" in known else [],
        author=owner.title() if "author" in known and owner else "",
        author_username=owner if "author_username" in known else "",
        url=f"https://{host}/post/{post_id}",
        tags=(tags if tags is not None else ["A"]) if "tags" in known else [],
        publish_time=publish_time if "publish_time" in known else "",
        source="test",
        completeness=frozenset(known),
    )


def _page(
    items: list[Post],
    *,
    source: str = "mobile_tag",
    cursor: str | None = None,
    exhausted: bool = True,
    restarted: bool = False,
) -> SourcePage:
    return SourcePage(
        items=items,
        source=source,
        next_cursor=cursor,
        exhausted=exhausted,
        sort="new",
        mapped_count=len(items),
        dropped_count=0,
        complete=True,
        restarted=restarted,
    )


def _sub(
    target: str, sub_type: str, role: str = "subscribe", sub_id: int = 1
) -> Subscription:
    return Subscription(
        id=sub_id,
        session_id="session",
        type=sub_type,
        role=role,
        target=target,
    )


def test_mobile_rejects_empty_url_alias_at_identity_boundary():
    payload = json.loads(
        (FIXTURES / "post_detail.json").read_text(encoding="utf-8")
    )["envelope"]
    payload = json.loads(json.dumps(payload))
    item = payload["response"]["posts"][0]
    item["blogPageUrl"] = "https://demo.lofter.com/post/1a_2b"
    item["permalink"] = ""

    with pytest.raises(SourceSchemaError) as exc_info:
        parse_mobile_post_detail(payload)

    assert exc_info.value.location == "post.url"


@pytest.mark.asyncio
async def test_invalid_restart_cannot_hide_page_owner_conflict():
    pages = iter([
        _page([_post(owner="alice")], cursor="next", exhausted=False),
        _page([_post(owner="bob")], restarted=True),
    ])

    with pytest.raises(SourceSchemaError) as exc_info:
        await collect_pages(AsyncMock(side_effect=lambda cursor: next(pages)))

    assert exc_info.value.location == "post.owner"


@pytest.mark.asyncio
async def test_deadline_cannot_hide_returned_page_owner_conflict():
    pages = iter([
        _page([_post(owner="alice")], cursor="next", exhausted=False),
        _page([_post(owner="bob")]),
    ])
    values = iter([0.0, 0.0, 0.0, 0.0, 10.0])

    with pytest.raises(SourceSchemaError) as exc_info:
        await collect_pages(
            AsyncMock(side_effect=lambda cursor: next(pages)),
            deadline=10.0,
            monotonic=lambda: next(values),
        )

    assert exc_info.value.location == "post.owner"


@pytest.mark.asyncio
async def test_later_complete_duplicate_restores_count_lower_bound():
    unknown = _post(fields=set(POST_FIELDS) - {"tags"})
    duplicate = _post(tags=["A"])
    other = _post(
        "1a_2c", tags=["B"], publish_time="2026-01-02 00:00:00"
    )
    source = AsyncMock()
    source.list_tag.side_effect = [
        _page([unknown], cursor="next", exhausted=False),
        _page([duplicate, other]),
    ]
    source.get_post.side_effect = SourceError()

    result = await count_posts("A", source)

    assert result.status == "partial"
    assert result.count == 1
    assert result.candidates == 2


@pytest.mark.asyncio
async def test_excluded_tag_occurrence_remains_cross_type_evidence():
    tag_post = _post(owner="alice", tags=["A", "X"])
    blog_post = _post(owner="bob")
    blocks = AsyncMock()
    blocks.list_by_session.return_value = []

    with (
        patch("core.scheduler.fetch_tag_posts", return_value=[tag_post]),
        patch("core.scheduler.fetch_blog_posts", return_value=[blog_post]),
        pytest.raises(SourceSchemaError) as exc_info,
    ):
        await _preflight_session(
            "session",
            {
                "tag": [
                    _sub("A", "tag", sub_id=1),
                    _sub("X", "tag", role="exclude", sub_id=2),
                ],
                "blog": [_sub("bob", "blog", sub_id=3)],
            },
            AsyncMock(),
            blocks,
        )

    assert exc_info.value.location == "post.owner"


@pytest.mark.asyncio
async def test_failed_blog_enrichment_retains_raw_cross_type_evidence():
    tag_post = _post(owner="bob")
    raw_blog = _post(
        owner="alice", fields=set(POST_FIELDS) - {"author"}
    )
    source = AsyncMock()
    source.get_post.side_effect = RuntimeError("ordinary detail failure")
    blocks = AsyncMock()
    blocks.list_by_session.return_value = [
        AuthorBlock("session", "name", "blocked", "Blocked")
    ]

    with (
        patch("core.scheduler.fetch_tag_posts", return_value=[tag_post]),
        patch("core.scheduler.fetch_blog_posts", return_value=[raw_blog]),
        pytest.raises(SourceSchemaError) as exc_info,
    ):
        await _preflight_session(
            "session",
            {
                "tag": [_sub("A", "tag", sub_id=1)],
                "blog": [_sub("alice", "blog", sub_id=2)],
            },
            source,
            blocks,
        )

    assert exc_info.value.location == "post.owner"


def test_ineligible_duplicate_can_strengthen_eligible_post_fields():
    partial = _post(
        title="", fields=set(POST_FIELDS) - {"title"}
    )
    complete = _post(title="Known from B")

    posts, sources, _ = _merge_eligible_tag_posts(
        {"A": [partial], "B": [complete]},
        {"A": ["1a_2b"], "B": []},
        True,
    )

    assert len(posts) == 1
    assert posts[0].title == "Known from B"
    assert "title" in posts[0].completeness
    assert sources == {"1a_2b": {"A"}}
