import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.author_block import AuthorBlock
from core.client import DWR_SEARCH_URL
from core.content_source import DefaultContentSource
from core.errors import SourceSchemaError
from core.expression_planner import (
    minimum_cover_alternatives,
    parse_count_expression,
)
from core.filter import FilterRule
from core.mobile_adapter import POST_DETAIL_URL, TAG_POSTS_URL
from core.parser import Post
from core.post_consumers import filter_blocked_with_fields
from core.scheduler import _fetch_all_tag_targets
from core.source_scan import SourcePage
from core.tag_count import count_posts
from tests.test_client import FakeResponse, make_client
from tests.test_command_permissions import _load_main_module


FIXTURES = Path(__file__).parent / "fixtures" / "lofter"


def _page(items, cursor=None, *, exhausted=None):
    return SourcePage(
        items=items,
        source="mobile_tag",
        next_cursor=cursor,
        exhausted=cursor is None if exhausted is None else exhausted,
        sort="new",
        mapped_count=len(items),
        dropped_count=0,
        complete=True,
    )


def _timed_post(post_id: str, publish_time: str) -> Post:
    return Post(
        post_id=post_id,
        title="",
        summary="",
        tags=["A"],
        publish_time=publish_time,
    )


def _fixture_envelope(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["envelope"]


def _json_response(payload: object) -> FakeResponse:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return FakeResponse(body=body)


def _tag_response(*, exhausted: bool) -> FakeResponse:
    payload = _fixture_envelope("tag_posts.json")
    payload["data"]["offset"] = -1 if exhausted else 20
    return _json_response(payload)


def _detail_response(tags: str) -> FakeResponse:
    payload = _fixture_envelope("post_detail.json")
    payload["response"]["posts"][0]["post"]["tag"] = tags
    return _json_response(payload)


def _deep_json_response() -> FakeResponse:
    return FakeResponse(body=b"[" * 10_000 + b"0" + b"]" * 10_000)


async def _count_real_chain(
    expression: str,
    responses: list[FakeResponse],
    **kwargs,
):
    client, session, _ = await make_client(responses)
    source = DefaultContentSource(client)
    try:
        result = await count_posts(expression, source, **kwargs)
    finally:
        await source.close()
    return result, session


@pytest.mark.asyncio
async def test_count_sort_regression_cannot_complete_cover():
    source = AsyncMock()
    source.list_tag.side_effect = [
        _page([_timed_post("p1", "2026-01-09 00:00:00")], "next"),
        _page([_timed_post("p2", "2026-01-10 00:00:00")]),
    ]

    result = await count_posts("A", source, page_size=1)

    assert result.status == "partial"
    assert result.count == 1
    assert result.candidates == 2
    assert result.scanned_pages == {"A": 2}
    assert result.warnings == ["标签「A」分页 sort=new 时间倒退"]


@pytest.mark.asyncio
async def test_count_deadline_preserves_completed_cover():
    source = AsyncMock()

    async def list_tag(tag, cursor, limit, sort):
        if tag == "A":
            return _page([])
        await asyncio.Event().wait()

    source.list_tag.side_effect = list_tag
    result = await count_posts(
        "A&B", source, tag_concurrency=2, _deadline=0.05
    )

    assert result.status == "success"
    assert result.count == 0
    assert result.scanned_pages == {"A": 0}
    assert result.warnings == []


@pytest.mark.asyncio
async def test_count_real_chain_json_midscan_preserves_lower_bound():
    result, session = await _count_real_chain(
        "A",
        [
            _tag_response(exhausted=False),
            _detail_response("A"),
            _deep_json_response(),
            FakeResponse(status=404),
        ],
    )

    assert result.status == "partial"
    assert result.count == 1
    assert result.candidates == 1
    assert result.scanned_pages == {"A": 1}
    assert result.warnings == [
        "标签「A」扫描失败：内容源 HTTP 请求失败（HTTP 404）"
    ]
    assert [url for _, url, _ in session.requests] == [
        TAG_POSTS_URL,
        POST_DETAIL_URL,
        TAG_POSTS_URL,
        DWR_SEARCH_URL,
    ]
    assert session.requests[2][2]["data"]["offset"] == "20"
    assert "c0-param7=number:0" in session.requests[3][2]["data"]


@pytest.mark.asyncio
async def test_count_real_chain_first_json_failure_is_failed():
    result, session = await _count_real_chain(
        "A", [_deep_json_response(), FakeResponse(status=404)]
    )

    assert result.status == "failed"
    assert result.count == 0
    assert result.candidates == 0
    assert result.scanned_pages == {"A": 0}
    assert result.warnings == [
        "标签「A」扫描失败：内容源 HTTP 请求失败（HTTP 404）"
    ]
    assert [url for _, url, _ in session.requests] == [
        TAG_POSTS_URL,
        DWR_SEARCH_URL,
    ]
    assert "c0-param7=number:0" in session.requests[1][2]["data"]


@pytest.mark.asyncio
async def test_count_real_chain_completed_cover_survives_json_failure():
    result, session = await _count_real_chain(
        "A&B",
        [
            _tag_response(exhausted=True),
            _detail_response("A,B"),
            _deep_json_response(),
            FakeResponse(status=404),
        ],
        tag_concurrency=1,
    )

    assert result.status == "success"
    assert result.count == 1
    assert result.candidates == 1
    assert result.scanned_pages == {"A": 1}
    assert result.warnings == []
    assert [url for _, url, _ in session.requests] == [
        TAG_POSTS_URL,
        POST_DETAIL_URL,
        TAG_POSTS_URL,
        DWR_SEARCH_URL,
    ]
    tag_forms = [
        kwargs["data"]
        for _, url, kwargs in session.requests
        if url == TAG_POSTS_URL
    ]
    assert [form["tag"] for form in tag_forms] == ["A", "B"]
    assert "c0-param0=string:B" in session.requests[-1][2]["data"]


def test_planner_limits_minimized_or_alternatives_not_raw_product():
    expression = "&".join(f"A{index}" for index in range(17))
    covers = minimum_cover_alternatives(
        parse_count_expression(f"({expression})|({expression})")
    )

    assert set(covers) == {
        frozenset({f"A{index}"}) for index in range(17)
    }


@pytest.mark.asyncio
async def test_scheduler_without_exclude_does_not_enrich_tags():
    partial = Post(
        post_id="1a_2b",
        title="Demo",
        summary="",
        url="https://demo.lofter.com/post/1a_2b",
        publish_time="2026-07-29 05:00:00",
        source="mobile_tag",
        completeness=frozenset({"title", "url", "publish_time"}),
    )
    source = AsyncMock()
    source.get_post.side_effect = SourceSchemaError("tags")

    with patch("core.scheduler.fetch_tag_posts", return_value=[partial]):
        result = await _fetch_all_tag_targets(
            FilterRule(search_tags=["A"]), source
        )

    assert result == {"A": [partial]}
    source.get_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_known_username_block_short_circuits_missing_author():
    partial = Post(
        post_id="1a_2b",
        title="Demo",
        summary="",
        author_username="hit",
        url="https://hit.lofter.com/post/1a_2b",
        completeness=frozenset({"title", "author_username", "url"}),
    )
    blocks = [
        AuthorBlock("session", "name", "hit", "hit"),
        AuthorBlock("session", "username", "hit", "hit"),
    ]
    source = AsyncMock()
    source.get_post.side_effect = SourceSchemaError("author")

    visible, blocked = await filter_blocked_with_fields(
        [partial], blocks, source
    )

    assert visible == []
    assert blocked == [partial]
    source.get_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_known_username_nonmatch_enriches_possible_name_match():
    partial = Post(
        post_id="1a_2b",
        title="Demo",
        summary="",
        author_username="other",
        url="https://other.lofter.com/post/1a_2b",
        completeness=frozenset({"title", "author_username", "url"}),
    )
    detail = Post(
        post_id="1a_2b",
        title="Demo",
        summary="",
        author="Blocked",
        author_username="other",
        url=partial.url,
    )
    source = AsyncMock()
    source.get_post.return_value = detail

    visible, blocked = await filter_blocked_with_fields(
        [partial],
        [AuthorBlock("session", "name", "blocked", "Blocked")],
        source,
    )

    assert visible == []
    assert [post.author for post in blocked] == ["Blocked"]
    source.get_post.assert_awaited_once_with(partial.url)


def test_auto_post_zero_images_limit_falls_back_to_body():
    main = _load_main_module()
    main.Comp = SimpleNamespace(
        Plain=lambda text: SimpleNamespace(text=text),
        Image=SimpleNamespace(fromURL=lambda url: SimpleNamespace(url=url)),
    )
    event = SimpleNamespace(
        unified_msg_origin="FriendMessage:session",
        chain_result=lambda chain: chain,
    )
    post = Post(
        post_id="1a_2b",
        title="Demo",
        summary="",
        content="FULL-CONTENT",
        images=["https://example.invalid/a.jpg"],
        url="https://demo.lofter.com/post/1a_2b",
        completeness=frozenset({"title", "content", "images", "url"}),
    )

    chain = main._auto_post_result(event, post, post.url, 0)

    assert len(chain) == 1
    assert "FULL-CONTENT" in chain[0].text
    assert post.url in chain[0].text
