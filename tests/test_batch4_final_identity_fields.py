import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.author_block import AuthorBlock
from core.content_source import DefaultContentSource
from core.dwr_parser import _map_post
from core.errors import SourcePartialError, SourceSchemaError
from core.mobile_parser import parse_mobile_post_detail, parse_mobile_tag_page
from core.parser import Post, parse_embedded_post, parse_post_page
from core.post_consumers import ensure_subscription_posts, filter_blocked_with_fields
from core.post_fields import merge_post_fields

FIXTURES = Path(__file__).parent / "fixtures" / "lofter"
DETAIL_URL = "https://demo.lofter.com/post/1a_2b"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _embedded_html(item: dict | list[dict]) -> str:
    data = {"state": {"detail": {"post": item}}}
    return (
        "<html><script>window.__initialize_data__ = "
        + json.dumps(data)
        + ";</script></html>"
    )


def _embedded_item(**changes) -> dict:
    item = {
        "postId": 43,
        "blogId": 26,
        "blogPageUrl": DETAIL_URL,
        "title": "Demo",
        "dirContent": "Summary",
        "content": "Content",
        "blogInfo": {
            "blogId": 26,
            "blogNickName": "Demo",
            "blogName": "demo",
        },
    }
    item.update(changes)
    return item


@pytest.mark.parametrize("timestamp", [0, -1])
def test_mobile_nonpositive_publish_time_is_not_reliable(timestamp):
    detail = _fixture("post_detail.json")["envelope"]
    detail = json.loads(json.dumps(detail))
    detail["response"]["posts"][0]["post"]["publishTime"] = timestamp
    tag = _fixture("tag_posts.json")["envelope"]
    tag = json.loads(json.dumps(tag))
    tag["data"]["list"][0]["postData"]["postView"][
        "publishTime"
    ] = timestamp

    with pytest.raises(SourceSchemaError, match="publishTime"):
        parse_mobile_post_detail(detail)
    with pytest.raises(SourcePartialError):
        parse_mobile_tag_page(tag)


@pytest.mark.asyncio
async def test_mobile_bare_url_username_stays_unknown_and_is_enriched():
    payload = _fixture("tag_posts.json")["envelope"]
    payload = json.loads(json.dumps(payload))
    payload["data"]["list"][0]["postData"]["postView"][
        "permalink"
    ] = "https://lofter.com/post/1a_2b"
    partial = parse_mobile_tag_page(payload).items[0]
    detail = parse_mobile_post_detail(
        _fixture("post_detail.json")["envelope"]
    )
    source = AsyncMock()
    source.get_post.return_value = detail

    visible, blocked = await filter_blocked_with_fields(
        [partial],
        [AuthorBlock("session", "username", "demo", "demo")],
        source,
    )

    assert "author_username" not in partial.completeness
    assert visible == []
    assert [post.author_username for post in blocked] == ["demo"]
    source.get_post.assert_awaited_once_with(partial.url)


def test_dwr_rejects_url_numeric_and_blog_name_conflict():
    item = {
        "post": {
            "id": 44,
            "blogId": 27,
            "blogPageUrl": "https://alice.lofter.com/post/1a_2b",
            "title": "Demo",
            "publishTime": 1710000000000,
            "blogInfo": {
                "blogId": 27,
                "blogName": "bob",
                "blogNickName": "Bob",
            },
        }
    }

    with pytest.raises(SourceSchemaError):
        _map_post(item)


def test_embedded_rejects_same_slug_owner_conflicts():
    item = _embedded_item(
        blogPageUrl="https://beta.lofter.com/post/1a_2b",
        postUrl="https://alpha.lofter.com/post/1a_2b",
        blogInfo={
            "blogId": 26,
            "blogNickName": "Gamma",
            "blogName": "gamma",
        },
    )

    with pytest.raises(SourceSchemaError):
        parse_embedded_post(
            _embedded_html(item),
            "https://alpha.lofter.com/post/1a_2b",
        )


def test_embedded_rejects_conflicting_structured_blog_ids():
    item = _embedded_item(blogId=27)

    with pytest.raises(SourceSchemaError, match="embedded.post.id"):
        parse_embedded_post(_embedded_html(item), DETAIL_URL)


def test_embedded_rejects_conflicting_duplicate_candidate_fields():
    first = _embedded_item(tags=["allow"])
    second = _embedded_item(
        tags=["deny"],
        images=["https://example.invalid/a.jpg"],
    )

    with pytest.raises(SourceSchemaError) as exc_info:
        parse_embedded_post(_embedded_html([first, second]), DETAIL_URL)

    assert exc_info.value.location == "post.evidence"


@pytest.mark.asyncio
async def test_invalid_embedded_url_falls_through_to_html_parser():
    item = _embedded_item(
        postUrl="http://demo.lofter.com/post/1a_2b",
        blogPageUrl=None,
    )
    html = (
        _embedded_html(item)
        + '<link rel="canonical" href="https://demo.lofter.com/post/1a_2b">'
        + "<title>Fallback-Demo</title><p id='p_body'>HTML-BODY</p>"
    )
    client = SimpleNamespace(get=AsyncMock(return_value=html))
    source = DefaultContentSource(client)
    source._mobile.get_post = AsyncMock(
        side_effect=SourceSchemaError("response")
    )

    post = await source.get_post(DETAIL_URL)

    assert post.source == "html_post"
    assert post.content == "HTML-BODY"
    client.get.assert_awaited_once_with(DETAIL_URL, credentialed=False)


def test_embedded_opaque_url_allows_unprovable_numeric_ids():
    url = "https://demo.lofter.com/post/opaque-id"
    item = _embedded_item(blogPageUrl=url)

    post = parse_embedded_post(_embedded_html(item), url)

    assert post.post_id == "opaque-id"
    assert post.url == url


def test_mobile_detail_rejects_same_slug_sibling_owner_conflict():
    payload = _fixture("post_detail.json")["envelope"]
    payload = json.loads(json.dumps(payload))
    payload["response"]["posts"][0]["blogPageUrl"] = (
        "https://bob.lofter.com/post/1a_2b"
    )

    with pytest.raises(SourceSchemaError, match="post.url"):
        parse_mobile_post_detail(payload)


def test_merge_rejects_same_slug_cross_blog_identity():
    base = Post(
        post_id="1a_2b",
        title="List",
        summary="",
        author_username="alice",
        url="https://alice.lofter.com/post/1a_2b",
        completeness=frozenset({"title", "author_username", "url"}),
    )
    detail = Post(
        post_id="1a_2b",
        title="Detail",
        summary="",
        author_username="bob",
        url="https://bob.lofter.com/post/1a_2b",
    )

    with pytest.raises(SourceSchemaError, match="post.owner"):
        merge_post_fields(base, detail)


def test_embedded_blog_name_does_not_complete_author_nickname():
    item = _embedded_item(
        blogPageUrl="https://synthetic.lofter.com/post/1a_2b",
        blogInfo={"blogId": 26, "blogName": "synthetic"},
    )

    post = parse_embedded_post(
        _embedded_html(item),
        "https://synthetic.lofter.com/post/1a_2b",
    )

    assert post.author == ""
    assert "author" not in post.completeness
    assert post.author_username == "synthetic"


@pytest.mark.parametrize("nickname", [None, 123])
def test_dwr_blog_name_does_not_complete_author_nickname(nickname):
    post = _map_post({
        "post": {
            "blogPageUrl": "https://synthetic.lofter.com/post/1a_2b",
            "title": "Demo",
            "blogInfo": {
                "blogNickName": nickname,
                "blogName": "synthetic",
            },
        }
    })

    assert post is not None
    assert post.author == ""
    assert "author" not in post.completeness
    assert post.author_username == "synthetic"


@pytest.mark.asyncio
async def test_embedded_publish_time_satisfies_subscription_contract():
    item = _embedded_item(publishTime=1710000000000)
    post = parse_embedded_post(_embedded_html(item), DETAIL_URL)
    source = AsyncMock()

    result = await ensure_subscription_posts([post], source)

    assert result[0].publish_time == "2024-03-09 16:00:00"
    assert "publish_time" in post.completeness
    source.get_post.assert_not_awaited()


def test_dwr_known_empty_summary_conflicts_with_nonempty_alias():
    with pytest.raises(SourceSchemaError) as exc_info:
        _map_post({
            "post": {
                "blogPageUrl": "https://demo.lofter.com/post/1a_2b",
                "title": "Demo",
                "dirContent": "",
                "content": "<p>FALLBACK-CONTENT</p>",
            }
        })

    assert exc_info.value.location == "post.evidence"


def test_embedded_known_empty_alias_conflicts_with_nonempty_alias():
    item = _embedded_item(
        dirContent="",
        digest="FALLBACK-SUMMARY",
        content="",
        postContent="FALLBACK-CONTENT",
        images=[],
        photoLinks=["https://example.invalid/a.jpg"],
    )

    with pytest.raises(SourceSchemaError) as exc_info:
        parse_embedded_post(_embedded_html(item), DETAIL_URL)

    assert exc_info.value.location == "post.evidence"


@pytest.mark.asyncio
async def test_html_unmatched_content_and_images_remain_unknown():
    html = """
    <link rel="canonical" href="https://demo.lofter.com/post/1a_2b">
    <title>Demo</title>
    <article>ACTUAL-BODY</article>
    <img src="https://example.invalid/a.jpg">
    """

    post = await parse_post_page(html, DETAIL_URL)

    assert post.content == ""
    assert post.images == []
    assert {"content", "images"}.isdisjoint(post.completeness)


@pytest.mark.asyncio
async def test_html_rejects_same_slug_cross_blog_evidence():
    html = """
    <meta property="og:url" content="https://bob.lofter.com/post/1a_2b">
    <link rel="canonical" href="https://alice.lofter.com/post/1a_2b">
    <title>Demo</title>
    """

    with pytest.raises(SourceSchemaError, match="post.evidence"):
        await parse_post_page(
            html, "https://alice.lofter.com/post/1a_2b"
        )


@pytest.mark.parametrize("timestamp", [1, "1"])
def test_dwr_tiny_positive_publish_time_remains_unknown(timestamp):
    post = _map_post({
        "post": {
            "blogPageUrl": "https://demo.lofter.com/post/1a_2b",
            "title": "Demo",
            "publishTime": timestamp,
        }
    })

    assert post is not None
    assert post.publish_time == ""
    assert "publish_time" not in post.completeness
