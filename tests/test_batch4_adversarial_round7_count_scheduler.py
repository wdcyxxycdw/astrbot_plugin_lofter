from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from core.errors import SourceSchemaError
from core.filter import FilterRule
from core.parser import Post
from core.scheduler import _fetch_tag_candidates
from core.source_scan import SourcePage
from core.tag_count import count_posts


def _post(
    post_id: str,
    *,
    owner: str = "demo",
    tags: list[str] | None = None,
    publish_time: str | None = "2026-01-01 00:00:00",
) -> Post:
    host = f"{owner}.lofter.com" if owner else "lofter.com"
    known = {"title", "url", "images"}
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
        images=["https://img.example/a.jpg"],
        author_username=owner,
        tags=tags or [],
        url=f"https://{host}/post/{post_id}",
        publish_time=publish_time or "",
        source="dwr",
        completeness=frozenset(known),
    )


def _page(
    items: list[Post],
    *,
    cursor: str | None = None,
    exhausted: bool = True,
    sort: str = "new",
    complete: bool = True,
    evidence: tuple[Post, ...] = (),
) -> SourcePage:
    return SourcePage(
        items=items,
        source="dwr",
        next_cursor=cursor,
        exhausted=exhausted,
        sort=sort,
        mapped_count=len(items),
        dropped_count=0 if complete else 1,
        complete=complete,
        evidence_items=evidence,
    )


@pytest.mark.asyncio
async def test_count_registers_whole_page_before_first_detail_timeout():
    unknown = _post(
        "1a_2b", tags=None, publish_time="2026-01-02 00:00:00"
    )
    known = _post("1a_2c", tags=["A"])
    source = AsyncMock()
    source.list_tag.return_value = _page([unknown, known])

    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    source.get_post.side_effect = hang
    result = await count_posts("A", source, _deadline=0.02)

    assert result.status == "partial"
    assert result.count == 1
    assert result.candidates == 2
    assert result.scanned_pages == {"A": 1}


@pytest.mark.asyncio
async def test_count_same_id_complete_occurrence_survives_earlier_timeout():
    unknown = _post(
        "1a_2b", tags=None, publish_time="2026-01-02 00:00:00"
    )
    known = _post(
        "1a_2b", tags=["A"], publish_time="2026-01-02 00:00:00"
    )
    source = AsyncMock()
    source.list_tag.return_value = _page([unknown, known])

    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    source.get_post.side_effect = hang
    result = await count_posts("A", source, _deadline=0.02)

    assert result.status == "partial"
    assert result.count == 1
    assert result.candidates == 1
    assert result.scanned_pages == {"A": 1}


@pytest.mark.asyncio
async def test_count_registers_evidence_and_items_before_evidence_timeout():
    evidence = _post(
        "1a_2b", tags=None, publish_time="2026-01-02 00:00:00"
    )
    item = _post("1a_2c", tags=["A"])
    source = AsyncMock()
    source.list_tag.return_value = _page([item], evidence=(evidence,))

    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    source.get_post.side_effect = hang
    result = await count_posts("A", source, _deadline=0.02)

    assert result.status == "partial"
    assert result.count == 1
    assert result.candidates == 1
    assert result.scanned_pages == {"A": 1}


@pytest.mark.asyncio
async def test_count_duplicate_page_complete_tags_restore_lower_bound():
    unknown = _post(
        "1a_2b", tags=None, publish_time="2026-01-02 00:00:00"
    )
    known = _post(
        "1a_2b", tags=["A"], publish_time="2026-01-02 00:00:00"
    )
    source = AsyncMock()
    source.list_tag.side_effect = [
        _page([unknown], cursor="next", exhausted=False),
        _page([known]),
    ]
    source.get_post.side_effect = SourceSchemaError("tags")

    result = await count_posts("A", source)

    assert result.status == "partial"
    assert result.count == 1
    assert result.candidates == 1
    assert result.scanned_pages == {"A": 2}
    assert any(
        "疑似分页未生效或接口返回重复页" in warning
        for warning in result.warnings
    )


@pytest.mark.asyncio
async def test_invalid_page_tags_still_participate_in_cross_cover_conflict():
    invalid = _post("1a_2b", tags=["A"])
    valid = _post("1a_2b", tags=["A", "B"])
    source = AsyncMock()

    async def list_tag(tag, cursor, limit, sort):
        if tag == "A":
            return _page([invalid], sort="old")
        return _page([valid])

    source.list_tag.side_effect = list_tag
    result = await count_posts("A&B", source)

    assert result.status == "partial"
    assert result.count == 0
    assert result.candidates == 1
    assert result.scanned_pages == {"B": 1}
    assert "重复作品字段冲突" in result.warnings


@pytest.mark.asyncio
async def test_partial_covers_union_reliable_matched_lower_bound():
    from_a = _post("1a_2b", tags=["A", "B"])
    from_b = _post("1a_2c", tags=["A", "B"])
    source = AsyncMock()

    async def list_tag(tag, cursor, limit, sort):
        return _page([from_a if tag == "A" else from_b], complete=False)

    source.list_tag.side_effect = list_tag
    result = await count_posts("A&B", source)

    assert result.status == "partial"
    assert result.count == 2
    assert result.candidates == 1
    assert sum(result.scanned_pages.values()) == 1


@pytest.mark.asyncio
async def test_same_scan_duplicate_publish_time_conflict_is_not_exact():
    first = _post(
        "1a_2b", tags=["A"], publish_time="2026-01-03 00:00:00"
    )
    duplicate = _post(
        "1a_2b", tags=["A"], publish_time="2026-01-02 00:00:00"
    )
    second = _post(
        "1a_2c", tags=["A"], publish_time="2026-01-01 00:00:00"
    )
    source = AsyncMock()
    source.list_tag.side_effect = [
        _page([first], cursor="next", exhausted=False),
        _page([duplicate, second]),
    ]

    result = await count_posts("A", source)

    assert result.status == "partial"
    assert result.count == 1
    assert result.candidates == 2
    assert "重复作品字段冲突" in result.warnings


@pytest.mark.asyncio
async def test_cross_cover_publish_time_conflict_is_not_exact():
    first = _post(
        "1a_2b", tags=["A", "B"], publish_time="2026-01-02 00:00:00"
    )
    second = _post(
        "1a_2b", tags=["A", "B"], publish_time="2026-01-01 00:00:00"
    )
    source = AsyncMock()

    async def list_tag(tag, cursor, limit, sort):
        return _page([first if tag == "A" else second])

    source.list_tag.side_effect = list_tag
    result = await count_posts("A&B", source)

    assert result.status == "partial"
    assert result.count == 0
    assert result.candidates == 1
    assert "重复作品字段冲突" in result.warnings


@pytest.mark.asyncio
async def test_unknown_and_known_publish_time_do_not_conflict():
    unknown = _post("1a_2b", tags=["A", "B"], publish_time=None)
    known = _post("1a_2b", tags=["A", "B"])
    source = AsyncMock()

    async def list_tag(tag, cursor, limit, sort):
        return _page([unknown if tag == "A" else known])

    source.list_tag.side_effect = list_tag
    result = await count_posts("A&B", source)

    assert result.status == "success"
    assert result.count == 1


@pytest.mark.asyncio
async def test_scheduler_evidence_owner_conflict_precedes_db_eligibility():
    evidence = _post("1a_2b", owner="alice", tags=["A"])
    ownerless = _post("1a_2b", owner="", tags=["A"])
    detail = _post("1a_2b", owner="bob", tags=["A"])
    source = AsyncMock()
    source.list_tag.return_value = _page(
        [ownerless], evidence=(evidence,)
    )
    source.get_post.return_value = detail
    db = AsyncMock()
    db.apply_tag_legacy_rules.return_value = ({"A": ["1a_2b"]}, False)

    with pytest.raises(SourceSchemaError) as exc_info:
        await _fetch_tag_candidates(
            "session", FilterRule(["A"], []), source, db
        )

    assert exc_info.value.location == "post.owner"
    db.apply_tag_legacy_rules.assert_not_awaited()
