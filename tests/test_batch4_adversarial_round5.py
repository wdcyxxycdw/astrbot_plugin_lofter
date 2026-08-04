import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.content_source as source_module
from core.content_source import DefaultContentSource
from core.dwr_parser import DWRParseResult
from core.errors import SourceSchemaError
from core.filter import FilterRule
from core.mobile_parser import MobilePage
from core.parser import Post, parse_embedded_post
from core.scheduler import (
    _fetch_all_tag_targets,
    _merge_eligible_tag_posts,
)
from core.source_scan import SourcePage, collect_pages
from core.tag_count import count_posts


def _post(
    post_id: str = "1a_2b",
    *,
    owner: str = "alice",
    tags: list[str] | None = None,
    source: str = "mobile_tag",
    publish_time: str = "2026-01-01 00:00:00",
) -> Post:
    known = {"title", "url", "publish_time", "images"}
    if owner:
        known.add("author_username")
    if tags is not None:
        known.add("tags")
    host = f"{owner}.lofter.com" if owner else "lofter.com"
    return Post(
        post_id=post_id,
        title="Demo",
        summary="",
        images=["https://img.example/a.jpg"],
        tags=tags or [],
        author_username=owner,
        url=f"https://{host}/post/{post_id}",
        publish_time=publish_time,
        source=source,
        completeness=frozenset(known),
    )


def _page(
    items: list[Post],
    *,
    source: str = "mobile_tag",
    next_cursor: str | None = None,
    exhausted: bool = True,
    restarted: bool = False,
    restart_requires_prior_coverage: bool = True,
    evidence: tuple[Post, ...] = (),
) -> SourcePage:
    return SourcePage(
        items=items,
        source=source,
        next_cursor=next_cursor,
        exhausted=exhausted,
        sort="new",
        mapped_count=len(items),
        dropped_count=0,
        complete=True,
        restarted=restarted,
        evidence_items=evidence,
        restart_requires_prior_coverage=restart_requires_prior_coverage,
    )


def _mobile_incomplete(items: list[Post]) -> MobilePage:
    return MobilePage(
        items=items,
        source="mobile_tag",
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=len(items),
        dropped_count=1,
        complete=False,
    )


@pytest.mark.asyncio
async def test_nonempty_dwr_fallback_checks_incomplete_mobile_owner(monkeypatch):
    client = SimpleNamespace(search_tag=AsyncMock(return_value="dwr"))
    source = DefaultContentSource(client)
    source._mobile.list_tag = AsyncMock(
        return_value=_mobile_incomplete([_post(tags=["A"], owner="alice")])
    )
    monkeypatch.setattr(
        source_module,
        "parse_dwr_response_result",
        AsyncMock(side_effect=[
            DWRParseResult([_post(tags=["A"], owner="bob", source="dwr")], 1, 0, False),
            DWRParseResult([], 0, 0, True),
        ]),
    )

    with pytest.raises(SourceSchemaError, match="post.owner"):
        await count_posts("A", source)


@pytest.mark.asyncio
async def test_nonempty_dwr_fallback_uses_mobile_items_as_validation_only(monkeypatch):
    first = _post("1a_2b", tags=["A"], publish_time="2026-01-02 00:00:00")
    second = _post("1a_2c", tags=["A"])
    client = SimpleNamespace(search_tag=AsyncMock(return_value="dwr"))
    source = DefaultContentSource(client)
    source._mobile.list_tag = AsyncMock(
        return_value=_mobile_incomplete([first, second])
    )
    monkeypatch.setattr(
        source_module,
        "parse_dwr_response_result",
        AsyncMock(side_effect=[
            DWRParseResult([first], 1, 0, False),
            DWRParseResult([], 0, 0, True),
        ]),
    )

    result = await count_posts("A", source)

    assert result.status == "partial"
    assert result.count == 1
    assert result.candidates == 1


def test_embedded_conflicting_alias_without_url_cannot_be_skipped():
    valid = {
        "blogId": 26,
        "postId": 43,
        "blogPageUrl": "https://alice.lofter.com/post/1a_2b",
        "title": "Alice",
        "content": "Body",
        "blogInfo": {"blogId": 26, "blogName": "alice"},
    }
    conflicting = {
        "blogId": 26,
        "postId": 43,
        "id": 44,
        "title": "Bob",
        "content": "Body",
        "blogInfo": {"blogId": 26, "blogName": "bob"},
    }
    html = (
        "<script>window.__initialize_data__ = "
        + json.dumps({"state": {"posts": [valid, conflicting]}})
        + ";</script>"
    )

    with pytest.raises(SourceSchemaError, match="embedded.post.id"):
        parse_embedded_post(html, "https://alice.lofter.com/post/1a_2b")


@pytest.mark.asyncio
async def test_count_restart_requires_fallback_to_cover_mobile_witnesses():
    first = _post("1a_2b", tags=["A"], publish_time="2026-01-02 00:00:00")
    second = _post("1a_2c", tags=["A"])
    source = AsyncMock()
    source.list_tag.side_effect = [
        _page([first, second], next_cursor="next", exhausted=False),
        _page(
            [first],
            source="dwr",
            next_cursor="dwr-next",
            exhausted=False,
            restarted=True,
        ),
        _page([], source="dwr"),
    ]

    result = await count_posts("A", source)

    assert result.status == "partial"
    assert result.count == 1
    assert result.candidates == 1


@pytest.mark.asyncio
async def test_count_new_scope_restart_uses_only_dwr_candidates():
    mobile = [
        _post("1a_2b", tags=["A"], publish_time="2026-01-03 00:00:00"),
        _post("1a_2c", tags=["A"], publish_time="2026-01-02 00:00:00"),
    ]
    dwr = [
        _post(
            "1a_2d",
            tags=["A"],
            source="dwr",
            publish_time="2026-01-01 00:00:00",
        )
    ]
    source = AsyncMock()
    source.list_tag.side_effect = [
        _page(mobile, next_cursor="next", exhausted=False),
        _page(
            dwr,
            source="dwr",
            restarted=True,
            restart_requires_prior_coverage=False,
        ),
    ]

    result = await count_posts("A", source)

    assert result.status == "success"
    assert result.count == 1
    assert result.candidates == 1


@pytest.mark.asyncio
async def test_count_new_scope_restart_keeps_explicit_evidence_required():
    mobile = _post("1a_2b", tags=["A"], publish_time="2026-01-03 00:00:00")
    witness = _post("1a_2c", tags=["A"], publish_time="2026-01-02 00:00:00")
    dwr = _post(
        "1a_2d",
        tags=["A"],
        source="dwr",
        publish_time="2026-01-01 00:00:00",
    )
    source = AsyncMock()
    source.list_tag.side_effect = [
        _page(
            [mobile],
            next_cursor="next",
            exhausted=False,
            evidence=(witness,),
        ),
        _page(
            [dwr],
            source="dwr",
            restarted=True,
            restart_requires_prior_coverage=False,
        ),
    ]

    result = await count_posts("A", source)

    assert result.status == "partial"
    assert result.count == 1
    assert result.candidates == 1
    assert any("fallback 未覆盖已有可靠证据" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_collect_pages_restart_preserves_owner_evidence():
    pages = iter([
        _page([_post(tags=["A"], owner="alice")], next_cursor="next", exhausted=False),
        _page([_post(tags=["A"], owner="bob", source="dwr")], source="dwr", restarted=True),
    ])

    with pytest.raises(SourceSchemaError, match="post.owner"):
        await collect_pages(AsyncMock(side_effect=lambda cursor: next(pages)))


@pytest.mark.asyncio
async def test_collect_pages_records_owner_before_repeated_cursor_exit():
    pages = iter([
        _page([_post(tags=["A"], owner="alice")], next_cursor="next", exhausted=False),
        _page(
            [_post(tags=["A"], owner="bob", publish_time="2025-12-31 00:00:00")],
            next_cursor="next",
            exhausted=False,
        ),
    ])

    with pytest.raises(SourceSchemaError, match="post.owner"):
        await collect_pages(AsyncMock(side_effect=lambda cursor: next(pages)))


def test_ineligible_target_cannot_hide_owner_conflict():
    posts = {
        "A": [_post(tags=["A"], owner="alice")],
        "B": [_post(tags=["B"], owner="bob")],
    }

    with pytest.raises(SourceSchemaError, match="post.owner"):
        _merge_eligible_tag_posts(posts, {"A": ["1a_2b"], "B": []}, False)


@pytest.mark.asyncio
async def test_count_propagates_detail_owner_conflict():
    source = AsyncMock()
    source.list_tag.return_value = _page([_post(tags=None, owner="alice")])
    source.get_post.return_value = _post(tags=["A"], owner="bob")

    with pytest.raises(SourceSchemaError, match="post.owner"):
        await count_posts("A", source)


@pytest.mark.asyncio
async def test_count_restart_can_complete_with_same_reliable_ids():
    first = _post("1a_2b", tags=["A"], publish_time="2026-01-02 00:00:00")
    second = _post("1a_2c", tags=["A"])
    source = AsyncMock()
    source.list_tag.side_effect = [
        _page([first, second], next_cursor="next", exhausted=False),
        _page(
            [first, second],
            source="dwr",
            restarted=True,
            evidence=(first, second),
        ),
    ]

    result = await count_posts("A", source)

    assert result.status == "success"
    assert result.count == 2
    assert result.candidates == 2


@pytest.mark.asyncio
async def test_collect_pages_propagates_midscan_identity_error():
    source = AsyncMock()
    source.side_effect = [
        _page([_post(tags=["A"])], next_cursor="next", exhausted=False),
        SourceSchemaError("post.owner"),
    ]

    with pytest.raises(SourceSchemaError, match="post.owner"):
        await collect_pages(source)


@pytest.mark.asyncio
async def test_count_propagates_page_identity_error():
    source = AsyncMock()
    source.list_tag.side_effect = SourceSchemaError("post.owner")

    with pytest.raises(SourceSchemaError, match="post.owner"):
        await count_posts("A", source)


@pytest.mark.asyncio
async def test_scheduler_enriches_ownerless_cross_target_duplicates():
    source = AsyncMock()
    source.list_tag.return_value = _page([_post(tags=None, owner="")])
    source.get_post.side_effect = [
        _post(tags=["A", "B"], owner="alice"),
        _post(tags=["A", "B"], owner="bob"),
    ]
    rule = FilterRule(search_tags=["A", "B"], exclude_tags=[])

    with pytest.raises(SourceSchemaError, match="post.owner"):
        await _fetch_all_tag_targets(rule, source)
