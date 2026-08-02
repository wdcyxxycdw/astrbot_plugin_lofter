from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.content_source import DefaultContentSource
from core.dwr_parser import _map_post, parse_dwr_response_result
from core.errors import (
    SourceHTTPError,
    SourcePartialError,
    SourceSchemaError,
    attach_source_evidence,
)
from core.mobile_parser import parse_mobile_post_detail
from core.parser import (
    POST_FIELDS,
    Post,
    parse_blog_posts,
    parse_embedded_post,
)
from core.post_fields import validate_post_evidence
from core.scheduler import _poll_session
from core.source_scan import SourcePage, collect_pages
from core.storage import Subscription

TIME = "2026-01-02 00:00:00"
OLDER = "2026-01-01 00:00:00"
FIXTURES = Path(__file__).parent / "fixtures" / "lofter"


def _post(post_id: str, owner: str, publish_time: str = TIME) -> Post:
    source = f"test_{owner}"
    return Post(
        post_id=post_id,
        title="Demo",
        summary="Summary",
        content="Content",
        images=[],
        author="Demo",
        author_username=owner,
        url=f"https://{owner}.lofter.com/post/{post_id}",
        tags=["A"],
        publish_time=publish_time,
        source=source,
        completeness=POST_FIELDS,
        provenance={field: source for field in POST_FIELDS},
    )


def _page(post: Post) -> SourcePage:
    return SourcePage(
        items=[post],
        source="mobile_blog",
        next_cursor="next",
        exhausted=False,
        sort="new",
        mapped_count=1,
        dropped_count=0,
        complete=True,
    )


def _sub(target: str, sub_type: str, sub_id: int) -> Subscription:
    return Subscription(
        id=sub_id,
        session_id="session",
        type=sub_type,
        role="subscribe",
        target=target,
    )


def _typed_error(kind: str, witness: Post) -> Exception:
    error = (
        SourceSchemaError("response")
        if kind == "schema"
        else SourceHTTPError(503)
    )
    attach_source_evidence(error, (witness,))
    return error


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["schema", "http"])
async def test_midscan_typed_error_keeps_current_page_witness(kind: str):
    first = _post("1a_2c", "alice")
    current = _post("1a_2b", "alice", OLDER)

    async def fetch(cursor):
        if cursor is None:
            return _page(first)
        raise _typed_error(kind, current)

    with pytest.raises(SourcePartialError) as exc_info:
        await collect_pages(fetch)

    assert exc_info.value.evidence_items == (first, current)
    assert (exc_info.value.mapped_count, exc_info.value.dropped_count) == (1, 0)


@pytest.mark.asyncio
async def test_midscan_witness_conflict_blocks_scheduler_side_effects():
    tag_post = _post("1a_2b", "bob")
    first_blog = _post("1a_2c", "alice")
    current_witness = _post("1a_2b", "alice", OLDER)
    source = AsyncMock()
    source.list_blog.side_effect = [
        _page(first_blog),
        _typed_error("http", current_witness),
    ]
    blocks = AsyncMock()
    blocks.list_by_session.return_value = []
    db = AsyncMock()
    db.apply_tag_legacy_rules.return_value = ({"A": ["1a_2b"]}, True)
    db.filter_unseen_targets.return_value = ["1a_2b"]
    db.filter_unsent.return_value = ["1a_2b"]
    send = AsyncMock(return_value=True)

    with patch("core.scheduler.fetch_tag_posts", return_value=[tag_post]):
        await _poll_session(
            "session",
            {
                "tag": [_sub("A", "tag", 1)],
                "blog": [_sub("alice", "blog", 2)],
            },
            source,
            db,
            send,
            blocks,
        )

    send.assert_not_awaited()
    db.filter_unseen_targets.assert_not_awaited()
    db.mark_accepted_targets.assert_not_awaited()


@pytest.mark.parametrize("alias", ["postUrl", "permalink"])
def test_dwr_preserves_owner_from_owned_sibling_url(alias: str):
    mapped = _map_post({
        "post": {
            "blogPageUrl": "https://lofter.com/post/1a_2b",
            alias: "https://bob.lofter.com/post/1a_2b",
        }
    })

    assert mapped is not None
    assert mapped.url == "https://bob.lofter.com/post/1a_2b"
    assert mapped.author_username == "bob"
    with pytest.raises(SourceSchemaError) as exc_info:
        validate_post_evidence([mapped, _post("1a_2b", "alice")])
    assert exc_info.value.location == "post.owner"


@pytest.mark.asyncio
async def test_dwr_drop_witness_preserves_sibling_owner():
    body = """
    dwr.engine._remoteHandleCallback("0", "0", [{post: {
        blogPageUrl: "https://lofter.com/post/1a_2b",
        postUrl: "https://bob.lofter.com/post/1a_2b",
        dirContent: {content: 123}
    }}]);
    """

    with pytest.raises(SourceSchemaError) as exc_info:
        await parse_dwr_response_result(body)

    witness = exc_info.value.evidence_items[0]
    assert witness.url == "https://bob.lofter.com/post/1a_2b"
    assert witness.author_username == "bob"


@pytest.mark.parametrize("alias", ["postUrl", "permalink"])
def test_embedded_preserves_owner_from_owned_sibling_url(alias: str):
    item = {
        "blogId": 26,
        "postId": 43,
        "blogPageUrl": "https://lofter.com/post/1a_2b",
        alias: "https://bob.lofter.com/post/1a_2b",
        "title": "Demo",
    }
    payload = json.dumps({"state": {"posts": [item]}})
    html = f"<script>window.__initialize_data__ = {payload};</script>"

    post = parse_embedded_post(html, "https://lofter.com/post/1a_2b")

    assert post.url == "https://bob.lofter.com/post/1a_2b"
    assert post.author_username == "bob"
    with pytest.raises(SourceSchemaError) as exc_info:
        validate_post_evidence([post, _post("1a_2b", "alice")])
    assert exc_info.value.location == "post.owner"


@pytest.mark.asyncio
async def test_blog_shell_with_expected_owner_is_not_valid_empty():
    shell = "<html><body><p>temporary upstream shell</p></body></html>"

    with pytest.raises(SourceSchemaError) as exc_info:
        await parse_blog_posts(shell, expected_owner="demo")

    assert exc_info.value.location == "blog"


@pytest.mark.parametrize("include_primary", [False, True])
def test_dwr_validates_sibling_conflict_without_valid_primary_url(
    include_primary: bool,
):
    post = {
        "postUrl": "https://alice.lofter.com/post/1a_2b",
        "permalink": "https://bob.lofter.com/post/1a_2b",
    }
    if include_primary:
        post["blogPageUrl"] = 123

    with pytest.raises(SourceSchemaError) as exc_info:
        _map_post({"post": post})

    assert exc_info.value.location == "dwr.post.id"


@pytest.mark.asyncio
async def test_get_post_validates_mobile_witness_against_fallback():
    mobile_error = SourceSchemaError("title")
    attach_source_evidence(mobile_error, (_post("1a_2b", "alice"),))
    source = DefaultContentSource(SimpleNamespace())
    source._mobile = SimpleNamespace(
        get_post=AsyncMock(side_effect=mobile_error)
    )
    source._post_fallback = AsyncMock(
        return_value=_post("1a_2b", "bob")
    )

    with pytest.raises(SourceSchemaError) as exc_info:
        await source.get_post("https://lofter.com/post/1a_2b")

    assert exc_info.value.location == "post.owner"


def _embedded_candidate(owner: str, **changes) -> dict:
    item = {
        "blogId": 26,
        "postId": 43,
        "blogPageUrl": f"https://{owner}.lofter.com/post/1a_2b",
        "title": "Demo",
        "blogInfo": {
            "blogId": 26,
            "blogName": owner,
            "blogNickName": "Demo",
        },
    }
    item.update(changes)
    return item


def test_embedded_failure_keeps_successful_candidate_prefix():
    first = _embedded_candidate("alice")
    invalid = _embedded_candidate(
        "alice",
        blogInfo={
            "blogId": 26,
            "blogName": "alice",
            "blogNickName": 123,
        },
    )
    payload = json.dumps({"state": {"posts": [first, invalid]}})
    html = f"<script>window.__initialize_data__ = {payload};</script>"

    with pytest.raises(SourceSchemaError) as exc_info:
        parse_embedded_post(html, "https://lofter.com/post/1a_2b")

    evidence = exc_info.value.evidence_items
    assert [post.author_username for post in evidence] == ["alice"]


@pytest.mark.asyncio
async def test_post_fallback_validates_embedded_witness_against_html():
    embedded_error = SourceSchemaError("embedded.blogInfo")
    attach_source_evidence(embedded_error, (_post("1a_2b", "alice"),))
    source = DefaultContentSource(
        SimpleNamespace(get=AsyncMock(return_value="html"))
    )

    with (
        patch(
            "core.content_source.parse_embedded_post",
            side_effect=embedded_error,
        ),
        patch(
            "core.content_source.parse_post_page",
            new=AsyncMock(return_value=_post("1a_2b", "bob")),
        ),
        pytest.raises(SourceSchemaError) as exc_info,
    ):
        await source._post_fallback(
            "https://lofter.com/post/1a_2b", "1a_2b"
        )

    assert exc_info.value.location == "post.owner"


def test_mobile_owner_aliases_use_casefold_identity():
    fixture = json.loads(
        (FIXTURES / "post_detail.json").read_text(encoding="utf-8")
    )
    payload = fixture["envelope"]
    item = payload["response"]["posts"][0]
    item["blogInfo"]["blogName"] = "Demo"
    item["permalink"] = "https://DEMO.lofter.com/post/1a_2b"

    post = parse_mobile_post_detail(payload)

    assert post.author_username.casefold() == "demo"
