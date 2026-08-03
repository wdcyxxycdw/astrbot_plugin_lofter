from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import core.content_source as source_module
import core.mobile_parser as mobile_module
import core.parser as parser_module
from core.author_block import AuthorBlock
from core.content_source import DefaultContentSource
from core.dwr_parser import parse_dwr_response_result
from core.errors import SourceSchemaError
from core.mobile_parser import parse_mobile_blog_page, parse_mobile_post_detail
from core.parser import POST_FIELDS, Post, parse_blog_posts
from core.post_fields import validate_post_evidence
from core.scheduler import _preflight_session
from core.source_scan import SourcePage
from core.storage import Subscription

FIXTURES = Path(__file__).parent / "fixtures" / "lofter"
TIME = "2026-01-01 00:00:00"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _detail_item() -> dict:
    item = _fixture("post_detail.json")["envelope"]["response"]["posts"][0]
    return json.loads(json.dumps(item))


def _detail_payload(item: dict) -> dict:
    return {
        "meta": {"status": 200, "msg": "demo"},
        "response": {"posts": [item]},
    }


def _blog_payload(item: dict) -> dict:
    return {
        "meta": {"status": 200, "msg": "demo"},
        "response": {
            "archives": [],
            "minTimeStamp": 1710000000000,
            "isMember": False,
            "offset": -1,
            "firstPost": None,
            "posts": [item],
        },
    }


def _post(
    post_id: str,
    *,
    owner: str = "demo",
    source: str = "test",
    complete_owner: bool = True,
    publish_time: str = TIME,
) -> Post:
    known = set(POST_FIELDS)
    if not complete_owner:
        known.remove("author_username")
    host = f"{owner}.lofter.com" if owner else "lofter.com"
    return Post(
        post_id=post_id,
        title="Demo",
        summary="Summary",
        content="Content",
        images=[],
        author="Demo",
        author_username=owner if complete_owner else "",
        url=f"https://{host}/post/{post_id}",
        tags=["A"],
        publish_time=publish_time,
        source=source,
        completeness=frozenset(known),
        provenance={field: source for field in known},
    )


def _page(items: list[Post], *, complete: bool = True) -> SourcePage:
    return SourcePage(
        items=items,
        source="mobile_blog",
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=len(items),
        dropped_count=0 if complete else 1,
        complete=complete,
    )


def _sub(target: str, sub_type: str, sub_id: int) -> Subscription:
    return Subscription(
        id=sub_id,
        session_id="session",
        type=sub_type,
        role="subscribe",
        target=target,
    )


def _ownerless_mobile_item() -> dict:
    item = _detail_item()
    item["permalink"] = "https://lofter.com/post/1a_2b"
    item["blogPageUrl"] = "https://lofter.com/post/1a_2b"
    return item


def test_mobile_detail_accepts_ownerless_urls_with_structured_owner():
    post = parse_mobile_post_detail(_detail_payload(_ownerless_mobile_item()))

    assert post.post_id == "1a_2b"
    assert post.author_username == "demo"
    assert post.url == "https://lofter.com/post/1a_2b"


def test_mobile_blog_accepts_ownerless_urls_with_structured_owner():
    page = parse_mobile_blog_page(_blog_payload(_ownerless_mobile_item()))

    assert [post.post_id for post in page.items] == ["1a_2b"]
    assert page.items[0].author_username == "demo"


@pytest.mark.parametrize("field", ["permalink", "blogPageUrl"])
def test_mobile_still_rejects_nonempty_response_owner_conflict(field: str):
    item = _detail_item()
    item[field] = "https://other.lofter.com/post/1a_2b"

    with pytest.raises(SourceSchemaError):
        parse_mobile_post_detail(_detail_payload(item))


def test_mobile_photo_url_value_error_is_typed():
    item = _detail_item()
    item["post"]["photoLinks"] = ["https://["]

    with pytest.raises(SourceSchemaError) as exc_info:
        parse_mobile_post_detail(_detail_payload(item))

    assert exc_info.value.location == "photoLinks"


def test_mobile_url_parser_programming_error_propagates(monkeypatch):
    item = _detail_item()

    def fail(_value):
        raise RuntimeError("programming error")

    monkeypatch.setattr(mobile_module, "urlparse", fail)
    with pytest.raises(RuntimeError, match="programming error"):
        parse_mobile_post_detail(_detail_payload(item))


@pytest.mark.asyncio
async def test_blog_malformed_canonical_url_is_typed():
    html = '<link rel="canonical" href="https://["><a href="/post/1a_2b">p</a>'

    with pytest.raises(SourceSchemaError):
        await parse_blog_posts(html, expected_owner="demo")


@pytest.mark.asyncio
async def test_blog_url_parser_programming_error_propagates(monkeypatch):
    html = '<link rel="canonical" href="https://demo.lofter.com/">'

    def fail(_value):
        raise RuntimeError("programming error")

    monkeypatch.setattr(parser_module, "urlparse", fail)
    with pytest.raises(RuntimeError, match="programming error"):
        await parse_blog_posts(html)


@pytest.mark.asyncio
async def test_blog_relative_link_uses_expected_owner_without_canonical():
    html = '<a href="/post/1a_2b">p</a>'

    posts = await parse_blog_posts(html, expected_owner="demo")

    assert posts[0].url == "https://demo.lofter.com/post/1a_2b"
    assert posts[0].author_username == "demo"


@pytest.mark.asyncio
async def test_blog_relative_link_rejects_declared_owner_conflict():
    html = '<div data-blog-name="other"><a href="/post/1a_2b">p</a></div>'

    with pytest.raises(SourceSchemaError) as exc_info:
        await parse_blog_posts(html, expected_owner="demo")

    assert exc_info.value.location == "post.owner"


@pytest.mark.asyncio
async def test_blog_fallback_runtime_retains_mobile_and_html_evidence(monkeypatch):
    primary = _post("1a_2b", source="mobile_blog")
    raw = _post("1a_2c", source="html_blog", publish_time="")
    raw.completeness = raw.completeness - {"publish_time"}
    client = SimpleNamespace(get=AsyncMock(return_value="html"))
    source = DefaultContentSource(client)
    source._mobile = SimpleNamespace(list_blog=AsyncMock(return_value=SimpleNamespace(
        items=[primary],
        source="mobile_blog",
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=1,
        dropped_count=1,
        complete=False,
        evidence_items=(),
        identity_records=(primary,),
    )))
    monkeypatch.setattr(
        source_module, "parse_blog_posts", AsyncMock(return_value=[raw])
    )
    source.get_post = AsyncMock(side_effect=RuntimeError("ordinary detail failure"))

    with pytest.raises(RuntimeError, match="ordinary detail failure") as exc_info:
        await source.list_blog("demo", None, 20)

    evidence = getattr(exc_info.value, "evidence_items", ())
    assert primary in evidence
    assert raw in evidence


@pytest.mark.asyncio
async def test_blog_enrichment_prefix_participates_in_cross_type_validation():
    tag_post = _post("1a_2b", owner="bob", source="mobile_tag")
    raw_first = _post("1a_2b", owner="", complete_owner=False)
    raw_second = _post("1a_2c", owner="", complete_owner=False)
    detail_first = _post("1a_2b", owner="alice", source="mobile_detail")
    source = AsyncMock()
    source.list_blog.return_value = _page([raw_first, raw_second])
    source.get_post.side_effect = [
        detail_first,
        RuntimeError("ordinary detail failure"),
        RuntimeError("ordinary detail failure"),
    ]
    blocks = AsyncMock()
    blocks.list_by_session.return_value = [
        AuthorBlock("session", "username", "blocked", "blocked")
    ]

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
async def test_dwr_nonidentity_drop_retains_validation_only_identity_witness():
    body = """
    dwr.engine._remoteHandleCallback("0", "0", [
        {post: {blogPageUrl: "https://demo.lofter.com/post/1a_2b"}},
        {post: {
            blogPageUrl: "https://demo.lofter.com/post/1a_2c",
            blogInfo: {blogName: "demo"},
            dirContent: {content: 123}
        }}
    ]);
    """

    result = await parse_dwr_response_result(body)

    assert [post.post_id for post in result.items] == ["1a_2b"]
    assert [post.post_id for post in result.evidence_items] == ["1a_2c"]
    assert result.evidence_items[0].completeness == frozenset({
        "author_username", "url",
    })


@pytest.mark.asyncio
async def test_dwr_all_nonidentity_drops_attach_witnesses_to_items_error():
    body = """
    dwr.engine._remoteHandleCallback("0", "0", [
        {post: {
            blogPageUrl: "https://demo.lofter.com/post/1a_2b",
            dirContent: {content: 123}
        }},
        {post: {
            blogPageUrl: "https://demo.lofter.com/post/1a_2c",
            dirContent: {content: 456}
        }}
    ]);
    """

    with pytest.raises(SourceSchemaError) as exc_info:
        await parse_dwr_response_result(body)

    assert exc_info.value.location == "dwr.items"
    evidence = exc_info.value.evidence_items
    assert [post.post_id for post in evidence] == ["1a_2b", "1a_2c"]
    assert all(
        post.completeness == frozenset({"author_username", "url"})
        for post in evidence
    )


@pytest.mark.asyncio
async def test_tag_fallback_error_keeps_dwr_identity_witness():
    primary = _post("1a_2b", owner="alice", source="mobile_tag")
    body = """
    dwr.engine._remoteHandleCallback("0", "0", [
        {post: {
            blogPageUrl: "https://bob.lofter.com/post/1a_2b",
            dirContent: {content: 123}
        }}
    ]);
    """
    client = SimpleNamespace(search_tag=AsyncMock(return_value=body))
    source = DefaultContentSource(client)
    source._mobile = SimpleNamespace(list_tag=AsyncMock(return_value=SimpleNamespace(
        items=[primary],
        source="mobile_tag",
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=1,
        dropped_count=1,
        complete=False,
        evidence_items=(),
        identity_records=(primary,),
    )))

    page = await source.list_tag("A", None, 20, "new")

    assert [post.author_username for post in page.evidence_items] == [
        "bob", "alice",
    ]
    with pytest.raises(SourceSchemaError) as exc_info:
        validate_post_evidence([*page.evidence_items, *page.items])
    assert exc_info.value.location == "post.owner"


@pytest.mark.asyncio
async def test_dwr_source_page_keeps_witness_out_of_business_items():
    body = """
    dwr.engine._remoteHandleCallback("0", "0", [
        {post: {blogPageUrl: "https://demo.lofter.com/post/1a_2b"}},
        {post: {
            blogPageUrl: "https://demo.lofter.com/post/1a_2c",
            dirContent: {content: 123}
        }}
    ]);
    """
    client = SimpleNamespace(search_tag=AsyncMock(return_value=body))
    source = DefaultContentSource(client)

    page = await source._dwr_page("A", "0", 20, restarted=False)

    assert [post.post_id for post in page.items] == ["1a_2b"]
    assert [post.post_id for post in page.evidence_items] == ["1a_2c"]
    assert page.mapped_count == 1
    assert page.dropped_count == 1
