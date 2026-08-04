import asyncio

import pytest

from core.errors import (
    PostEvidenceError,
    SourceError,
    SourcePartialError,
    SourceSchemaError,
    SourceTimeoutError,
    attach_source_evidence,
)
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

    with pytest.raises(SourcePartialError) as exc_info:
        await collect_pages(lambda _cursor: ready(next(pages)))

    error = exc_info.value
    assert error.reason == "evidence_shortfall"
    assert error.source == "dwr"
    assert error.restarted is True
    assert error.page_count == 2
    assert error.unique_count == 2


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


@pytest.mark.asyncio
async def test_partial_reason_priority_is_stable():
    cases = [
        (
            [page(["a"], cursor="2"), page([], source="dwr", cursor="3")],
            "empty_nonterminal_page",
        ),
        (
            [page(["a"], cursor="2"), page(["b"], source="dwr", sort="hot", exhausted=True)],
            "source_changed",
        ),
        (
            [page(["a"], cursor="2"), page(["a"], exhausted=True)],
            "page_repeated",
        ),
    ]

    for pages in cases:
        scripted = iter(pages[0])
        with pytest.raises(SourcePartialError) as exc_info:
            await collect_pages(lambda _cursor: ready(next(scripted)))
        assert exc_info.value.reason == pages[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pages", "reason"),
    [
        (
            [page(["a"], cursor="2"), page(["b"], cursor="2")],
            "cursor_stalled",
        ),
        (
            [page(["a"], cursor="2"), page(["b"], sort="hot", exhausted=True)],
            "sort_changed",
        ),
        (
            [
                page(["a", "b"], cursor="2"),
                SourcePage(
                    items=[
                        post("b", "2026-01-08 00:00"),
                        post("a", "2026-01-09 00:00"),
                    ],
                    source="mobile_tag",
                    next_cursor=None,
                    exhausted=True,
                    sort="new",
                    mapped_count=2,
                    dropped_count=0,
                    complete=True,
                ),
            ],
            "no_unique_progress",
        ),
        (
            [page(["a"], cursor=None, exhausted=False)],
            "next_cursor_missing",
        ),
    ],
)
async def test_progress_partial_reasons(pages, reason):
    scripted = iter(pages)

    with pytest.raises(SourcePartialError) as exc_info:
        await collect_pages(lambda _cursor: ready(next(scripted)))

    assert exc_info.value.reason == reason


@pytest.mark.asyncio
async def test_order_reasons_distinguish_within_and_across_pages():
    within = SourcePage(
        items=[
            post("old", "2026-01-07 00:00:00"),
            post("new", "2026-01-09 00:00:00"),
        ],
        source="mobile_tag",
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=2,
        dropped_count=0,
        complete=True,
    )
    with pytest.raises(SourcePartialError) as within_info:
        await collect_pages(lambda _cursor: ready(within))
    assert within_info.value.reason == "order_regressed_within_page"

    pages = iter([
        page(["a", "b"], cursor="2"),
        SourcePage(
            items=[post("c", "2026-01-10 00:00:00")],
            source="mobile_tag",
            next_cursor=None,
            exhausted=True,
            sort="new",
            mapped_count=1,
            dropped_count=0,
            complete=True,
        ),
    ])
    with pytest.raises(SourcePartialError) as across_info:
        await collect_pages(lambda _cursor: ready(next(pages)))
    assert across_info.value.reason == "order_regressed_across_pages"


@pytest.mark.asyncio
async def test_restart_reasons_and_validated_restart_context():
    with pytest.raises(SourcePartialError) as first_info:
        await collect_pages(
            lambda _cursor: ready(
                page(["a"], source="dwr", exhausted=True, restarted=True)
            )
        )
    assert first_info.value.reason == "restart_without_prior_page"
    assert first_info.value.restarted is False

    same_source = iter([
        page(["a"], cursor="2"),
        page(["a"], cursor="3", restarted=True),
    ])
    with pytest.raises(SourcePartialError) as same_info:
        await collect_pages(lambda _cursor: ready(next(same_source)))
    assert same_info.value.reason == "restart_same_source"
    assert same_info.value.restarted is False

    repeated = iter([
        page(["a"], cursor="2"),
        page(["a"], source="dwr", cursor="3", restarted=True),
        page(["b"], source="mobile_tag", exhausted=True, restarted=True),
    ])
    with pytest.raises(SourcePartialError) as repeated_info:
        await collect_pages(lambda _cursor: ready(next(repeated)))
    assert repeated_info.value.reason == "restart_repeated"
    assert repeated_info.value.restarted is True
    assert repeated_info.value.page_count == 3


@pytest.mark.asyncio
async def test_midscan_errors_keep_typed_context_without_payload():
    evidence = post("evidence", "2026-01-08 00:00:00")

    async def typed_fetch(cursor):
        if cursor is None:
            return page(["a"], cursor="2")
        error = SourcePartialError(
            2,
            1,
            reason="page_incomplete",
            source="dwr",
            restarted=False,
            page_count=1,
            unique_count=2,
        )
        attach_source_evidence(error, (evidence,))
        raise error

    with pytest.raises(SourcePartialError) as typed_info:
        await collect_pages(typed_fetch)

    typed = typed_info.value
    assert (typed.mapped_count, typed.dropped_count) == (3, 1)
    assert typed.reason == "page_incomplete"
    assert typed.source == "dwr"
    assert typed.page_count == 2
    assert typed.unique_count == 2
    assert typed.evidence_count == 2

    async def schema_fetch(cursor):
        if cursor is None:
            return page(["a"], cursor="2")
        raise SourceSchemaError("response")

    with pytest.raises(SourcePartialError) as schema_info:
        await collect_pages(schema_fetch)
    assert schema_info.value.reason == "source_schema_after_progress"

    async def source_fetch(cursor):
        if cursor is None:
            return page(["a"], cursor="2")
        raise SourceError()

    with pytest.raises(SourcePartialError) as source_info:
        await collect_pages(source_fetch)
    assert source_info.value.reason == "source_error_after_progress"


@pytest.mark.asyncio
async def test_post_evidence_error_remains_fail_closed_after_progress():
    first = page(["a"], cursor="2")
    conflicting = post("a", "2026-01-09 00:00:00")
    conflicting.title = "conflicting-title"
    second = SourcePage(
        items=[conflicting],
        source="mobile_tag",
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=1,
        dropped_count=0,
        complete=True,
    )
    pages = iter([first, second])

    with pytest.raises(PostEvidenceError) as exc_info:
        await collect_pages(lambda _cursor: ready(next(pages)))

    assert exc_info.value.diagnostic == "field_conflict:title:scan_ledger"


@pytest.mark.asyncio
async def test_dwr_twenty_zero_evidence_shortfall_has_exact_safe_context():
    visible = [
        post(f"visible-{index}", f"2026-01-{30 - index:02d} 00:00:00")
        for index in range(20)
    ]
    hidden = post("hidden", "2026-01-31 00:00:00")
    terminal = SourcePage(
        items=visible,
        source="dwr",
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=20,
        dropped_count=0,
        complete=True,
        evidence_items=(hidden,),
    )

    with pytest.raises(SourcePartialError) as exc_info:
        await collect_pages(lambda _cursor: ready(terminal), limit=20)

    error = exc_info.value
    assert (error.mapped_count, error.dropped_count) == (20, 0)
    assert error.reason == "evidence_shortfall"
    assert error.source == "dwr"
    assert error.restarted is False
    assert error.page_count == 1
    assert error.unique_count == 20
    assert error.evidence_count == 21


async def ready(value):
    return value
