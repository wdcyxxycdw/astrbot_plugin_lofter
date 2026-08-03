import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.content_source import DefaultContentSource
from core.dwr_parser import _map_post
from core.errors import SourceSchemaError
from core.expression_planner import (
    CountExpressionError,
    minimum_cover_alternatives,
    parse_count_expression,
)
from core.filter import FilterRule
from core.parser import Post, parse_embedded_post, parse_post_page
from core.post_consumers import apply_filter_with_fields
from core.source_scan import SourcePage
from core.tag_count import count_posts, match_expression

ALICE_URL = "https://alice.lofter.com/post/1a_2b"
OWNERLESS_URL = "https://lofter.com/post/1a_2b"


def _page(items, *, exhausted=True):
    return SourcePage(
        items=items,
        source="mobile_tag",
        next_cursor=None,
        exhausted=exhausted,
        sort="new",
        mapped_count=len(items),
        dropped_count=0,
        complete=True,
    )


def _post(post_id, publish_time, *, tags=None, url=""):
    fields = {"publish_time"}
    if tags is not None:
        fields.add("tags")
    if url:
        fields.add("url")
    return Post(
        post_id=post_id,
        title="",
        summary="",
        tags=tags or [],
        publish_time=publish_time,
        url=url,
        completeness=frozenset(fields),
    )


def _embedded_html(items):
    payload = {"state": {"posts": items}}
    return (
        "<script>window.__initialize_data__ = "
        + json.dumps(payload)
        + ";</script>"
    )


def _embedded_item(**changes):
    item = {
        "postId": 43,
        "blogId": 26,
        "blogPageUrl": ALICE_URL,
        "title": "Demo",
        "content": "Body",
        "blogInfo": {"blogId": 26, "blogName": "alice"},
    }
    item.update(changes)
    return item


@pytest.mark.asyncio
async def test_count_detail_enrich_obeys_absolute_deadline():
    partial = _post("1a_2b", "2026-01-02 00:00:00", url=ALICE_URL)
    source = AsyncMock()
    source.list_tag.return_value = _page([partial])

    async def get_post(_url):
        await asyncio.Event().wait()

    source.get_post.side_effect = get_post
    result = await asyncio.wait_for(
        count_posts("A", source, _deadline=0.02),
        0.2,
    )

    assert result.status == "partial"
    assert result.candidates == 1
    assert result.count == 0
    assert result.warnings == ["标签「A」扫描超过统计 deadline"]


@pytest.mark.parametrize("parser", ["dwr", "embedded"])
def test_partial_structured_blog_id_conflict_is_rejected(parser):
    blog = {"blogId": 27, "blogName": "alice"}
    if parser == "dwr":
        with pytest.raises(SourceSchemaError, match="dwr.post.id"):
            _map_post({
                "post": {
                    "blogPageUrl": ALICE_URL,
                    "title": "Demo",
                    "blogInfo": blog,
                }
            })
        return
    item = _embedded_item(blogId=None, postId=None, blogInfo=blog)
    with pytest.raises(SourceSchemaError, match="embedded.post.id"):
        parse_embedded_post(_embedded_html([item]), ALICE_URL)


@pytest.mark.parametrize("parser", ["dwr", "embedded"])
def test_partial_structured_post_id_conflict_is_rejected(parser):
    if parser == "dwr":
        with pytest.raises(SourceSchemaError, match="dwr.post.id"):
            _map_post({
                "post": {
                    "postId": 44,
                    "blogPageUrl": ALICE_URL,
                    "title": "Demo",
                    "blogInfo": {"blogName": "alice"},
                }
            })
        return
    item = _embedded_item(
        blogId=None,
        postId=44,
        blogInfo={"blogName": "alice"},
    )
    with pytest.raises(SourceSchemaError, match="embedded.post.id"):
        parse_embedded_post(_embedded_html([item]), ALICE_URL)


def test_opaque_embedded_allows_uncomparable_partial_ids():
    url = "https://alice.lofter.com/post/opaque-id"
    item = _embedded_item(
        blogPageUrl=url,
        blogId=None,
        postId=None,
        blogInfo={"blogId": 27, "blogName": "alice"},
    )
    post = parse_embedded_post(_embedded_html([item]), url)
    assert post.post_id == "opaque-id"


@pytest.mark.asyncio
async def test_html_ownerless_first_does_not_hide_owned_conflict():
    html = f"""
    <link rel="canonical" href="{OWNERLESS_URL}">
    <meta property="og:url" content="https://bob.lofter.com/post/1a_2b">
    <title>Demo</title>
    """
    with pytest.raises(SourceSchemaError, match="post.evidence"):
        await parse_post_page(html, ALICE_URL)


@pytest.mark.asyncio
async def test_html_ownerless_only_remains_valid():
    html = f'<link rel="canonical" href="{OWNERLESS_URL}"><title>Demo</title>'
    post = await parse_post_page(html, OWNERLESS_URL)
    assert post.url == OWNERLESS_URL


def test_embedded_cross_candidate_owner_conflict_is_rejected():
    alpha = _embedded_item(content="Rich", title="Alpha")
    beta = _embedded_item(
        blogPageUrl="https://beta.lofter.com/post/1a_2b",
        title="Beta",
        content=None,
        blogInfo={"blogId": 26, "blogName": "beta"},
    )
    with pytest.raises(SourceSchemaError, match="embedded"):
        parse_embedded_post(_embedded_html([alpha, beta]), ALICE_URL)


def test_embedded_ownerless_sibling_does_not_create_conflict():
    alpha = _embedded_item()
    ownerless = _embedded_item(
        blogPageUrl=OWNERLESS_URL,
        content=None,
        blogInfo={"blogId": 26},
    )
    post = parse_embedded_post(_embedded_html([alpha, ownerless]), ALICE_URL)
    assert post.author_username == "alice"


@pytest.mark.asyncio
async def test_mobile_detail_owner_mismatch_propagates_without_fallback():
    client = SimpleNamespace(get=AsyncMock())
    source = DefaultContentSource(client)
    source._mobile.get_post = AsyncMock(return_value=Post(
        post_id="1a_2b",
        title="Bob",
        summary="",
        url="https://bob.lofter.com/post/1a_2b",
        author_username="bob",
        completeness=frozenset({"title", "url", "author_username"}),
    ))

    with pytest.raises(SourceSchemaError) as exc_info:
        await source.get_post(ALICE_URL)

    assert exc_info.value.location == "post.owner"
    client.get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("request_url", [ALICE_URL, OWNERLESS_URL])
async def test_mobile_detail_same_or_ownerless_request_stays_primary(request_url):
    source = DefaultContentSource(SimpleNamespace())
    mobile = Post(
        post_id="1a_2b",
        title="Alice",
        summary="",
        url=ALICE_URL,
        author_username="alice",
        completeness=frozenset({"title", "url", "author_username"}),
    )
    source._mobile.get_post = AsyncMock(return_value=mobile)
    assert await source.get_post(request_url) is mobile


@pytest.mark.asyncio
async def test_default_post_completeness_does_not_skip_tag_enrich():
    partial = Post(
        post_id="1a_2b",
        title="Demo",
        summary="",
        url=ALICE_URL,
    )
    detail = Post(
        post_id="1a_2b",
        title="Demo",
        summary="",
        tags=["B"],
        url=ALICE_URL,
        completeness=frozenset({"title", "summary", "tags", "url"}),
    )
    source = AsyncMock()
    source.get_post.return_value = detail

    assert partial.completeness == frozenset({"title", "url"})
    located = Post(
        post_id="1a_2b",
        title="Demo",
        summary="",
        url=ALICE_URL,
        completeness=frozenset({"url"}),
    )
    visible = await apply_filter_with_fields(
        [located], FilterRule(["A"], ["B"]), source
    )

    assert visible == []
    source.get_post.assert_awaited_once_with(ALICE_URL)


def test_dwr_serialized_empty_image_list_remains_incomplete():
    post = _map_post({
        "post": {
            "blogPageUrl": ALICE_URL,
            "title": "Demo",
            "firstImageUrl": "[]",
            "blogInfo": {"blogName": "alice"},
        }
    })
    assert post is not None
    assert post.images == []
    assert "images" not in post.completeness


def test_planner_normalizes_case_insensitive_cover_tags():
    expression = "|".join(f"(T{i}&t{i})" for i in range(9))
    covers = minimum_cover_alternatives(parse_count_expression(expression))
    assert covers == (frozenset({*(f"T{i}" for i in range(9))}),)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("A|(B&-B)", {frozenset({"A"})}),
        ("A&(B|-A)", {frozenset({"A"}), frozenset({"B"})}),
        ("(A&-A)|B", {frozenset({"B"})}),
        ("A&(-A|B|C)", {frozenset({"A"}), frozenset({"B", "C"})}),
    ],
)
def test_planner_handles_correlated_positive_and_negative_terms(
    expression, expected
):
    covers = minimum_cover_alternatives(parse_count_expression(expression))
    assert set(covers) == expected


@pytest.mark.asyncio
async def test_count_uses_alternative_cover_after_boolean_simplification():
    source = AsyncMock()

    async def list_tag(tag, cursor, limit, sort):
        if tag.casefold() == "a":
            raise SourceSchemaError("response")
        return _page([])

    source.list_tag.side_effect = list_tag
    result = await count_posts("A&(B|-A)", source)
    assert result.status == "success"
    assert result.scanned_pages == {"B": 0}


@pytest.mark.asyncio
async def test_count_rejects_sort_regression_within_single_page():
    source = AsyncMock()
    source.list_tag.return_value = _page([
        _post("p1", "2026-01-01 00:00:00", tags=["A"]),
        _post("p2", "2026-01-02 00:00:00", tags=["A"]),
    ])

    result = await count_posts("A", source)

    assert result.status == "partial"
    assert result.count == 0
    assert result.candidates == 2
    assert result.warnings == ["标签「A」分页 sort=new 时间倒退"]


def test_planner_fails_closed_when_search_budget_is_exhausted(monkeypatch):
    monkeypatch.setattr(
        "core.expression_planner.MAX_PLANNER_STEPS", 1
    )
    expression = parse_count_expression("A | B")
    with pytest.raises(CountExpressionError, match="计算量超过限制"):
        minimum_cover_alternatives(expression)


def test_count_expression_uses_unicode_casefold_semantics():
    expression = parse_count_expression("Straße | STRASSE")
    post = _post("p1", "2026-01-01 00:00:00", tags=["strasse"])

    assert match_expression(expression, post)
    assert minimum_cover_alternatives(expression) == (
        frozenset({"Straße"}),
    )
