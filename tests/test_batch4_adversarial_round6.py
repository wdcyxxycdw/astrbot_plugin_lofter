import asyncio
from unittest.mock import AsyncMock

import pytest

from core.dwr_parser import parse_dwr_response_result
from core.errors import SourcePartialError, SourceSchemaError
from core.mobile_parser import parse_mobile_tag_page
from core.parser import Post
from core.source_scan import SourcePage, collect_pages
from core.tag_count import count_posts


def _tag_item(
    post_id: str = "1a_2b",
    *,
    view_blog_id: int = 26,
    count_blog_id: int = 26,
    photo_count: int = 0,
) -> dict:
    return {
        "postData": {
            "postView": {
                "blogId": view_blog_id,
                "permalink": f"https://demo.lofter.com/post/{post_id}",
                "photoCount": photo_count,
                "title": "Demo",
                "publishTime": 1710000000000,
            },
            "postCount": {"blogId": count_blog_id},
        }
    }


def _tag_payload(items: list[object]) -> dict:
    return {
        "code": 0,
        "msg": "demo",
        "data": {"list": items, "offset": -1},
    }


@pytest.mark.asyncio
async def test_dwr_rejects_conflicting_post_id_alias():
    body = """
    dwr.engine._remoteHandleCallback("0", "0", [{post: {
        blogPageUrl: "https://demo.lofter.com/post/1a_2b",
        blogId: 26,
        id: 43,
        postId: 44
    }}]);
    """

    with pytest.raises(SourceSchemaError) as exc_info:
        await parse_dwr_response_result(body)

    assert exc_info.value.location == "dwr.post.id"


@pytest.mark.asyncio
async def test_dwr_mixed_page_propagates_identity_error():
    body = """
    dwr.engine._remoteHandleCallback("0", "0", [
        {post: {
            blogPageUrl: "https://demo.lofter.com/post/1a_2b",
            blogInfo: {blogName: "demo"}
        }},
        {post: {
            blogPageUrl: "https://demo.lofter.com/post/1a_2b",
            blogInfo: {blogName: "other"}
        }}
    ]);
    """

    with pytest.raises(SourceSchemaError) as exc_info:
        await parse_dwr_response_result(body)

    assert exc_info.value.location == "dwr.post.id"


@pytest.mark.asyncio
async def test_dwr_still_drops_nonidentity_schema_error():
    body = """
    dwr.engine._remoteHandleCallback("0", "0", [
        {post: {blogPageUrl: "https://demo.lofter.com/post/1a_2b"}},
        {post: {
            blogPageUrl: "https://demo.lofter.com/post/1a_2c",
            dirContent: {content: 123}
        }}
    ]);
    """

    result = await parse_dwr_response_result(body)

    assert result.mapped_count == 1
    assert result.dropped_count == 1
    assert result.complete is False


@pytest.mark.parametrize(
    ("item", "location"),
    [
        (_tag_item(count_blog_id=27), "postData.postCount.blogId"),
        (
            _tag_item(view_blog_id=27, count_blog_id=27),
            "postData.postView.blogId",
        ),
    ],
)
def test_mobile_mixed_page_propagates_identity_error(item, location):
    with pytest.raises(SourceSchemaError) as exc_info:
        parse_mobile_tag_page(_tag_payload([_tag_item(), item]))

    assert exc_info.value.location == location


def test_mobile_still_drops_nonidentity_schema_error():
    page = parse_mobile_tag_page(
        _tag_payload([_tag_item(), _tag_item(post_id="1a_2c", photo_count=-1)])
    )

    assert page.mapped_count == 1
    assert page.dropped_count == 1
    assert page.complete is False
    assert [post.post_id for post in page.evidence_items] == ["1a_2c"]
    assert page.evidence_items[0].completeness == frozenset({
        "author_username", "url",
    })


def test_mobile_zero_mapped_partial_retains_identity_evidence():
    with pytest.raises(SourcePartialError) as exc_info:
        parse_mobile_tag_page(
            _tag_payload([_tag_item(post_id="1a_2c", photo_count=-1)])
        )

    evidence = getattr(exc_info.value, "evidence_items", ())
    assert [post.post_id for post in evidence] == ["1a_2c"]
    assert evidence[0].completeness == frozenset({"author_username", "url"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    ["postData.postCount.blogId", "postData.postView.blogId"],
)
async def test_source_scan_propagates_mobile_identity_locations(location):
    fetch_page = AsyncMock(side_effect=SourceSchemaError(location))

    with pytest.raises(SourceSchemaError) as exc_info:
        await collect_pages(fetch_page)

    assert exc_info.value.location == location


def _count_post(
    post_id: str,
    *,
    owner: str = "demo",
    tags: list[str] | None = None,
    publish_time: str = "2026-01-01 00:00:00",
) -> Post:
    known = {"title", "url", "publish_time", "author_username"}
    if tags is not None:
        known.add("tags")
    return Post(
        post_id=post_id,
        title="Demo",
        summary="",
        author_username=owner,
        tags=tags or [],
        url=f"https://{owner}.lofter.com/post/{post_id}",
        publish_time=publish_time,
        source="dwr",
        completeness=frozenset(known),
    )


def _count_page(
    items: list[Post],
    *,
    cursor: str | None = None,
    exhausted: bool = True,
    complete: bool = True,
    evidence: tuple[Post, ...] = (),
) -> SourcePage:
    return SourcePage(
        items=items,
        source="dwr",
        next_cursor=cursor,
        exhausted=exhausted,
        sort="new",
        mapped_count=len(items),
        dropped_count=0 if complete else 1,
        complete=complete,
        evidence_items=evidence,
    )


@pytest.mark.asyncio
async def test_count_records_evidence_owner_before_enrichment_failure():
    alice = _count_post("1a_2b", owner="alice")
    bob = _count_post("1a_2b", owner="bob", tags=["A"])
    source = AsyncMock()
    source.list_tag.return_value = _count_page([bob], evidence=(alice,))
    source.get_post.side_effect = SourceSchemaError("response")

    with pytest.raises(SourceSchemaError) as exc_info:
        await count_posts("A", source)

    assert exc_info.value.location == "post.owner"


@pytest.mark.asyncio
async def test_count_evidence_timeout_after_valid_page_is_partial():
    evidence = _count_post("1a_2b")
    fallback = _count_post("1a_2c", tags=["A"])
    source = AsyncMock()
    source.list_tag.return_value = _count_page(
        [fallback], evidence=(evidence,)
    )

    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    source.get_post.side_effect = hang
    result = await count_posts("A", source, _deadline=0.02)

    assert result.status == "partial"
    assert result.count == 1
    assert result.candidates == 1
    assert result.scanned_pages == {"A": 1}


@pytest.mark.asyncio
async def test_complete_cover_cannot_shrink_reliable_matched_floor():
    p1 = _count_post("1a_2b", tags=["A", "B"])
    p2 = _count_post("1a_2c", tags=["A", "B"])
    source = AsyncMock()

    async def list_tag(tag, cursor, limit, sort):
        if tag == "A":
            return _count_page([p1])
        return _count_page([p1, p2], complete=False)

    source.list_tag.side_effect = list_tag
    result = await count_posts("A&B", source)

    assert result.status == "partial"
    assert result.count == 2
    assert result.candidates == 2
    assert result.scanned_pages == {"B": 1}


@pytest.mark.asyncio
async def test_count_evidence_does_not_pollute_fallback_seen_ids():
    p1 = _count_post(
        "1a_2b", tags=["A"], publish_time="2026-01-02 00:00:00"
    )
    p2 = _count_post("1a_2c", tags=["A"])
    source = AsyncMock()
    source.list_tag.side_effect = [
        _count_page([p1], cursor="next", exhausted=False, evidence=(p2,)),
        _count_page([p2]),
    ]

    result = await count_posts("A", source)

    assert result.status == "success"
    assert result.count == 2
    assert result.candidates == 2
    assert not any("重复页" in warning for warning in result.warnings)
