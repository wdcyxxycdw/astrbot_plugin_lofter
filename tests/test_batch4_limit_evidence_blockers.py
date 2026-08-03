from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.content_source as source_module
from core.author_block import AuthorBlock, filter_blocked_posts
from core.content_source import DefaultContentSource
from core.count_scanner import _TagState
from core.dwr_parser import _map_items as map_dwr_items
from core.errors import (
    SourceHTTPError,
    SourceLimitError,
    SourcePartialError,
    SourceSchemaError,
    attach_source_evidence,
)
from core.mobile_parser import parse_mobile_blog_page, parse_mobile_post_detail
from core.parser import (
    Post,
    parse_blog_posts,
    parse_embedded_post,
    parse_post_page,
)
from core.source_limits import MAX_TITLE_BYTES, MAX_URL_BYTES

FIXTURES = Path(__file__).parent / "fixtures" / "lofter"


def _detail_item(post_id: int = 43, **changes) -> dict:
    fixture = json.loads(
        (FIXTURES / "post_detail.json").read_text(encoding="utf-8")
    )
    item = fixture["envelope"]["response"]["posts"][0]
    slug = f"1a_{post_id:x}"
    item["post"]["id"] = post_id
    item["permalink"] = f"https://demo.lofter.com/post/{slug}"
    item["blogPageUrl"] = f"https://demo.lofter.com/post/{slug}"
    item["post"].update(changes)
    return item


def _blog_payload(items: list[dict]) -> dict:
    return {
        "meta": {"status": 200, "msg": "demo"},
        "response": {
            "archives": [],
            "minTimeStamp": 1710000000000,
            "isMember": False,
            "offset": -1,
            "firstPost": None,
            "posts": items,
        },
    }


def _post(post_id: str, owner: str = "demo", *, tags=None) -> Post:
    fields = {"title", "url", "publish_time"}
    if owner:
        fields.add("author_username")
    if tags is not None:
        fields.add("tags")
    host = f"{owner}.lofter.com" if owner else "lofter.com"
    return Post(
        post_id=post_id,
        title="Demo",
        summary="",
        author_username=owner,
        url=f"https://{host}/post/{post_id}",
        tags=tags or [],
        publish_time="2026-01-01 00:00:00",
        source="test",
        completeness=frozenset(fields),
        provenance={field: "test" for field in fields},
    )


def _safe_limit(*evidence: Post) -> SourceLimitError:
    error = SourceLimitError("title", MAX_TITLE_BYTES)
    error.identity_prefix_complete = True
    attach_source_evidence(error, evidence)
    return error


def test_mobile_limit_keeps_dropped_good_and_current_identity_order():
    dropped = _detail_item(43, title=123)
    good = _detail_item(44)
    limited = _detail_item(45, title="x" * (MAX_TITLE_BYTES + 1))

    with pytest.raises(SourceLimitError) as exc_info:
        parse_mobile_blog_page(_blog_payload([dropped, good, limited]))

    evidence = exc_info.value.evidence_items
    assert [post.post_id for post in evidence] == ["1a_2b", "1a_2c", "1a_2d"]
    assert "title" not in evidence[-1].completeness
    assert exc_info.value.identity_prefix_complete is True


def test_dwr_limit_keeps_dropped_good_and_current_identity_order():
    items = [
        {"post": {
            "blogPageUrl": "https://demo.lofter.com/post/1a_2b",
            "dirContent": {"content": 123},
        }},
        {"post": {
            "blogPageUrl": "https://demo.lofter.com/post/1a_2c",
            "title": "Good",
        }},
        {"post": {
            "blogPageUrl": "https://demo.lofter.com/post/1a_2d",
            "title": "x" * (MAX_TITLE_BYTES + 1),
        }},
    ]

    with pytest.raises(SourceLimitError) as exc_info:
        map_dwr_items(items)

    evidence = exc_info.value.evidence_items
    assert [post.post_id for post in evidence] == ["1a_2b", "1a_2c", "1a_2d"]
    assert "title" not in evidence[-1].completeness
    assert exc_info.value.identity_prefix_complete is True


def test_preidentity_limit_is_not_marked_safe_for_fallback():
    oversized = "https://demo.lofter.com/post/" + "x" * MAX_URL_BYTES
    items = [{"post": {"blogPageUrl": oversized}}]

    with pytest.raises(SourceLimitError) as exc_info:
        map_dwr_items(items)

    assert getattr(exc_info.value, "identity_prefix_complete", False) is False
    assert getattr(exc_info.value, "evidence_items", ()) == ()


def test_embedded_limit_keeps_successful_prefix_before_current_witness():
    first = {
        "blogId": 26,
        "postId": 43,
        "blogPageUrl": "https://demo.lofter.com/post/1a_2b",
        "title": "Good",
    }
    limited = {**first, "title": "x" * (MAX_TITLE_BYTES + 1)}
    payload = json.dumps({"state": {"posts": [first, limited]}})
    html = f"<script>window.__initialize_data__ = {payload};</script>"

    with pytest.raises(SourceLimitError) as exc_info:
        parse_embedded_post(html, "https://demo.lofter.com/post/1a_2b")

    evidence = exc_info.value.evidence_items
    assert [post.post_id for post in evidence] == ["1a_2b", "1a_2b"]
    assert "title" in evidence[0].completeness
    assert "title" not in evidence[1].completeness


@pytest.mark.asyncio
async def test_html_post_limit_keeps_current_identity_witness():
    html = (
        '<link rel="canonical" href="https://demo.lofter.com/post/1a_2b">'
        f"<title>{'x' * (MAX_TITLE_BYTES + 1)}</title>"
    )

    with pytest.raises(SourceLimitError) as exc_info:
        await parse_post_page(html, "https://demo.lofter.com/post/1a_2b")

    witness = exc_info.value.evidence_items[0]
    assert witness.post_id == "1a_2b"
    assert witness.author_username == "demo"
    assert "title" not in witness.completeness


@pytest.mark.asyncio
async def test_blog_html_limit_keeps_prefix_before_current_witness():
    html = (
        '<a href="https://demo.lofter.com/post/1a_2b">Good</a>'
        '<a href="https://demo.lofter.com/post/1a_2c">'
        f"{'x' * (MAX_TITLE_BYTES + 1)}</a>"
    )

    with pytest.raises(SourceLimitError) as exc_info:
        await parse_blog_posts(html, expected_owner="demo")

    assert [post.post_id for post in exc_info.value.evidence_items] == [
        "1a_2b", "1a_2c",
    ]


@pytest.mark.asyncio
async def test_unverified_mobile_limit_cannot_enter_post_fallback():
    source = DefaultContentSource(SimpleNamespace())
    source._mobile = SimpleNamespace(
        get_post=AsyncMock(side_effect=SourceLimitError("body", 1))
    )
    source._post_fallback = AsyncMock(return_value=_post("1a_2b"))

    with pytest.raises(SourceLimitError):
        await source.get_post("https://demo.lofter.com/post/1a_2b")

    source._post_fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_blog_credentialed_fallback_must_cover_public_limit_evidence(
    monkeypatch,
):
    first = _post("1a_2b")
    missing = _post("1a_2c")
    client = SimpleNamespace(get=AsyncMock(side_effect=["public", "credentialed"]))
    source = DefaultContentSource(client)
    source._mobile = SimpleNamespace(
        list_blog=AsyncMock(side_effect=SourceSchemaError("response"))
    )
    monkeypatch.setattr(
        source_module,
        "parse_blog_posts",
        AsyncMock(side_effect=[_safe_limit(first, missing), [first]]),
    )

    with pytest.raises(SourcePartialError) as exc_info:
        await source.list_blog("demo", None, 20)

    assert [post.post_id for post in exc_info.value.evidence_items] == [
        "1a_2b", "1a_2c",
    ]


@pytest.mark.asyncio
async def test_blog_credentialed_fallback_keeps_covered_witnesses_validation_only(
    monkeypatch,
):
    first = _post("1a_2b")
    second = _post("1a_2c")
    client = SimpleNamespace(get=AsyncMock(side_effect=["public", "credentialed"]))
    source = DefaultContentSource(client)
    source._mobile = SimpleNamespace(
        list_blog=AsyncMock(side_effect=SourceSchemaError("response"))
    )
    fallback = [_post("1a_2b"), _post("1a_2c")]
    monkeypatch.setattr(
        source_module,
        "parse_blog_posts",
        AsyncMock(side_effect=[_safe_limit(first, second), fallback]),
    )

    page = await source.list_blog("demo", None, 20)

    assert page.items == fallback
    assert page.evidence_items == (first, second)
    assert page.complete is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["page", "evidence"])
async def test_count_detail_error_evidence_is_validation_only(mode: str):
    unknown = _post("1a_2b", owner="", tags=None)
    unknown.completeness = frozenset({"title", "url", "publish_time"})
    witness = _post("1a_2b", owner="", tags=["A"])
    error = SourceHTTPError(503)
    attach_source_evidence(error, (witness,))
    source = SimpleNamespace(get_post=AsyncMock(side_effect=error))
    state = _TagState("A")

    if mode == "page":
        state.observe_page([unknown])
        accepted = await state.consume(
            [unknown], lambda post: bool(post.tags), source, 1.0,
            lambda: 0.0, verified=False,
        )
    else:
        accepted = await state.consume_evidence(
            (unknown,), lambda post: bool(post.tags), source, 1.0,
            lambda: 0.0,
        )

    result = state.result()
    assert accepted is True
    assert result.tag_evidence == {"1a_2b": frozenset({"a"})}
    assert result.matched_ids == set()
    assert result.candidate_ids == ({"1a_2b"} if mode == "page" else set())


def test_mobile_empty_blog_name_uses_verified_url_owner():
    payload = _blog_payload([_detail_item()])
    item = payload["response"]["posts"][0]
    item["blogInfo"]["blogName"] = ""

    post = parse_mobile_post_detail({
        "meta": payload["meta"],
        "response": {"posts": [item]},
    })

    assert post.author_username == "demo"
    assert "author_username" in post.completeness
    assert post.provenance["author_username"] == "mobile_detail"


def test_mobile_ownerless_detail_does_not_claim_username_completeness():
    item = _detail_item()
    item["blogInfo"].update({
        "blogName": "",
        "homePageUrl": "https://lofter.com/",
    })
    item["permalink"] = "https://lofter.com/post/1a_2b"
    item["blogPageUrl"] = "https://lofter.com/post/1a_2b"
    payload = {
        "meta": {"status": 200, "msg": "demo"},
        "response": {"posts": [item]},
    }

    post = parse_mobile_post_detail(payload)

    assert post.author_username == ""
    assert "author_username" not in post.completeness
    assert "author_username" not in post.provenance


def test_mobile_url_owner_can_match_author_block_when_blog_name_is_empty():
    item = _detail_item()
    item["blogInfo"]["blogName"] = ""
    payload = {
        "meta": {"status": 200, "msg": "demo"},
        "response": {"posts": [item]},
    }
    post = parse_mobile_post_detail(payload)
    blocks = [AuthorBlock("session", "username", "demo", "demo")]

    visible, blocked = filter_blocked_posts([post], blocks)

    assert visible == []
    assert blocked == [post]
