from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.dwr_parser as dwr_module
import core.parser as parser_module
from core.content_source import DefaultContentSource
from core.dwr_parser import _map_post
from core.errors import (
    SourceHTTPError,
    SourceSchemaError,
    attach_source_evidence,
)
from core.parser import Post, extract_initialize_data, parse_blog_posts, parse_embedded_post
from core.source_scan import SourcePage
from core.tag_count import count_posts

POST_URL = "https://demo.lofter.com/post/1a_2b"
DEEP_JSON = "[" * 10_000 + "0" + "]" * 10_000


def _embedded_html(data: object) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"<script>window.__initialize_data__ = {payload};</script>"


def _count_post(post_id: str, owner: str) -> Post:
    return Post(
        post_id=post_id,
        title="Demo",
        summary="",
        author_username=owner,
        url=f"https://{owner}.lofter.com/post/{post_id}",
        tags=["A", "B"],
        publish_time="2026-01-01 00:00:00",
        source="test",
        completeness=frozenset({
            "title", "author_username", "url", "tags", "publish_time",
        }),
    )


def _page(posts: list[Post]) -> SourcePage:
    return SourcePage(
        items=posts,
        source="dwr",
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=len(posts),
        dropped_count=0,
        complete=True,
    )


def test_embedded_deep_json_is_typed_schema_error():
    html = f"<script>window.__initialize_data__ = {DEEP_JSON};</script>"

    with pytest.raises(SourceSchemaError) as exc_info:
        extract_initialize_data(html)

    assert exc_info.value.location == "embedded.json"


def test_embedded_json_does_not_hide_programming_error(monkeypatch):
    def fail(_value):
        raise RuntimeError("programming error")

    monkeypatch.setattr(parser_module.json, "loads", fail)

    with pytest.raises(RuntimeError, match="programming error"):
        extract_initialize_data(
            '<script>window.__initialize_data__ = {"ok": true};</script>'
        )


@pytest.mark.asyncio
async def test_embedded_deep_json_reaches_legacy_html_fallback():
    html = f"""
    <html><head>
    <link rel="canonical" href="{POST_URL}">
    <title>Legacy title-Demo</title>
    <meta name="Description" content="Legacy summary">
    </head><body>
    <script>window.__initialize_data__ = {DEEP_JSON};</script>
    </body></html>
    """
    client = SimpleNamespace(get=AsyncMock(return_value=html))
    source = DefaultContentSource(client)
    source._mobile = SimpleNamespace(
        get_post=AsyncMock(side_effect=SourceSchemaError("response"))
    )

    post = await source.get_post(POST_URL)

    assert post.source == "html_post"
    assert post.title == "Legacy title"
    client.get.assert_awaited_once_with(POST_URL, credentialed=False)


def test_embedded_deep_image_json_is_typed_schema_error():
    data = {
        "post": {
            "postId": 43,
            "blogId": 26,
            "blogPageUrl": POST_URL,
            "title": "Demo",
            "firstImageUrl": DEEP_JSON,
            "blogInfo": {"blogId": 26, "blogName": "demo"},
        }
    }

    with pytest.raises(SourceSchemaError) as exc_info:
        parse_embedded_post(_embedded_html(data), POST_URL)

    assert exc_info.value.location == "embedded.images"


def test_dwr_deep_image_json_remains_unknown():
    mapped = _map_post({
        "post": {
            "blogPageUrl": POST_URL,
            "title": "Demo",
            "firstImageUrl": DEEP_JSON,
            "blogInfo": {"blogName": "demo"},
        }
    })

    assert mapped is not None
    assert mapped.images == []
    assert "images" not in mapped.completeness


def test_dwr_image_json_does_not_hide_programming_error(monkeypatch):
    def fail(_value):
        raise RuntimeError("programming error")

    monkeypatch.setattr(dwr_module.json, "loads", fail)

    with pytest.raises(RuntimeError, match="programming error"):
        _map_post({
            "post": {
                "blogPageUrl": POST_URL,
                "firstImageUrl": "not-json",
                "blogInfo": {"blogName": "demo"},
            }
        })


@pytest.mark.asyncio
async def test_blog_action_and_image_anchors_do_not_claim_known_title():
    html = f"""
    <link rel="canonical" href="https://demo.lofter.com/">
    <a href="{POST_URL}"><img src="cover.jpg"></a>
    <a href="https://demo.lofter.com/post/1a_2c">阅读全文</a>
    """

    posts = await parse_blog_posts(html)

    assert [post.title for post in posts] == ["", "阅读全文"]
    assert all("title" not in post.completeness for post in posts)


@pytest.mark.asyncio
async def test_count_validates_witness_from_failed_source():
    alice = _count_post("1a_2b", "alice")
    bob = _count_post("1a_2b", "bob")
    error = SourceHTTPError(503)
    attach_source_evidence(error, (alice,))
    source = AsyncMock()

    async def list_tag(tag, cursor, limit, sort):
        if tag == "A":
            raise error
        return _page([bob])

    source.list_tag.side_effect = list_tag

    with pytest.raises(SourceSchemaError) as exc_info:
        await count_posts("A&B", source, tag_concurrency=1)

    assert exc_info.value.location == "post.owner"


@pytest.mark.asyncio
async def test_failed_source_witness_is_not_count_candidate():
    witness = _count_post("1a_2c", "bob")
    visible = _count_post("1a_2b", "bob")
    error = SourceHTTPError(503)
    attach_source_evidence(error, (witness,))
    source = AsyncMock()

    async def list_tag(tag, cursor, limit, sort):
        if tag == "A":
            raise error
        return _page([visible])

    source.list_tag.side_effect = list_tag

    result = await count_posts("A&B", source, tag_concurrency=1)

    assert result.status == "success"
    assert result.count == 1
    assert result.candidates == 1
    assert result.scanned_pages == {"B": 1}
