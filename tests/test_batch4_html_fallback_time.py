from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

import core.content_source as source_module
from core.content_source import DefaultContentSource
from core.errors import SourceHTTPError, SourcePartialError, SourceSchemaError
from core.parser import Post


def _post(
    post_id: str,
    *,
    publish_time: str | None = None,
    title: str = "Demo",
    source: str = "html_blog",
) -> Post:
    known = {"author_username", "title", "url"}
    if publish_time is not None:
        known.add("publish_time")
    return Post(
        post_id=post_id,
        title=title,
        summary="",
        author_username="demo",
        url=f"https://demo.lofter.com/post/{post_id}",
        publish_time=publish_time or "",
        source=source,
        completeness=frozenset(known),
        provenance={field: source for field in known},
    )


def _source(monkeypatch, raw: list[Post]) -> DefaultContentSource:
    client = SimpleNamespace(get=AsyncMock(return_value="html"))
    source = DefaultContentSource(client)
    source._mobile = SimpleNamespace(
        list_blog=AsyncMock(side_effect=SourceSchemaError("response"))
    )
    monkeypatch.setattr(
        source_module, "parse_blog_posts", AsyncMock(return_value=raw)
    )
    return source


@pytest.mark.asyncio
async def test_html_blog_fallback_enriches_all_missing_publish_times(monkeypatch):
    raw = [_post("1a_2b"), _post("1a_2c")]
    source = _source(monkeypatch, raw)
    details = [
        _post("1a_2b", publish_time="2026-01-02 00:00:00", source="detail"),
        _post("1a_2c", publish_time="2026-01-01 00:00:00", source="detail"),
    ]
    source.get_post = AsyncMock(side_effect=details)

    result = await source.list_blog("demo", None, 20)

    assert [post.publish_time for post in result.items] == [
        "2026-01-02 00:00:00",
        "2026-01-01 00:00:00",
    ]
    assert result.source == "html_blog"
    assert result.sort == "new"
    assert result.complete is True
    assert source.get_post.await_args_list == [
        call(raw[0].url), call(raw[1].url)
    ]


@pytest.mark.asyncio
async def test_html_blog_single_item_still_requires_real_publish_time(monkeypatch):
    raw = [_post("1a_2b")]
    detail = _post(
        "1a_2b", publish_time="2026-01-02 00:00:00", source="detail"
    )
    source = _source(monkeypatch, raw)
    source.get_post = AsyncMock(return_value=detail)

    result = await source.list_blog("demo", None, 20)

    assert result.items[0].publish_time == "2026-01-02 00:00:00"
    source.get_post.assert_awaited_once_with(raw[0].url)


@pytest.mark.asyncio
async def test_html_blog_fallback_rejects_detail_without_publish_time(monkeypatch):
    raw = [_post("1a_2b")]
    source = _source(monkeypatch, raw)
    source.get_post = AsyncMock(return_value=_post("1a_2b", source="detail"))

    with pytest.raises(SourceSchemaError) as exc_info:
        await source.list_blog("demo", None, 20)

    assert exc_info.value.location == "publishTime"
    assert exc_info.value.evidence_items[:len(raw)] == tuple(raw)


@pytest.mark.asyncio
async def test_html_blog_fallback_rejects_detail_field_conflict(monkeypatch):
    raw = [_post("1a_2b", title="List title")]
    source = _source(monkeypatch, raw)
    source.get_post = AsyncMock(return_value=_post(
        "1a_2b",
        title="Detail title",
        publish_time="2026-01-02 00:00:00",
        source="detail",
    ))

    with pytest.raises(SourceSchemaError) as exc_info:
        await source.list_blog("demo", None, 20)

    assert exc_info.value.location == "post.evidence"
    assert exc_info.value.evidence_items[:len(raw)] == tuple(raw)


@pytest.mark.asyncio
async def test_html_blog_fallback_rejects_publish_time_regression(monkeypatch):
    raw = [_post("1a_2b"), _post("1a_2c")]
    source = _source(monkeypatch, raw)
    source.get_post = AsyncMock(side_effect=[
        _post("1a_2b", publish_time="2026-01-01 00:00:00", source="detail"),
        _post("1a_2c", publish_time="2026-01-02 00:00:00", source="detail"),
    ])

    with pytest.raises(SourcePartialError) as exc_info:
        await source.list_blog("demo", None, 20)

    assert exc_info.value.mapped_count == 2
    assert exc_info.value.evidence_items[:len(raw)] == tuple(raw)


@pytest.mark.asyncio
async def test_html_blog_detail_failure_retains_whole_raw_page(monkeypatch):
    raw = [_post("1a_2b"), _post("1a_2c")]
    source = _source(monkeypatch, raw)
    source.get_post = AsyncMock(side_effect=[
        _post("1a_2b", publish_time="2026-01-02 00:00:00", source="detail"),
        RuntimeError("ordinary detail failure"),
    ])

    with pytest.raises(RuntimeError) as exc_info:
        await source.list_blog("demo", None, 20)

    assert exc_info.value.evidence_items[:len(raw)] == tuple(raw)


@pytest.mark.asyncio
async def test_failed_html_enrichment_stays_validation_only_with_primary(
    monkeypatch,
):
    primary = _post(
        "1a_2b", publish_time="2026-01-02 00:00:00", source="mobile_blog"
    )
    raw = _post("1a_2c")
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
    source.get_post = AsyncMock(side_effect=SourceHTTPError(503))

    result = await source.list_blog("demo", None, 20)

    assert result.items == [primary]
    assert raw in result.evidence_items
    assert raw not in result.items


@pytest.mark.asyncio
async def test_complete_mobile_blog_path_does_not_enrich_details():
    primary = _post("1a_2b", publish_time="2026-01-02 00:00:00")
    source = DefaultContentSource(SimpleNamespace())
    source._mobile = SimpleNamespace(list_blog=AsyncMock(return_value=SimpleNamespace(
        items=[primary],
        source="mobile_blog",
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=1,
        dropped_count=0,
        complete=True,
        evidence_items=(),
        identity_records=(primary,),
    )))
    source.get_post = AsyncMock()

    result = await source.list_blog("demo", None, 20)

    assert result.items == [primary]
    source.get_post.assert_not_awaited()
