from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from core.errors import SourceSchemaError
from core.filter import FilterRule
from core.parser import Post, parse_embedded_post
from core.scheduler import _fetch_all_tag_targets
from core.source_scan import SourcePage
from core.tag_count import count_posts

POST_URL = "https://demo.lofter.com/post/1a_2b"


def _embedded_html(items: list[dict]) -> str:
    payload = json.dumps({"state": {"posts": items}})
    return f"<script>window.__initialize_data__ = {payload};</script>"


def _embedded_item(**changes) -> dict:
    item = {
        "postId": 43,
        "blogId": 26,
        "blogPageUrl": POST_URL,
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


def _page(items: list[Post]) -> SourcePage:
    return SourcePage(
        items=items,
        source="mobile_tag",
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=len(items),
        dropped_count=0,
        complete=True,
    )


def _post(*, author: str = "Demo", tags: list[str] | None = None) -> Post:
    fields = {
        "title", "author", "author_username", "url", "publish_time",
    }
    if tags is not None:
        fields.add("tags")
    return Post(
        post_id="1a_2b",
        title="Demo",
        summary="",
        author=author,
        author_username="demo",
        url=POST_URL,
        tags=tags or [],
        publish_time="2026-01-01 00:00:00",
        source="mobile_tag",
        completeness=frozenset(fields),
    )


@pytest.mark.parametrize(
    "sibling_blog,sibling_time",
    [
        ({"blogId": 26, "blogNickName": "Other", "blogName": "demo"}, 1710000000000),
        ({"blogId": 26, "blogNickName": "Demo", "blogName": "demo"}, 1710000001000),
    ],
)
def test_embedded_low_score_sibling_evidence_is_validated(
    sibling_blog, sibling_time,
):
    rich = _embedded_item(publishTime=1710000000000)
    evidence_only = {
        "postId": 43,
        "blogId": 26,
        "blogPageUrl": POST_URL,
        "publishTime": sibling_time,
        "blogInfo": sibling_blog,
    }

    with pytest.raises(SourceSchemaError) as exc_info:
        parse_embedded_post(_embedded_html([rich, evidence_only]), POST_URL)

    assert exc_info.value.location == "post.evidence"


def test_embedded_prefers_parsed_completeness_over_present_none_keys():
    complete = _embedded_item(content="ACTUAL BODY")
    shell = _embedded_item(
        dirContent=None,
        content=None,
        postContent=None,
        description=None,
        digest=None,
        firstImageUrl=None,
        photoLinks=None,
        images=None,
        tag=None,
        tags=None,
    )

    post = parse_embedded_post(_embedded_html([complete, shell]), POST_URL)

    assert post.content == "ACTUAL BODY"
    assert "content" in post.completeness


@pytest.mark.parametrize(
    ("first_changes", "second_changes"),
    [
        ({"title": "First"}, {"title": "Second"}),
        ({"dirContent": "First"}, {"dirContent": "Second"}),
        ({"content": "First"}, {"content": "Second"}),
        ({"images": ["https://example.invalid/a.jpg"]},
         {"images": ["https://example.invalid/b.jpg"]}),
    ],
)
def test_embedded_rejects_known_display_field_conflicts(
    first_changes, second_changes,
):
    first = _embedded_item(**first_changes)
    second = _embedded_item(**second_changes)

    with pytest.raises(SourceSchemaError) as exc_info:
        parse_embedded_post(_embedded_html([first, second]), POST_URL)

    assert exc_info.value.location == "post.evidence"


@pytest.mark.asyncio
async def test_count_scans_casefold_alias_seen_only_in_negative_branch():
    source = AsyncMock()
    calls: list[str] = []

    async def list_tag(tag, cursor, limit, sort):
        calls.append(tag)
        if tag == "STRASSE":
            return _page([_post(tags=["STRASSE"])])
        return _page([])

    source.list_tag.side_effect = list_tag
    result = await count_posts("(-STRASSE&X)|Straße", source)

    assert calls == ["STRASSE", "Straße", "X"]
    assert result.status == "success"
    assert result.count == 1


@pytest.mark.asyncio
async def test_scheduler_rejects_duplicate_author_evidence_before_dedup():
    source = AsyncMock()
    source.list_tag.return_value = _page([
        _post(author="Allowed", tags=["A"]),
        _post(author="Blocked", tags=["A"]),
    ])
    rule = FilterRule(search_tags=["A"])

    with pytest.raises(SourceSchemaError) as exc_info:
        await _fetch_all_tag_targets(rule, source)

    assert exc_info.value.location == "post.evidence"
