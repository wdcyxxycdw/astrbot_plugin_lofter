from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.content_source as source_module
from core.content_source import DefaultContentSource
from core.count_scanner import _TagState, scan_tags
from core.errors import (
    PostEvidenceError,
    SourceHTTPError,
    SourcePartialError,
    SourceSchemaError,
)
from core.filter import FilterRule
from core.mobile_parser import parse_mobile_post_detail, parse_mobile_tag_page
from core.parser import Post, parse_embedded_post, parse_post_page
from core.post_fields import merge_post_fields
from core.scheduler import _fetch_all_tag_targets
from core.source_scan import SourcePage, collect_pages
from core.tag_count import count_posts

FIXTURES = Path(__file__).parent / "fixtures" / "lofter"
POST_URL = "https://demo.lofter.com/post/1a_2b"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _post(
    post_id: str = "1a_2b",
    *,
    tags: list[str] | None = None,
    url: str = POST_URL,
    publish_time: str = "2026-01-01 00:00:00",
) -> Post:
    known = {"title", "url", "publish_time", "author_username", "images"}
    if tags is not None:
        known.add("tags")
    return Post(
        post_id=post_id,
        title="Demo",
        summary="",
        images=["https://img.example/a.jpg"],
        author_username="demo",
        tags=tags or [],
        url=url,
        publish_time=publish_time,
        source="mobile_tag",
        completeness=frozenset(known),
    )


def _page(
    items: list[Post], *, evidence: tuple[Post, ...] = (), complete: bool = True
) -> SourcePage:
    return SourcePage(
        items=items,
        source="mobile_tag",
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=len(items),
        dropped_count=0 if complete else 1,
        complete=complete,
        evidence_items=evidence,
    )


def _embedded_html(items: list[dict]) -> str:
    payload = json.dumps({"state": {"posts": items}})
    return f"<script>window.__initialize_data__ = {payload};</script>"


def _embedded_item(**changes) -> dict:
    item = {
        "postId": 43,
        "blogId": 26,
        "blogPageUrl": POST_URL,
        "title": "Demo",
        "content": "Body",
        "blogInfo": {"blogId": 26, "blogName": "demo"},
    }
    item.update(changes)
    return item


def test_mobile_detail_rejects_id_and_post_id_alias_conflict():
    payload = _fixture("post_detail.json")["envelope"]
    payload = json.loads(json.dumps(payload))
    payload["response"]["posts"][0]["post"]["postId"] = 44

    with pytest.raises(SourceSchemaError) as exc_info:
        parse_mobile_post_detail(payload)

    assert exc_info.value.location == "post.id"


def test_mobile_tag_rejects_id_and_post_id_alias_conflict():
    payload = _fixture("tag_posts.json")["envelope"]
    payload = json.loads(json.dumps(payload))
    view = payload["data"]["list"][0]["postData"]["postView"]
    view.update({"id": 43, "postId": 44})

    with pytest.raises(SourceSchemaError) as exc_info:
        parse_mobile_tag_page(payload)

    assert exc_info.value.location == "post.id"


def test_mobile_ownerless_homepage_is_not_owner_conflict():
    payload = _fixture("post_detail.json")["envelope"]
    payload = json.loads(json.dumps(payload))
    payload["response"]["posts"][0]["blogInfo"]["homePageUrl"] = (
        "https://lofter.com/"
    )

    post = parse_mobile_post_detail(payload)

    assert post.author_username == "demo"


def test_embedded_rejects_conflicting_tag_aliases():
    item = _embedded_item(tags=["A"], tag=["B"])

    with pytest.raises(SourceSchemaError) as exc_info:
        parse_embedded_post(_embedded_html([item]), POST_URL)

    assert exc_info.value.location == "embedded.tags"


@pytest.mark.asyncio
async def test_embedded_unrelated_post_allows_html_fallback():
    unrelated = _embedded_item(
        postId=44,
        blogPageUrl="https://demo.lofter.com/post/1a_2c",
    )
    html = (
        _embedded_html([unrelated])
        + f'<link rel="canonical" href="{POST_URL}"><title>Fallback</title>'
    )
    client = SimpleNamespace(get=AsyncMock(return_value=html))
    source = DefaultContentSource(client)
    source._mobile.get_post = AsyncMock(side_effect=SourceSchemaError("response"))

    post = await source.get_post(POST_URL)

    assert post.source == "html_post"
    assert post.title == "Fallback"
    client.get.assert_awaited_once_with(POST_URL, credentialed=False)


@pytest.mark.asyncio
async def test_public_html_without_identity_reaches_credentialed_fallback():
    public = "<title>Public shell</title>"
    credentialed = f'<link rel="canonical" href="{POST_URL}"><title>Private</title>'
    client = SimpleNamespace(get=AsyncMock(side_effect=[public, credentialed]))
    source = DefaultContentSource(client)
    source._mobile.get_post = AsyncMock(side_effect=SourceSchemaError("response"))

    post = await source.get_post(POST_URL)

    assert post.title == "Private"
    assert [call.kwargs["credentialed"] for call in client.get.await_args_list] == [
        False,
        True,
    ]


@pytest.mark.asyncio
async def test_html_identity_conflict_precedes_missing_content():
    html = '<link rel="canonical" href="https://bob.lofter.com/post/1a_2b">'

    with pytest.raises(SourceSchemaError) as exc_info:
        await parse_post_page(html, "https://alice.lofter.com/post/1a_2b")

    assert exc_info.value.location == "post.evidence"


@pytest.mark.asyncio
async def test_blog_fallback_failure_preserves_incomplete_primary_counts():
    primary = _post()
    client = SimpleNamespace(
        get=AsyncMock(side_effect=[SourceHTTPError(503), SourceHTTPError(503)])
    )
    source = DefaultContentSource(client)
    source._mobile.list_blog = AsyncMock(
        return_value=SimpleNamespace(
            items=[primary],
            source="mobile_blog",
            next_cursor=None,
            exhausted=True,
            sort="new",
            mapped_count=1,
            dropped_count=1,
            complete=False,
        )
    )

    with pytest.raises(SourcePartialError) as exc_info:
        await collect_pages(lambda cursor: source.list_blog("demo", cursor, 20))

    assert (exc_info.value.mapped_count, exc_info.value.dropped_count) == (1, 1)


@pytest.mark.asyncio
async def test_count_does_not_match_witness_after_earlier_detail_timeout():
    unknown = _post(tags=None)
    known = _post("1a_2c", tags=["A"], url="https://demo.lofter.com/post/1a_2c")
    source = AsyncMock()
    source.list_tag.return_value = _page([], evidence=(unknown, known))

    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    source.get_post.side_effect = hang
    result = await count_posts("A", source, _deadline=0.02)

    assert result.status == "partial"
    assert result.count == 0
    assert result.candidates == 0


@pytest.mark.asyncio
async def test_scan_tag_evidence_is_not_candidate_or_match():
    witness = _post(tags=["A"])
    source = AsyncMock()
    source.list_tag.return_value = _page([], evidence=(witness,))

    results = await scan_tags(
        ["A"],
        source,
        20,
        lambda post: "a" in {tag.casefold() for tag in post.tags},
        1,
        1.0,
        lambda: 0.0,
    )

    result = results["a"]
    assert result.candidate_ids == set()
    assert result.matched_ids == set()
    assert result.tag_evidence == {witness.post_id: frozenset({"a"})}
    assert result.owner_evidence == {witness.post_id: "demo"}


def test_count_scan_exports_canonical_url_evidence():
    state = _TagState("A")
    state.observe_page((_post(tags=["A"]),))

    assert state.result().url_evidence == {"1a_2b": POST_URL}


def test_merge_rejects_known_publish_time_conflict():
    base = _post(tags=["A"], publish_time="2026-01-02 00:00:00")
    detail = _post(tags=["A"], publish_time="2026-01-01 00:00:00")

    with pytest.raises(PostEvidenceError) as exc_info:
        merge_post_fields(base, detail)

    assert exc_info.value.location == "post.evidence"
    assert exc_info.value.diagnostic == (
        "field_conflict:publish_time:post_ledger"
    )


def test_merge_prefers_owned_url_over_ownerless_url():
    base = _post(url="https://lofter.com/post/1a_2b", tags=["A"])
    detail = _post(tags=["A"])

    merged = merge_post_fields(base, detail)

    assert merged.url == POST_URL
    assert merged.author_username == "demo"


@pytest.mark.asyncio
async def test_global_field_conflict_cannot_hide_behind_empty_cover():
    source = AsyncMock()

    async def list_tag(tag, cursor, limit, sort):
        if tag == "A":
            return _page([_post(tags=["A"])])
        if tag == "B":
            return _page([_post(tags=["B"])])
        return _page([])

    source.list_tag.side_effect = list_tag
    result = await count_posts("A&B&C", source)

    assert result.status == "partial"
    assert result.count == 0
    assert "重复作品字段冲突" in result.warnings


@pytest.mark.asyncio
async def test_casefold_aliases_are_all_queried_before_exact_success():
    source = AsyncMock()
    calls: list[str] = []

    async def list_tag(tag, cursor, limit, sort):
        calls.append(tag)
        if tag == "STRASSE":
            return _page([_post(tags=["strasse"])])
        return _page([])

    source.list_tag.side_effect = list_tag
    result = await count_posts("Straße|STRASSE", source)

    assert calls == ["Straße", "STRASSE"]
    assert result.status == "success"
    assert result.count == 1


@pytest.mark.asyncio
async def test_scheduler_validates_cross_target_fields_before_exclude():
    source = AsyncMock()

    async def list_tag(tag, cursor, limit, sort):
        tags = [tag, "X"] if tag == "A" else [tag]
        return _page([_post(tags=tags)])

    source.list_tag.side_effect = list_tag
    rule = FilterRule(search_tags=["A", "B"], exclude_tags=["X"])

    with pytest.raises(SourceSchemaError) as exc_info:
        await _fetch_all_tag_targets(rule, source)

    assert exc_info.value.location == "post.evidence"


@pytest.mark.asyncio
async def test_scheduler_revalidates_fields_enriched_across_targets():
    source = AsyncMock()
    source.list_tag.side_effect = lambda tag, cursor, limit, sort: _page([
        _post(tags=None)
    ])
    source.get_post.side_effect = [
        _post(tags=["A"]),
        _post(tags=["B", "X"]),
    ]
    rule = FilterRule(search_tags=["A", "B"], exclude_tags=["X"])

    with pytest.raises(SourceSchemaError) as exc_info:
        await _fetch_all_tag_targets(rule, source)

    assert exc_info.value.location == "post.evidence"
