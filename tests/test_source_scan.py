import asyncio

import pytest

from core.errors import SourcePartialError, SourceTimeoutError
from core.parser import Post
from core.source_scan import SourcePage, collect_pages


def post(post_id, publish_time):
    return Post(
        post_id=post_id,
        title=post_id,
        summary="",
        url=f"https://demo.lofter.com/post/{post_id}",
        publish_time=publish_time,
    )


def page(
    ids,
    *,
    source="mobile_tag",
    cursor=None,
    exhausted=False,
    restarted=False,
    sort="new",
    complete=True,
):
    times = {}
    items = []
    for index, item in enumerate(ids):
        when = times.setdefault(item, f"2026-01-0{9 - index} 00:00")
        items.append(post(item, when))
    return SourcePage(
        items=items,
        source=source,
        next_cursor=cursor,
        exhausted=exhausted,
        sort=sort,
        mapped_count=len(items),
        dropped_count=0,
        complete=complete,
        restarted=restarted,
    )


@pytest.mark.asyncio
async def test_collect_pages_preserves_source_affinity():
    pages = {
        None: page(["a", "b"], cursor="2"),
        "2": SourcePage(
            items=[post("c", "2026-01-07 00:00")],
            source="mobile_tag",
            next_cursor=None,
            exhausted=True,
            sort="new",
            mapped_count=1,
            dropped_count=0,
            complete=True,
        ),
    }

    result = await collect_pages(lambda cursor: ready(pages[cursor]))

    assert [item.post_id for item in result.items] == ["a", "b", "c"]
    assert result.source == "mobile_tag"
    assert result.exhausted is True


@pytest.mark.asyncio
async def test_restart_discards_primary_prefix_and_uses_fallback_from_start():
    calls = []

    async def fetch(cursor):
        calls.append(cursor)
        if len(calls) == 1:
            return page(["primary-a"], cursor="primary-2")
        if len(calls) == 2:
            return page(
                ["primary-a"], source="dwr", cursor="fallback-2", restarted=True
            )
        return page(["fallback-b"], source="dwr", exhausted=True)

    result = await collect_pages(fetch)

    assert calls == [None, "primary-2", "fallback-2"]
    assert [item.post_id for item in result.items] == ["primary-a", "fallback-b"]
    assert result.source == "dwr"


@pytest.mark.asyncio
async def test_restart_requires_fallback_to_cover_prior_ids():
    pages = iter([
        page(["primary-a", "primary-b"], cursor="primary-2"),
        page(
            ["fallback-a", "fallback-b"],
            source="dwr",
            exhausted=True,
            restarted=True,
        ),
    ])

    with pytest.raises(SourcePartialError):
        await collect_pages(lambda _cursor: ready(next(pages)))


@pytest.mark.asyncio
async def test_unmarked_source_switch_is_partial_not_spliced():
    pages = iter([
        page(["primary"], cursor="2"),
        page(["fallback"], source="dwr", exhausted=True),
    ])

    with pytest.raises(SourcePartialError) as exc_info:
        await collect_pages(lambda _cursor: ready(next(pages)))

    assert exc_info.value.mapped_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pages",
    [
        [page(["a"], cursor="1"), page(["b"], cursor="1")],
        [page(["a"], cursor="2"), page(["a"], exhausted=True)],
        [page(["a"], cursor="2"), page(["a", "a"], cursor="3")],
    ],
)
async def test_duplicate_cursor_page_or_no_progress_is_partial(pages):
    scripted = iter(pages)
    with pytest.raises(SourcePartialError):
        await collect_pages(lambda _cursor: ready(next(scripted)))


@pytest.mark.asyncio
async def test_sort_regression_is_partial():
    first = SourcePage(
        items=[post("a", "2026-01-09 00:00"), post("b", "2026-01-08 00:00")],
        source="mobile_tag",
        next_cursor="2",
        exhausted=False,
        sort="new",
        mapped_count=2,
        dropped_count=0,
        complete=True,
    )
    second = SourcePage(
        items=[post("c", "2026-01-10 00:00")],
        source="mobile_tag",
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=1,
        dropped_count=0,
        complete=True,
    )
    scripted = iter([first, second])

    with pytest.raises(SourcePartialError):
        await collect_pages(lambda _cursor: ready(next(scripted)))


@pytest.mark.asyncio
async def test_unknown_publish_time_needed_for_new_sort_is_partial():
    first = SourcePage(
        items=[post("a", "")],
        source="mobile_tag",
        next_cursor="2",
        exhausted=False,
        sort="new",
        mapped_count=1,
        dropped_count=0,
        complete=True,
    )

    with pytest.raises(SourcePartialError) as exc_info:
        await collect_pages(lambda _cursor: ready(first))

    assert exc_info.value.mapped_count == 1


@pytest.mark.asyncio
async def test_deadline_returns_typed_partial_with_reliable_counts():
    clock = iter([0.0, 0.0, 2.0])
    pages = {None: page(["a"], cursor="2")}

    with pytest.raises(SourcePartialError) as exc_info:
        await collect_pages(
            lambda cursor: ready(pages[cursor]), deadline=1.0,
            monotonic=lambda: next(clock),
        )

    assert exc_info.value.mapped_count == 1


@pytest.mark.asyncio
async def test_hanging_first_page_has_typed_timeout_and_is_cancelled():
    cancelled = asyncio.Event()

    async def fetch(_cursor):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with pytest.raises(SourceTimeoutError):
        await collect_pages(fetch, deadline=0.01)

    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_hanging_second_page_is_partial_and_is_cancelled():
    cancelled = asyncio.Event()

    async def fetch(cursor):
        if cursor is None:
            return page(["a"], cursor="2")
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with pytest.raises(SourcePartialError) as exc_info:
        await collect_pages(fetch, deadline=0.01)

    assert (exc_info.value.mapped_count, exc_info.value.dropped_count) == (1, 0)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_midscan_partial_failure_merges_malformed_counts():
    async def fetch(cursor):
        if cursor is None:
            return page(["a"] * 10, cursor="2")
        raise SourcePartialError(0, 2)

    with pytest.raises(SourcePartialError) as exc_info:
        await collect_pages(fetch)

    assert (exc_info.value.mapped_count, exc_info.value.dropped_count) == (10, 2)


@pytest.mark.asyncio
async def test_empty_non_terminal_page_is_typed_no_progress():
    with pytest.raises(SourcePartialError) as exc_info:
        await collect_pages(lambda _cursor: ready(page([], cursor="anything")))

    assert (exc_info.value.mapped_count, exc_info.value.dropped_count) == (0, 0)


@pytest.mark.asyncio
async def test_incomplete_page_cannot_succeed():
    with pytest.raises(SourcePartialError) as exc_info:
        await collect_pages(
            lambda _cursor: ready(page(["a"], exhausted=True, complete=False))
        )

    assert exc_info.value.mapped_count == 1


@pytest.mark.asyncio
async def test_limit_stops_without_fetching_extra_page():
    calls = []

    async def fetch(cursor):
        calls.append(cursor)
        return page(["a", "b"], cursor="2")

    result = await collect_pages(fetch, limit=1)

    assert [item.post_id for item in result.items] == ["a"]
    assert calls == [None]


async def ready(value):
    return value
