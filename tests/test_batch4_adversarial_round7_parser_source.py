from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.content_source as source_module
from core.content_source import DefaultContentSource
from core.dwr_parser import parse_dwr_response_result
from core.errors import SourcePartialError, SourceSchemaError
from core.mobile_parser import MobilePage, parse_mobile_tag_page
from core.parser import Post, parse_embedded_post
from core.post_fields import merge_post_fields
from core.source_scan import SourcePage, collect_pages


def _post(
    post_id: str,
    *,
    owner: str = "demo",
    tags: list[str] | None = None,
    publish_time: str | None = "2026-01-01 00:00:00",
    source: str = "mobile_blog",
) -> Post:
    host = f"{owner}.lofter.com" if owner else "lofter.com"
    known = {"title", "url"}
    if owner:
        known.add("author_username")
    if tags is not None:
        known.add("tags")
    if publish_time is not None:
        known.add("publish_time")
    return Post(
        post_id=post_id,
        title="Demo",
        summary="",
        author_username=owner,
        tags=tags or [],
        url=f"https://{host}/post/{post_id}",
        publish_time=publish_time or "",
        source=source,
        completeness=frozenset(known),
    )


def _page(
    items: list[Post],
    *,
    source: str = "dwr",
    cursor: str | None = None,
    exhausted: bool = True,
    evidence: tuple[Post, ...] = (),
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
        evidence_items=evidence,
    )


def _mobile_blog(items: list[Post], *, complete: bool = False) -> MobilePage:
    return MobilePage(
        items=items,
        source="mobile_blog",
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=len(items),
        dropped_count=0 if complete else 1,
        complete=complete,
    )


def _tag_item(
    post_id: str = "1a_2b",
    *,
    view_blog_id: int = 26,
    count_blog_id: int | None = 26,
) -> dict:
    count = {} if count_blog_id is None else {"blogId": count_blog_id}
    return {
        "postData": {
            "postView": {
                "blogId": view_blog_id,
                "permalink": f"https://demo.lofter.com/post/{post_id}",
                "photoCount": 0,
                "title": "Demo",
                "publishTime": 1710000000000,
            },
            "postCount": count,
        }
    }


def _tag_payload(items: list[object]) -> dict:
    return {"code": 0, "msg": "demo", "data": {"list": items, "offset": -1}}


@pytest.mark.asyncio
async def test_mobile_detail_generic_url_error_uses_public_fallback(monkeypatch):
    expected = _post("1a_2b", source="embedded_json")
    client = SimpleNamespace(get=AsyncMock(return_value="html"))
    source = DefaultContentSource(client)
    source._mobile.get_post = AsyncMock(side_effect=SourceSchemaError("url"))
    monkeypatch.setattr(source_module, "parse_embedded_post", lambda *args, **kwargs: expected)

    result = await source.get_post("https://demo.lofter.com/post/1a_2b")

    assert result is expected
    client.get.assert_awaited_once_with(
        "https://demo.lofter.com/post/1a_2b", credentialed=False
    )


@pytest.mark.asyncio
async def test_embedded_generic_blog_metadata_error_uses_legacy_html():
    item = {
        "blogId": 26,
        "postId": 43,
        "blogPageUrl": "https://demo.lofter.com/post/1a_2b",
        "title": "Embedded",
        "content": "Body",
        "blogInfo": {
            "blogId": 26,
            "blogName": "demo",
            "blogNickName": 123,
        },
    }
    html = (
        '<link rel="canonical" href="https://demo.lofter.com/post/1a_2b">'
        "<title>Fallback-Demo</title>"
        "<script>window.__initialize_data__ = "
        + json.dumps({"state": {"post": item}})
        + ";</script>"
    )
    client = SimpleNamespace(get=AsyncMock(return_value=html))
    source = DefaultContentSource(client)
    source._mobile.get_post = AsyncMock(side_effect=SourceSchemaError("response"))

    result = await source.get_post("https://demo.lofter.com/post/1a_2b")

    assert result.source == "html_post"
    assert result.title == "Fallback"


def test_merge_owner_conflict_uses_precise_identity_location():
    base = _post("1a_2b", owner="alice")
    detail = _post("1a_2b", owner="bob", source="mobile_detail")

    with pytest.raises(SourceSchemaError) as exc_info:
        merge_post_fields(base, detail)

    assert exc_info.value.location == "post.owner"


@pytest.mark.asyncio
@pytest.mark.parametrize("falsey", ["0", "false", "[]", "{}"])
async def test_dwr_falsey_nonstring_summary_is_dropped(falsey):
    body = f"""
    dwr.engine._remoteHandleCallback("0", "0", [
        {{post: {{blogPageUrl: "https://demo.lofter.com/post/1a_2b"}}}},
        {{post: {{
            blogPageUrl: "https://demo.lofter.com/post/1a_2c",
            dirContent: {{content: {falsey}}}
        }}}}
    ]);
    """

    result = await parse_dwr_response_result(body)

    assert result.mapped_count == 1
    assert result.dropped_count == 1
    assert result.complete is False


def test_embedded_low_score_sibling_still_contributes_owner_identity():
    valid = {
        "blogId": 26,
        "postId": 43,
        "blogPageUrl": "https://alice.lofter.com/post/1a_2b",
        "title": "Alice",
        "content": "Body",
        "blogInfo": {"blogId": 26, "blogName": "alice"},
    }
    identity_only = {
        "blogId": 26,
        "postId": 43,
        "blogPageUrl": "https://bob.lofter.com/post/1a_2b",
        "blogInfo": {"blogId": 26, "blogName": "bob"},
    }
    html = (
        "<script>window.__initialize_data__ = "
        + json.dumps({"state": {"posts": [valid, identity_only]}})
        + ";</script>"
    )

    with pytest.raises(SourceSchemaError) as exc_info:
        parse_embedded_post(html, "https://alice.lofter.com/post/1a_2b")

    assert exc_info.value.location == "embedded.post.owner"


def test_mobile_identity_preflight_precedes_ordinary_missing_field():
    conflicting = _tag_item(view_blog_id=27, count_blog_id=None)

    with pytest.raises(SourceSchemaError) as exc_info:
        parse_mobile_tag_page(_tag_payload([_tag_item(), conflicting]))

    assert exc_info.value.location == "postData.postView.blogId"


@pytest.mark.asyncio
async def test_incomplete_mobile_blog_owner_conflict_cannot_be_replaced(monkeypatch):
    client = SimpleNamespace(get=AsyncMock(return_value="html"))
    source = DefaultContentSource(client)
    source._mobile.list_blog = AsyncMock(
        return_value=_mobile_blog([_post("1a_2b", owner="bob")])
    )
    monkeypatch.setattr(
        source_module,
        "parse_blog_posts",
        AsyncMock(return_value=[_post("1a_2b", owner="alice", source="html_blog")]),
    )

    with pytest.raises(SourceSchemaError) as exc_info:
        await source.list_blog("alice", None, 20)

    assert exc_info.value.location == "post.owner"
    client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_incomplete_mobile_blog_ids_remain_fallback_evidence(monkeypatch):
    primary = [_post("1a_2b"), _post("1a_2c")]
    client = SimpleNamespace(get=AsyncMock(return_value="html"))
    source = DefaultContentSource(client)
    source._mobile.list_blog = AsyncMock(return_value=_mobile_blog(primary))
    monkeypatch.setattr(
        source_module,
        "parse_blog_posts",
        AsyncMock(return_value=[_post("1a_2b", source="html_blog")]),
    )

    with pytest.raises(SourcePartialError):
        await collect_pages(lambda cursor: source.list_blog("demo", cursor, 20))


@pytest.mark.asyncio
async def test_complete_blog_fallback_keeps_fallback_objects(monkeypatch):
    primary = [_post("1a_2b"), _post("1a_2c")]
    fallback = [
        _post("1a_2b", source="html_blog"),
        _post("1a_2c", source="html_blog"),
    ]
    client = SimpleNamespace(get=AsyncMock(return_value="html"))
    source = DefaultContentSource(client)
    source._mobile.list_blog = AsyncMock(return_value=_mobile_blog(primary))
    monkeypatch.setattr(
        source_module, "parse_blog_posts", AsyncMock(return_value=fallback)
    )

    result = await collect_pages(
        lambda cursor: source.list_blog("demo", cursor, 20)
    )

    assert result.items == fallback
    assert all(actual is expected for actual, expected in zip(result.items, fallback))
    assert {post.post_id for post in result.evidence_items} == {"1a_2b", "1a_2c"}


@pytest.mark.asyncio
async def test_limit_coverage_accepts_validated_tail_outside_business_prefix():
    evidence = (_post("1a_2b"),)
    page = _page([_post("1a_2c"), _post("1a_2b")], evidence=evidence)

    result = await collect_pages(AsyncMock(return_value=page), limit=1)

    assert [post.post_id for post in result.items] == ["1a_2c"]
    assert [post.post_id for post in result.evidence_items] == ["1a_2b", "1a_2b"]


@pytest.mark.asyncio
async def test_limit_coverage_accepts_visible_fallback_prefix():
    evidence = (_post("1a_2b"),)
    page = _page([_post("1a_2b"), _post("1a_2c")], evidence=evidence)

    result = await collect_pages(AsyncMock(return_value=page), limit=1)

    assert [post.post_id for post in result.items] == ["1a_2b"]


@pytest.mark.asyncio
async def test_source_scan_rejects_sort_regression_within_exact_page():
    page = _page([
        _post("1a_2b", publish_time="2026-01-01 00:00:00"),
        _post("1a_2c", publish_time="2026-01-02 00:00:00"),
    ])

    with pytest.raises(SourcePartialError):
        await collect_pages(AsyncMock(return_value=page))


@pytest.mark.asyncio
async def test_source_scan_exact_multi_item_page_requires_publish_time():
    page = _page([
        _post("1a_2b", publish_time=None),
        _post("1a_2c", publish_time="2026-01-01 00:00:00"),
    ])

    with pytest.raises(SourcePartialError):
        await collect_pages(AsyncMock(return_value=page))


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["tags", "publish_time"])
async def test_source_scan_rejects_duplicate_reliable_field_conflict(field):
    first = _post(
        "1a_2b", tags=["A"], publish_time="2026-01-03 00:00:00"
    )
    duplicate = _post(
        "1a_2b",
        tags=["B"] if field == "tags" else ["A"],
        publish_time=(
            "2026-01-03 00:00:00" if field == "tags"
            else "2026-01-02 00:00:00"
        ),
    )
    second = _post("1a_2c", tags=["A"], publish_time="2026-01-01 00:00:00")
    pages = iter([
        _page([first], cursor="next", exhausted=False),
        _page([duplicate, second]),
    ])

    with pytest.raises(SourceSchemaError) as exc_info:
        await collect_pages(AsyncMock(side_effect=lambda cursor: next(pages)))

    assert exc_info.value.location == "post.evidence"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_tags", "duplicate_tags"),
    [
        (["A", "b"], ["B", "a"]),
        (None, ["A"]),
    ],
)
async def test_source_scan_accepts_equivalent_or_newly_known_tags(
    first_tags, duplicate_tags
):
    first = _post(
        "1a_2b", tags=first_tags, publish_time="2026-01-03 00:00:00"
    )
    duplicate = _post(
        "1a_2b", tags=duplicate_tags, publish_time="2026-01-03 00:00:00"
    )
    second = _post("1a_2c", tags=["A"], publish_time="2026-01-01 00:00:00")
    pages = iter([
        _page([first], cursor="next", exhausted=False),
        _page([duplicate, second]),
    ])

    result = await collect_pages(
        AsyncMock(side_effect=lambda cursor: next(pages))
    )

    assert [post.post_id for post in result.items] == ["1a_2b", "1a_2c"]
