import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.dwr_parser import _map_post
from core.errors import PostEvidenceError, SourcePartialError, SourceSchemaError
from core.mobile_parser import (
    parse_mobile_post_detail,
    parse_mobile_tag_page,
)
from core.parser import Post, parse_blog_posts, parse_embedded_post
from core.post_consumers import (
    apply_filter_with_fields,
    ensure_subscription_posts,
    filter_blocked_with_fields,
)
from core.post_identity import (
    canonical_post_id,
    canonical_post_url,
    decimal_post_id,
    mobile_decimal_ids,
    post_id_from_url,
)
from core.post_fields import PostEvidenceLedger, merge_post_fields
from core.source_scan import SourcePage, collect_pages
from core.filter import FilterRule
from core.author_block import AuthorBlock

FIXTURES = Path(__file__).parent / "fixtures" / "lofter"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_identity_helpers_share_hex_decimal_contract():
    assert canonical_post_id("001A_00002B") == "1a_2b"
    assert decimal_post_id("26", 43) == "1a_2b"
    assert mobile_decimal_ids("001A_00002B") == ("26", "43")
    assert post_id_from_url(
        "https://demo.lofter.com/post/001A_00002B"
    ) == "1a_2b"
    assert canonical_post_url(
        "https://demo.lofter.com/post/001A_00002B"
    ) == "https://demo.lofter.com/post/1a_2b"


@pytest.mark.parametrize(
    "url",
    [
        "http://demo.lofter.com/post/1a_2b",
        "https://demo.lofter.com/post/1a_2b?x=1",
        "https://demo.lofter.com/post/1a_2b#fragment",
        "https://demo.lofter.com/post/1a_2b/extra",
        "https://attacker.example/post/1a_2b",
    ],
)
def test_canonical_post_url_rejects_noncanonical_boundaries(url):
    with pytest.raises(ValueError):
        canonical_post_url(url)
    assert post_id_from_url(url) == ""


def test_canonical_post_url_normalizes_host_port_slash_and_hex():
    assert canonical_post_url(
        "https://Demo.LOFTER.com:443/post/001A_00002B/"
    ) == "https://demo.lofter.com/post/1a_2b"


def test_identity_helpers_preserve_opaque_slug():
    assert canonical_post_id("opaque-Legacy_id") == "opaque-Legacy_id"
    assert mobile_decimal_ids("opaque-Legacy_id") is None
    assert post_id_from_url(
        "https://demo.lofter.com/post/opaque-Legacy_id"
    ) == "opaque-Legacy_id"


def test_mobile_detail_and_tag_share_canonical_identity():
    detail = parse_mobile_post_detail(_fixture("post_detail.json")["envelope"])
    tag = parse_mobile_tag_page(_fixture("tag_posts.json")["envelope"])

    assert detail.post_id == tag.items[0].post_id == "1a_2b"
    assert detail.url.endswith("/post/1a_2b")
    assert tag.items[0].url.endswith("/post/1a_2b")


@pytest.mark.asyncio
async def test_dwr_html_and_embedded_share_canonical_identity():
    url = "https://demo.lofter.com/post/001A_00002B"
    dwr = _map_post({
        "post": {
            "blogPageUrl": url,
            "title": "Demo",
            "publishTime": 1710000000000,
        }
    })
    html = '<link rel="canonical" href="%s"><a href="%s">Demo</a>' % (
        "https://demo.lofter.com/",
        url,
    )
    embedded = {
        "state": {
            "detail": {
                "post": {
                    "postId": 43,
                    "blogId": 26,
                    "blogPageUrl": url,
                    "title": "Demo",
                    "content": "Demo",
                    "blogInfo": {
                        "blogId": 26,
                        "blogNickName": "Demo",
                        "blogName": "demo",
                    },
                }
            }
        }
    }
    script = (
        "<script>window.__initialize_data__ = "
        + json.dumps(embedded)
        + ";</script>"
    )

    assert dwr is not None and dwr.post_id == "1a_2b"
    posts = await parse_blog_posts(html)
    assert [post.post_id for post in posts] == ["1a_2b"]
    assert parse_embedded_post(script, url).post_id == "1a_2b"


def test_mobile_tag_rejects_local_id_without_blog_or_canonical_url():
    payload = _fixture("tag_posts.json")["envelope"]
    payload = json.loads(json.dumps(payload))
    view = payload["data"]["list"][0]["postData"]["postView"]
    view.pop("blogId")
    view.pop("permalink")
    view["id"] = 43

    with pytest.raises(SourcePartialError):
        parse_mobile_tag_page(payload)
    with pytest.raises(ValueError):
        decimal_post_id(None, view["id"])


@pytest.mark.asyncio
async def test_source_publish_times_satisfy_subscription_contract():
    detail = parse_mobile_post_detail(_fixture("post_detail.json")["envelope"])
    tag = parse_mobile_tag_page(
        _fixture("tag_posts.json")["envelope"]
    ).items[0]
    dwr = _map_post({
        "post": {
            "blogPageUrl": "https://demo.lofter.com/post/1a_2b",
            "title": "Demo",
            "publishTime": 1710000000000,
        }
    })
    assert dwr is not None
    source = AsyncMock()

    posts = await ensure_subscription_posts([detail, tag, dwr], source)

    assert all(len(post.publish_time) == 19 for post in posts)
    assert {post.publish_time for post in posts} == {
        "2024-03-09 16:00:00"
    }
    source.get_post.assert_not_awaited()


def test_merge_summary_preserves_known_list_value_for_unknown_or_empty_detail():
    base = _complete_post(
        summary="列表摘要",
        completeness=frozenset({"summary", "url", "publish_time"}),
        provenance={"summary": "dwr", "url": "dwr", "publish_time": "dwr"},
    )
    unknown_detail = _complete_post(
        summary="poisoned",
        completeness=frozenset({"content", "url", "publish_time"}),
        provenance={"content": "html", "url": "html", "publish_time": "html"},
    )
    empty_detail = _complete_post(
        summary="",
        completeness=frozenset({"summary", "url", "publish_time"}),
        provenance={"summary": "mobile_detail", "url": "mobile_detail", "publish_time": "mobile_detail"},
    )

    unknown_merged = merge_post_fields(base, unknown_detail)
    empty_merged = merge_post_fields(base, empty_detail)

    assert unknown_merged.summary == "列表摘要"
    assert unknown_merged.provenance["summary"] == "dwr"
    assert empty_merged.summary == "列表摘要"
    assert empty_merged.provenance["summary"] == "dwr"


@pytest.mark.parametrize(
    "detail_source", ["mobile_detail", "embedded_json", "html_post"]
)
def test_merge_accepts_distinct_nonempty_dwr_and_detail_summaries(detail_source):
    base = _complete_post(
        summary="列表摘要",
        source="dwr",
        completeness=frozenset({"summary", "url", "publish_time"}),
        provenance={"summary": "dwr", "url": "dwr", "publish_time": "dwr"},
    )
    detail = _complete_post(
        summary="详情摘要",
        source=detail_source,
        completeness=frozenset({"summary", "url", "publish_time"}),
        provenance={
            "summary": detail_source,
            "url": detail_source,
            "publish_time": detail_source,
        },
    )

    merged = merge_post_fields(base, detail)

    assert merged.summary == "详情摘要"
    assert merged.provenance["summary"] == detail_source


def test_summary_ledger_keeps_each_role_through_copy_and_merge():
    dwr = _complete_post(
        summary="列表摘要",
        source="dwr",
        completeness=frozenset({"summary"}),
        provenance={"summary": "dwr"},
    )
    detail = _complete_post(
        summary="详情摘要",
        source="mobile_detail",
        completeness=frozenset({"summary"}),
        provenance={"summary": "mobile_detail"},
    )
    conflicting_dwr = _complete_post(
        summary="另一份列表摘要",
        source="dwr",
        completeness=frozenset({"summary"}),
        provenance={"summary": "dwr"},
    )
    first = PostEvidenceLedger()
    first.observe(dwr)
    first.observe(detail)
    copied = first.copy()
    incoming = PostEvidenceLedger()
    incoming.observe(conflicting_dwr)

    with pytest.raises(PostEvidenceError) as copy_info:
        copied.observe(conflicting_dwr)
    with pytest.raises(PostEvidenceError) as merge_info:
        first.merge(incoming)

    assert copy_info.value.diagnostic == "field_conflict:summary:post_ledger"
    assert merge_info.value.diagnostic == "field_conflict:summary:post_ledger"


def test_summary_role_exception_does_not_relax_strict_or_other_fields():
    dwr = _complete_post(
        title="列表标题",
        summary="列表摘要",
        source="dwr",
        completeness=frozenset({"title", "summary"}),
        provenance={"title": "dwr", "summary": "dwr"},
    )
    strict_summary = _complete_post(
        title="列表标题",
        summary="未知摘要",
        source="test",
        completeness=frozenset({"title", "summary"}),
        provenance={"title": "test", "summary": "test"},
    )
    conflicting_title = _complete_post(
        title="详情标题",
        summary="详情摘要",
        source="mobile_detail",
        completeness=frozenset({"title", "summary"}),
        provenance={"title": "mobile_detail", "summary": "mobile_detail"},
    )

    with pytest.raises(PostEvidenceError) as summary_info:
        merge_post_fields(dwr, strict_summary)
    with pytest.raises(PostEvidenceError) as title_info:
        merge_post_fields(dwr, conflicting_title)

    assert summary_info.value.diagnostic == "field_conflict:summary:post_ledger"
    assert title_info.value.diagnostic == "field_conflict:title:post_ledger"


def test_post_and_page_propagate_completeness_and_provenance():
    first = Post(
        post_id="1a_2b",
        title="Demo",
        summary="",
        url="https://demo.lofter.com/post/1a_2b",
        source="mobile_tag",
        completeness=frozenset({"title", "url"}),
        provenance={"title": "mobile_tag", "url": "canonical_url"},
    )
    second = Post(
        post_id="1a_2c",
        title="Next",
        summary="",
        url="https://demo.lofter.com/post/1a_2c",
        source="dwr",
        completeness=frozenset({"title", "url", "tags"}),
        provenance={"title": "dwr", "url": "canonical_url", "tags": "dwr"},
    )
    page = SourcePage(
        items=[first, second],
        source="mixed-test",
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=2,
        dropped_count=0,
        complete=True,
    )

    assert page.field_completeness == frozenset({"title", "url"})
    assert page.provenance == {"url": "canonical_url"}


@pytest.mark.asyncio
async def test_collect_pages_propagates_restart_metadata():
    calls = 0

    async def fetch(cursor):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _scan_page("1a_2b", "mobile_tag", "next")
        if calls == 2:
            return _scan_page(
                "1a_2b", "dwr", "fallback", restarted=True
            )
        return _scan_page("1a_2d", "dwr", None, exhausted=True)

    result = await collect_pages(fetch)

    assert result.restarted is True
    assert [post.post_id for post in result.items] == ["1a_2b", "1a_2d"]


@pytest.mark.asyncio
async def test_unknown_exclude_and_author_fields_are_enriched():
    partial = Post(
        post_id="1a_2b",
        title="Demo",
        summary="",
        url="https://demo.lofter.com/post/1a_2b",
        source="mobile_tag",
        completeness=frozenset({"title", "url"}),
    )
    detail = _complete_post(tags=["A"], author="Allowed")
    source = AsyncMock()
    source.get_post.return_value = detail

    filtered = await apply_filter_with_fields(
        [partial],
        FilterRule(search_tags=["A"], exclude_tags=["B"]),
        source,
    )
    visible, blocked = await filter_blocked_with_fields(
        filtered,
        [AuthorBlock("session", "name", "blocked", "Blocked")],
        source,
    )

    assert [post.post_id for post in visible] == ["1a_2b"]
    assert blocked == []
    source.get_post.assert_awaited_once_with(partial.url)


@pytest.mark.asyncio
async def test_subscription_enriches_required_location_and_time():
    partial = Post(
        post_id="1a_2b",
        title="Demo",
        summary="",
        url="https://demo.lofter.com/post/1a_2b",
        completeness=frozenset({"url"}),
    )
    detail = _complete_post()
    source = AsyncMock()
    source.get_post.return_value = detail

    result = await ensure_subscription_posts([partial], source)

    assert result[0].publish_time == "2026-07-29 05:00:00"
    source.get_post.assert_awaited_once_with(partial.url)


@pytest.mark.asyncio
async def test_subscription_reports_unknown_images_at_image_field():
    post = Post(
        post_id="1a_2b",
        title="Demo",
        summary="",
        images=[],
        url="https://demo.lofter.com/post/1a_2b",
        publish_time="2026-07-29 05:00:00",
        completeness=frozenset({"url", "publish_time"}),
    )
    class Source:
        async def get_post(self, url):
            assert url == post.url
            return post

    with pytest.raises(SourceSchemaError) as exc_info:
        await ensure_subscription_posts([post], Source(), {"images"})

    assert exc_info.value.location == "post.images"


@pytest.mark.asyncio
async def test_subscription_rejects_unknown_or_mismatched_identity_time():
    unknown_time = Post(
        post_id="1a_2b",
        title="Demo",
        summary="",
        url="https://demo.lofter.com/post/1a_2b",
        completeness=frozenset({"url"}),
    )
    source = AsyncMock()
    source.get_post.return_value = Post(
        post_id="1a_2b",
        title="Demo",
        summary="",
        url="https://demo.lofter.com/post/1a_2b",
        completeness=frozenset({"url"}),
    )
    mismatched = _complete_post(
        url="https://demo.lofter.com/post/1a_2c"
    )

    with pytest.raises(SourceSchemaError, match="publishTime"):
        await ensure_subscription_posts([unknown_time], source)
    with pytest.raises(SourceSchemaError, match="post_id"):
        await ensure_subscription_posts([mismatched], source)


def _complete_post(**changes) -> Post:
    values = {
        "post_id": "1a_2b",
        "title": "Demo",
        "summary": "",
        "url": "https://demo.lofter.com/post/1a_2b",
        "publish_time": "2026-07-29 05:00:00",
        "tags": [],
        "author": "Demo",
        "author_username": "demo",
        "source": "mobile_detail",
    }
    values.update(changes)
    return Post(**values)


def _scan_page(
    post_id: str,
    source: str,
    cursor: str | None,
    *,
    exhausted: bool = False,
    restarted: bool = False,
) -> SourcePage:
    post = _complete_post(
        post_id=post_id,
        url=f"https://demo.lofter.com/post/{post_id}",
    )
    return SourcePage(
        items=[post],
        source=source,
        next_cursor=cursor,
        exhausted=exhausted,
        sort="new",
        mapped_count=1,
        dropped_count=0,
        complete=True,
        restarted=restarted,
    )
