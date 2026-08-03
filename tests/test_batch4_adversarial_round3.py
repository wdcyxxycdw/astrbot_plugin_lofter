import asyncio
import json
from itertools import product
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.content_source as source_module
from core.content_source import DefaultContentSource
from core.dwr_parser import DWRParseResult
from core.errors import SourceSchemaError
from core.expression_planner import (
    minimum_cover_alternatives,
    parse_count_expression,
)
from core.mobile_parser import MobilePage
from core.parser import POST_FIELDS, Post, parse_embedded_post, parse_blog_posts
from core.scheduler import _enrich_blog_posts, fetch_tag_posts
from core.source_scan import SourcePage
from core.tag_count import count_posts

ALICE_POST = "https://alice.lofter.com/post/1a_2b"


def _post(*, tags=None, owner="alice", source="mobile_tag"):
    known = {"url", "publish_time"}
    if tags is not None:
        known.add("tags")
    if owner:
        known.add("author_username")
    return Post(
        post_id="1a_2b",
        title="Demo",
        summary="",
        tags=tags or [],
        author_username=owner,
        url=f"https://{owner}.lofter.com/post/1a_2b",
        publish_time="2026-01-01 00:00:00",
        source=source,
        completeness=frozenset(known | {"title"}),
    )


def _source_page(items, *, complete=True):
    return SourcePage(
        items=items,
        source="mobile_tag",
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=len(items),
        dropped_count=0 if complete else 1,
        complete=complete,
    )


def _mobile_page(items, *, complete=True, dropped=0, source="mobile_tag"):
    return MobilePage(
        items=items,
        source=source,
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=len(items),
        dropped_count=dropped,
        complete=complete,
    )


@pytest.mark.asyncio
async def test_mobile_blog_response_must_match_requested_owner():
    source = DefaultContentSource(SimpleNamespace())
    source._mobile = SimpleNamespace(
        list_blog=AsyncMock(
            return_value=_mobile_page(
                [_post(owner="bob", source="mobile_blog")],
                source="mobile_blog",
            )
        )
    )

    with pytest.raises(SourceSchemaError, match="post.owner"):
        await source.list_blog("alice", None, 20)


@pytest.mark.asyncio
async def test_html_blog_posts_bind_page_and_expected_owner():
    html = """
    <link rel="canonical" href="https://alice.lofter.com/">
    <a href="https://bob.lofter.com/post/1a_2b">Bob</a>
    """
    with pytest.raises(SourceSchemaError) as exc_info:
        await parse_blog_posts(html, expected_owner="alice")
    assert exc_info.value.location == "post.owner"


@pytest.mark.asyncio
async def test_html_blog_data_owner_must_match_expected_owner():
    html = """
    <div data-blog-name="bob">
      <a href="https://lofter.com/post/1a_2b">Bob</a>
    </div>
    """
    with pytest.raises(SourceSchemaError) as exc_info:
        await parse_blog_posts(html, expected_owner="alice")
    assert exc_info.value.location == "post.owner"


@pytest.mark.asyncio
async def test_ownerless_blog_post_uses_page_owner_identity():
    html = """
    <link rel="canonical" href="https://alice.lofter.com/">
    <a href="https://lofter.com/post/1a_2b">Ownerless</a>
    """
    posts = await parse_blog_posts(html)
    assert posts[0].author_username == "alice"
    assert "author_username" in posts[0].completeness


def test_embedded_rejects_conflicting_post_id_aliases():
    item = {
        "blogId": 26,
        "postId": 43,
        "id": 44,
        "blogPageUrl": ALICE_POST,
        "title": "Demo",
        "content": "Body",
        "blogInfo": {"blogId": 26, "blogName": "alice"},
    }
    html = (
        "<script>window.__initialize_data__ = "
        + json.dumps({"state": {"posts": [item]}})
        + ";</script>"
    )
    with pytest.raises(SourceSchemaError, match="embedded.post.id"):
        parse_embedded_post(html, ALICE_POST)


@pytest.mark.asyncio
async def test_malformed_embedded_ids_propagate_without_fallback():
    item = {
        "blogId": "bad",
        "postId": 43,
        "blogPageUrl": ALICE_POST,
        "title": "Embedded",
        "content": "Body",
        "blogInfo": {"blogId": "bad", "blogName": "alice"},
    }
    html = (
        f'<link rel="canonical" href="{ALICE_POST}"><title>Fallback</title>'
        "<script>window.__initialize_data__ = "
        + json.dumps({"state": {"posts": [item]}})
        + ";</script>"
    )
    client = SimpleNamespace(get=AsyncMock(return_value=html))
    source = DefaultContentSource(client)
    source._mobile.get_post = AsyncMock(side_effect=SourceSchemaError("response"))

    with pytest.raises(SourceSchemaError) as exc_info:
        await source.get_post(ALICE_POST)

    assert exc_info.value.location == "embedded.post.id"
    client.get.assert_awaited_once_with(ALICE_POST, credentialed=False)


@pytest.mark.asyncio
async def test_unsatisfiable_expression_is_exact_zero_without_scanning():
    source = AsyncMock()
    result = await count_posts("A&-A", source)

    assert result.status == "success"
    assert result.count == 0
    assert result.error == ""
    assert result.candidates == 0
    assert result.scanned_pages == {}
    source.list_tag.assert_not_awaited()


@pytest.mark.asyncio
async def test_conflicting_duplicate_post_fields_cannot_be_exact_success():
    source = AsyncMock()
    source.list_tag.return_value = _source_page([
        _post(tags=["A", "B"]),
        _post(tags=["A"]),
    ])

    result = await count_posts("A -B", source)

    assert result.status == "partial"
    assert any("重复作品字段冲突" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_incomplete_mobile_evidence_survives_failed_dwr_fallback():
    item = _post(tags=["A"])
    client = SimpleNamespace(search_tag=AsyncMock(side_effect=SourceSchemaError("dwr.items")))
    source = DefaultContentSource(client)
    source._mobile.list_tag = AsyncMock(
        return_value=_mobile_page([item], complete=False, dropped=1)
    )

    result = await count_posts("A", source)

    assert result.status == "partial"
    assert result.count == 1
    assert result.candidates == 1


@pytest.mark.asyncio
async def test_empty_dwr_fallback_preserves_incomplete_mobile_lower_bound(
    monkeypatch,
):
    item = _post(tags=["A"])
    client = SimpleNamespace(search_tag=AsyncMock(return_value="dwr"))
    source = DefaultContentSource(client)
    source._mobile.list_tag = AsyncMock(
        return_value=_mobile_page([item], complete=False, dropped=1)
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
async def test_nonempty_dwr_short_page_requires_empty_confirmation(monkeypatch):
    item = _post(tags=["A"], source="dwr")
    client = SimpleNamespace(search_tag=AsyncMock(return_value="dwr"))
    source = DefaultContentSource(client)
    monkeypatch.setattr(
        source_module,
        "parse_dwr_response_result",
        AsyncMock(return_value=DWRParseResult([item], 1, 0, False)),
    )

    page = await source._dwr_page("A", "0", 20, restarted=False)

    assert page.exhausted is False
    assert page.next_cursor == "v1:dwr:20"


@pytest.mark.asyncio
async def test_duplicate_only_page_still_records_conflicting_tags():
    source = AsyncMock()
    source.list_tag.side_effect = [
        SourcePage(
            items=[_post(tags=["A"])],
            source="mobile_tag",
            next_cursor="next",
            exhausted=False,
            sort="new",
            mapped_count=1,
            dropped_count=0,
            complete=True,
        ),
        _source_page([
            Post(
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
            )
        ]),
    ]

    result = await count_posts("A -B", source)

    assert result.status == "partial"
    assert result.count == 0
    assert result.candidates == 1
    assert "标签「A」重复作品字段冲突" in result.warnings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expression", "tags_by_scan"),
    [
        ("(A&-B)|(B&-A)", {"A": ["A"], "B": ["B"]}),
        ("A&B", {"A": ["A"], "B": ["A", "B"]}),
    ],
)
async def test_cross_tag_duplicate_fields_cannot_bypass_cover_selection(
    expression, tags_by_scan
):
    source = AsyncMock()

    async def list_tag(tag, cursor, limit, sort):
        return _source_page([_post(tags=tags_by_scan[tag])])

    source.list_tag.side_effect = list_tag
    result = await count_posts(expression, source)

    assert result.status == "partial"
    assert result.count == 0
    assert result.candidates == 1
    assert "重复作品字段冲突" in result.warnings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    ["post.owner", "post.evidence", "dwr.post.id"],
)
async def test_blog_enrichment_propagates_identity_schema_errors(location):
    source = AsyncMock()
    source.get_post.side_effect = SourceSchemaError(location)

    with pytest.raises(SourceSchemaError, match=location):
        await _enrich_blog_posts([_post(tags=None, source="mobile_blog")], source)


@pytest.mark.asyncio
async def test_blog_enrichment_tolerates_metadata_schema_drift():
    source = AsyncMock()
    source.get_post.side_effect = SourceSchemaError("embedded.blogInfo")
    post = _post(tags=None, source="mobile_blog")

    assert await _enrich_blog_posts([post], source) == [post]


@pytest.mark.asyncio
async def test_embedded_fallback_parsing_is_offloaded(monkeypatch):
    client = SimpleNamespace(get=AsyncMock(return_value="html"))
    source = DefaultContentSource(client)
    source._mobile.get_post = AsyncMock(side_effect=SourceSchemaError("response"))
    expected = _post(tags=[], source="embedded_json")
    parse = AsyncMock(return_value=expected)
    monkeypatch.setattr(source_module.asyncio, "to_thread", parse)

    result = await source.get_post(ALICE_POST)

    assert result is expected
    parse.assert_awaited_once()
    assert parse.await_args.args[:3] == (
        parse_embedded_post, "html", ALICE_POST
    )
    assert parse.await_args.kwargs == {"expected_post_id": "1a_2b"}


def test_planner_short_circuits_terminal_low_branch():
    tail = "|".join(f"(B{i}&C{i})" for i in range(7))
    expression = f"-A|(A&({tail}))"

    assert minimum_cover_alternatives(
        parse_count_expression(expression)
    ) == ()


def test_planner_accepts_128_minimal_alternatives():
    expression = "|".join(f"(A{i}&B{i})" for i in range(7))
    expected = {
        frozenset(choice)
        for choice in product(*[(f"A{i}", f"B{i}") for i in range(7)])
    }

    assert set(minimum_cover_alternatives(
        parse_count_expression(expression)
    )) == expected


def test_planner_handles_twelve_independent_or_groups():
    expression = "&".join(f"(A{i}|B{i})" for i in range(12))
    covers = minimum_cover_alternatives(parse_count_expression(expression))
    assert set(covers) == {
        frozenset({f"A{i}", f"B{i}"}) for i in range(12)
    }


@pytest.mark.asyncio
async def test_same_canonical_id_with_conflicting_owners_fails_closed():
    page = _source_page([
        _post(tags=["A"], owner="alice"),
        _post(tags=["A"], owner="bob"),
    ])
    subscription_source = AsyncMock()
    subscription_source.list_tag.return_value = page

    with pytest.raises(SourceSchemaError, match="post.owner"):
        await fetch_tag_posts(["A"], subscription_source)

    count_source = AsyncMock()
    count_source.list_tag.return_value = page
    with pytest.raises(SourceSchemaError, match="post.owner"):
        await count_posts("A", count_source)


@pytest.mark.asyncio
async def test_complete_blog_post_skips_unneeded_detail_enrichment():
    post = _post(tags=[], source="mobile_blog")
    post.completeness = POST_FIELDS
    source = AsyncMock()
    source.get_post.side_effect = SourceSchemaError("post.id")

    assert await _enrich_blog_posts([post], source) == [post]
    source.get_post.assert_not_awaited()
