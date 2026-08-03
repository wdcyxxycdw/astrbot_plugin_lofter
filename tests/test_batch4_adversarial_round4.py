from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.content_source as source_module
from core.content_source import DefaultContentSource
from core.dwr_parser import DWRParseResult
from core.errors import SourceSchemaError
from core.mobile_parser import MobilePage
from core.parser import POST_FIELDS, Post, parse_blog_posts
from core.scheduler import (
    _enrich_blog_posts,
    _merge_eligible_tag_posts,
    fetch_tag_posts,
)
from core.source_scan import SourcePage
from core.tag_count import count_posts

ALICE_POST = "https://alice.lofter.com/post/1a_2b"


def _post(tags: list[str], owner: str = "alice") -> Post:
    return Post(
        post_id="1a_2b",
        title="Demo",
        summary="",
        tags=tags,
        author_username=owner,
        url=f"https://{owner}.lofter.com/post/1a_2b",
        publish_time="2026-01-01 00:00:00",
        source="mobile_tag",
        completeness=frozenset({
            "title", "tags", "author_username", "url", "publish_time"
        }),
    )


def _page(post: Post) -> SourcePage:
    return SourcePage(
        items=[post],
        source="mobile_tag",
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=1,
        dropped_count=0,
        complete=True,
    )


@pytest.mark.asyncio
async def test_invalid_repeated_cursor_still_records_conflicting_tags():
    source = AsyncMock()
    source.list_tag.side_effect = [
        SourcePage(
            items=[_post(["A"])],
            source="mobile_tag",
            next_cursor="next",
            exhausted=False,
            sort="new",
            mapped_count=1,
            dropped_count=0,
            complete=True,
        ),
        SourcePage(
            items=[Post(
                post_id="001A_00002B",
                title="Demo",
                summary="",
                tags=["A", "B"],
                author_username="alice",
                url=ALICE_POST,
                publish_time="2025-12-31 00:00:00",
                source="mobile_tag",
                completeness=frozenset({
                    "title", "tags", "author_username", "url", "publish_time"
                }),
            )],
            source="mobile_tag",
            next_cursor="next",
            exhausted=False,
            sort="new",
            mapped_count=1,
            dropped_count=0,
            complete=True,
        ),
    ]

    result = await count_posts("A -B", source)

    assert result.status == "partial"
    assert result.count == 0
    assert result.candidates == 1
    assert "标签「A」重复作品字段冲突" in result.warnings
    assert "标签「A」分页 source/sort/cursor 无进展" in result.warnings


@pytest.mark.asyncio
async def test_empty_dwr_restart_preserves_prior_mobile_lower_bound(monkeypatch):
    first = MobilePage(
        items=[_post(["A"])],
        source="mobile_tag",
        next_cursor="next",
        exhausted=False,
        sort="new",
        mapped_count=1,
        dropped_count=0,
        complete=True,
    )
    source = DefaultContentSource(SimpleNamespace(search_tag=AsyncMock(return_value="dwr")))
    source._mobile.list_tag = AsyncMock(
        side_effect=[first, SourceSchemaError("response")]
    )
    monkeypatch.setattr(
        source_module,
        "parse_dwr_response_result",
        AsyncMock(return_value=DWRParseResult([], 0, 0, True)),
    )

    result = await count_posts("A", source)

    assert result.status == "partial"
    assert result.count == 1
    assert result.candidates == 1


@pytest.mark.asyncio
async def test_fetch_tag_posts_rejects_cross_tag_owner_conflict():
    source = AsyncMock()
    source.list_tag.side_effect = [_page(_post(["A"], "alice")), _page(_post(["B"], "bob"))]

    with pytest.raises(SourceSchemaError, match="post.owner"):
        await fetch_tag_posts(["A", "B"], source)


def test_eligible_tag_merge_rejects_cross_target_owner_conflict():
    posts = {
        "A": [_post(["A"], "alice")],
        "B": [_post(["B"], "bob")],
    }
    with pytest.raises(SourceSchemaError, match="post.owner"):
        _merge_eligible_tag_posts(posts, {"A": ["1a_2b"], "B": ["1a_2b"]}, False)


@pytest.mark.asyncio
async def test_count_rejects_cross_scan_owner_conflict():
    source = AsyncMock()

    async def list_tag(tag, cursor, limit, sort):
        return _page(_post(["A", "B"], "alice" if tag == "A" else "bob"))

    source.list_tag.side_effect = list_tag
    with pytest.raises(SourceSchemaError, match="post.owner"):
        await count_posts("A|B", source)


@pytest.mark.asyncio
async def test_count_rejects_enriched_cross_scan_owner_conflict():
    source = AsyncMock()
    base = Post(
        post_id="1a_2b",
        title="Demo",
        summary="",
        url="https://lofter.com/post/1a_2b",
        publish_time="2026-01-01 00:00:00",
        source="mobile_tag",
        completeness=frozenset({"title", "url", "publish_time"}),
    )
    source.list_tag.return_value = _page(base)
    source.get_post.side_effect = [
        _post(["A", "B"], "alice"),
        _post(["A", "B"], "bob"),
    ]

    with pytest.raises(SourceSchemaError, match="post.owner"):
        await count_posts("A|B", source)


@pytest.mark.asyncio
async def test_ownerless_html_blog_post_binds_expected_owner_during_enrichment():
    html = """
    <link rel="canonical" href="https://alice.lofter.com/">
    <a href="https://lofter.com/post/1a_2b">Ownerless</a>
    """
    posts = await parse_blog_posts(html, expected_owner="alice")
    source = AsyncMock()
    detail = _post(["A"], "bob")
    detail.completeness = POST_FIELDS
    source.get_post.return_value = detail

    with pytest.raises(SourceSchemaError, match="post.owner"):
        await _enrich_blog_posts(posts, source)
