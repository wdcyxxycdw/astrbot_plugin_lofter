from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

from .errors import (
    IDENTITY_SCHEMA_LOCATIONS,
    PostEvidenceError,
    SourceClosingError,
    SourceError,
    SourcePartialError,
    SourceSchemaError,
    SourceTimeoutError,
    attach_source_evidence,
)
from .parser import Post, post_owner_identity
from .post_identity import consistent_blog_owner, post_url_identity

SCAN_DEADLINE_SECONDS = 10 * 60


@dataclass(frozen=True)
class SourcePage:
    items: list[Post]
    source: str
    next_cursor: str | None
    exhausted: bool
    sort: str
    mapped_count: int
    dropped_count: int
    complete: bool
    diagnostics: tuple[str, ...] = field(default_factory=tuple)
    restarted: bool = False
    field_completeness: frozenset[str] | None = None
    provenance: dict[str, str] = field(default_factory=dict)
    evidence_items: tuple[Post, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        fields = self.field_completeness
        if fields is None:
            fields = _common_fields(self.items)
            object.__setattr__(self, "field_completeness", fields)
        if not self.provenance:
            object.__setattr__(self, "provenance", _common_provenance(self.items, fields))


class ContentSource(Protocol):
    async def get_post(self, url: str) -> Post: ...

    async def list_blog(
        self, username: str, cursor: str | None, limit: int
    ) -> SourcePage: ...

    async def list_tag(
        self, tag: str, cursor: str | None, limit: int, sort: str
    ) -> SourcePage: ...


PageFetcher = Callable[[str | None], Awaitable[SourcePage]]


def _common_fields(items: list[Post]) -> frozenset[str]:
    if not items:
        return frozenset()
    common = set(items[0].completeness)
    for post in items[1:]:
        common.intersection_update(post.completeness)
    return frozenset(common)


def _common_provenance(
    items: list[Post], fields: frozenset[str]
) -> dict[str, str]:
    result: dict[str, str] = {}
    for field_name in fields:
        sources = {post.provenance.get(field_name, post.source) for post in items}
        if len(sources) == 1:
            result[field_name] = sources.pop()
    return result


async def collect_pages(
    fetch_page: PageFetcher,
    *,
    limit: int | None = None,
    deadline: float = SCAN_DEADLINE_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> SourcePage:
    deadline_at = monotonic() + max(0.0, deadline)
    state = _ScanState()
    cursor: str | None = None
    restarted = False
    while True:
        page = await _fetch_with_deadline(
            fetch_page, cursor, state, deadline_at, monotonic
        )
        state.returned_page_count += 1
        _validate_page_identity(state, page)
        if monotonic() >= deadline_at:
            raise _deadline_error(state, page, "deadline_after_fetch")
        if page.restarted:
            _validate_restart(state, cursor, restarted, page)
            evidence = state.all_evidence_items()
            state = _restart_state(state, evidence)
            restarted = True
        _validate_progress(state, cursor, page)
        if not page.complete:
            raise _partial(state, page, reason="page_incomplete")
        state.add(page)
        if page.exhausted or _reached_limit(state, limit):
            if state.evidence_shortfall(limit):
                raise _partial(state, reason="evidence_shortfall")
            return state.result(page, limit, restarted)
        cursor = page.next_cursor
        await asyncio.sleep(0)


async def _fetch_with_deadline(
    fetch_page: PageFetcher,
    cursor: str | None,
    state: "_ScanState",
    deadline_at: float,
    monotonic: Callable[[], float],
) -> SourcePage:
    remaining = deadline_at - monotonic()
    if remaining <= 0:
        raise _deadline_error(state, reason="deadline_before_fetch")
    try:
        page = await asyncio.wait_for(fetch_page(cursor), remaining)
    except TimeoutError as exc:
        raise _deadline_error(state, reason="fetch_timeout") from exc
    except SourceClosingError:
        raise
    except SourcePartialError as exc:
        if state.page_count:
            raise _partial_error(state, exc) from exc
        raise
    except SourceSchemaError as exc:
        if exc.location in IDENTITY_SCHEMA_LOCATIONS:
            raise
        if state.page_count:
            raise _partial_with_error(
                state, exc, reason="source_schema_after_progress"
            ) from exc
        raise
    except SourceError as exc:
        if state.page_count:
            raise _partial_with_error(
                state, exc, reason="source_error_after_progress"
            ) from exc
        raise
    except Exception as exc:
        if state.page_count:
            attach_source_evidence(exc, state.all_evidence_items())
        raise
    return page


@dataclass
class _ScanState:
    items: list[Post] = field(default_factory=list)
    prior_items: tuple[Post, ...] = field(default_factory=tuple)
    ids: set[str] = field(default_factory=set)
    owners: dict[str, str] = field(default_factory=dict)
    fields: dict[str, dict[str, object]] = field(default_factory=dict)
    urls: dict[str, str] = field(default_factory=dict)
    cursors: set[str] = field(default_factory=set)
    page_signatures: set[tuple[str, ...]] = field(default_factory=set)
    page_count: int = 0
    returned_page_count: int = 0
    mapped_count: int = 0
    dropped_count: int = 0
    complete: bool = True
    diagnostics: list[str] = field(default_factory=list)
    source: str = ""
    sort: str = ""
    previous_oldest: str = ""
    restarted: bool = False

    def __post_init__(self) -> None:
        for post in self.prior_items:
            self.remember_identity(post)

    def all_evidence_items(self) -> tuple[Post, ...]:
        return (*self.prior_items, *self.items)

    def evidence_shortfall(self, limit: int | None) -> bool:
        ordered_ids: list[str] = []
        seen: set[str] = set()
        for post in self.prior_items:
            if post.post_id not in seen:
                seen.add(post.post_id)
                ordered_ids.append(post.post_id)
        required = ordered_ids if limit is None else ordered_ids[:limit]
        visible = self.items if limit is None else self.items[:limit]
        visible_ids = {post.post_id for post in visible}
        return not set(required).issubset(visible_ids)

    def remember_identity(self, post: Post) -> None:
        self._remember_owner(post)
        self._remember_known_fields(post)
        self._remember_url(post)

    def _remember_owner(self, post: Post) -> None:
        owner = post_owner_identity(post)
        existing_owner = self.owners.get(post.post_id, "")
        try:
            resolved_owner = consistent_blog_owner(existing_owner, owner)
        except ValueError:
            raise SourceSchemaError("post.owner") from None
        if resolved_owner:
            self.owners[post.post_id] = resolved_owner

    def _remember_known_fields(self, post: Post) -> None:
        text_fields = ("title", "summary", "content", "author")
        for field_name in text_fields:
            if post.has_fields({field_name}):
                self._remember_post_field(
                    post, field_name, getattr(post, field_name)
                )
        if post.has_fields({"images"}):
            self._remember_post_field(post, "images", tuple(post.images))
        if post.has_fields({"tags"}):
            value = frozenset(tag.casefold() for tag in post.tags)
            self._remember_post_field(post, "tags", value)
        if post.has_fields({"publish_time"}) and post.publish_time:
            self._remember_post_field(
                post, "publish_time", post.publish_time
            )

    def _remember_post_field(
        self, post: Post, field_name: str, value: object
    ) -> None:
        ledger = self.fields.setdefault(field_name, {})
        self._remember_field(ledger, post.post_id, field_name, value)

    def _remember_url(self, post: Post) -> None:
        if not post.has_fields({"url"}) or not post.url:
            return
        try:
            canonical, post_id, owner = post_url_identity(post.url)
        except ValueError:
            raise SourceSchemaError("post.url") from None
        if post_id != post.post_id:
            raise SourceSchemaError("post.id")
        existing = self.urls.get(post.post_id)
        if existing is None or existing == canonical:
            self.urls[post.post_id] = canonical
            return
        existing_owner = post_url_identity(existing)[2]
        if bool(existing_owner) != bool(owner):
            self.urls[post.post_id] = canonical if owner else existing
            return
        raise PostEvidenceError(
            "canonical_url_conflict", "url", "scan_ledger"
        )

    def _remember_field(
        self, ledger: dict, post_id: str, field_name: str, value: object
    ) -> None:
        existing = ledger.get(post_id)
        if existing is not None and existing != value:
            raise PostEvidenceError(
                "field_conflict", field_name, "scan_ledger"
            )
        ledger[post_id] = value

    def add(self, page: SourcePage) -> None:
        self.page_count += 1
        self.prior_items = (*self.prior_items, *page.evidence_items)
        self.source = page.source
        self.sort = page.sort
        self.mapped_count += page.mapped_count
        self.dropped_count += page.dropped_count
        self.complete = self.complete and page.complete
        self.diagnostics.extend(page.diagnostics)
        for post in page.items:
            self.remember_identity(post)
            if post.post_id in self.ids:
                self.prior_items = (*self.prior_items, post)
                continue
            self.ids.add(post.post_id)
            self.items.append(post)
        if page.next_cursor:
            self.cursors.add(page.next_cursor)
        self.page_signatures.add(tuple(post.post_id for post in page.items))
        times = [
            post.publish_time for post in page.items
            if post.has_fields({"publish_time"}) and post.publish_time
        ]
        if times:
            self.previous_oldest = min(times)

    def result(
        self,
        page: SourcePage,
        limit: int | None,
        restarted: bool,
    ) -> SourcePage:
        items = self.items if limit is None else self.items[:limit]
        return SourcePage(
            items=items,
            source=self.source,
            next_cursor=page.next_cursor,
            exhausted=page.exhausted,
            sort=page.sort,
            mapped_count=self.mapped_count,
            dropped_count=self.dropped_count,
            complete=self.complete,
            diagnostics=tuple(self.diagnostics),
            restarted=restarted,
            field_completeness=_common_fields(items),
            provenance=_common_provenance(items, _common_fields(items)),
            evidence_items=self.prior_items,
        )


def _restart_state(
    state: _ScanState, evidence: tuple[Post, ...]
) -> _ScanState:
    return _ScanState(
        prior_items=evidence,
        owners=dict(state.owners),
        fields={name: dict(values) for name, values in state.fields.items()},
        urls=dict(state.urls),
        returned_page_count=state.returned_page_count,
        restarted=True,
    )


def _validate_page_identity(state: _ScanState, page: SourcePage) -> None:
    for post in (*page.evidence_items, *page.items):
        state.remember_identity(post)


def _validate_progress(
    state: _ScanState, cursor: str | None, page: SourcePage
) -> None:
    if not page.exhausted and not page.items:
        raise _partial(state, page, reason="empty_nonterminal_page")
    if state.source and page.source != state.source:
        raise _partial(state, page, reason="source_changed")
    if state.sort and page.sort != state.sort:
        raise _partial(state, page, reason="sort_changed")
    if cursor is not None and page.next_cursor == cursor:
        raise _partial(state, page, reason="cursor_stalled")
    if page.next_cursor and page.next_cursor in state.cursors:
        raise _partial(state, page, reason="cursor_repeated")
    signature = tuple(post.post_id for post in page.items)
    if signature and signature in state.page_signatures:
        raise _partial(state, page, reason="page_repeated")
    if page.items and not any(post.post_id not in state.ids for post in page.items):
        raise _partial(state, page, reason="no_unique_progress")
    if _missing_sort_time(state, page):
        raise _partial(state, page, reason="publish_time_missing")
    order_reason = _sort_regression_reason(state, page)
    if order_reason is not None:
        raise _partial(state, page, reason=order_reason)
    if not page.exhausted and page.next_cursor is None:
        raise _partial(state, page, reason="next_cursor_missing")


def _validate_restart(
    state: _ScanState, cursor: str | None, restarted: bool, page: SourcePage
) -> None:
    if restarted:
        raise _partial(state, page, reason="restart_repeated")
    if not state.page_count:
        raise _partial(state, page, reason="restart_without_prior_page")
    if cursor is None:
        raise _partial(state, page, reason="restart_without_cursor")
    if page.source == state.source:
        raise _partial(state, page, reason="restart_same_source")


def _missing_sort_time(state: _ScanState, page: SourcePage) -> bool:
    needs_comparison = page.sort == "new" and (
        state.page_count > 0 or not page.exhausted or len(page.items) > 1
    )
    if not needs_comparison:
        return False
    return any(
        not post.has_fields({"publish_time"}) or not post.publish_time
        for post in page.items
    )


def _sort_regression_reason(
    state: _ScanState, page: SourcePage
) -> str | None:
    if page.sort != "new":
        return None
    times = [post.publish_time for post in page.items]
    if any(
        current > previous
        for previous, current in zip(times, times[1:])
    ):
        return "order_regressed_within_page"
    if state.previous_oldest and times and max(times) > state.previous_oldest:
        return "order_regressed_across_pages"
    return None


def _reached_limit(state: _ScanState, limit: int | None) -> bool:
    return limit is not None and len(state.items) >= limit


def _deadline_error(
    state: _ScanState,
    page: SourcePage | None = None,
    reason: str = "deadline_before_fetch",
) -> SourceError:
    if state.page_count or page is not None:
        return _partial(state, page, reason=reason)
    return SourceTimeoutError()


def _partial_with_error(
    state: _ScanState,
    error: SourceError,
    *,
    reason: str,
) -> SourcePartialError:
    partial = _partial(state, reason=reason)
    attach_source_evidence(partial, getattr(error, "evidence_items", ()))
    return partial


def _partial_error(
    state: _ScanState, error: SourcePartialError
) -> SourcePartialError:
    source = error.source if error.source != "unknown" else state.source
    partial = SourcePartialError(
        state.mapped_count + error.mapped_count,
        state.dropped_count + error.dropped_count,
        reason=error.reason,
        source=source,
        restarted=state.restarted or error.restarted is True,
        page_count=state.returned_page_count + error.page_count,
        unique_count=max(len(state.ids), error.unique_count),
    )
    attach_source_evidence(partial, state.all_evidence_items())
    attach_source_evidence(partial, getattr(error, "evidence_items", ()))
    return partial


def _partial(
    state: _ScanState,
    page: SourcePage | None = None,
    *,
    reason: str = "source_partial",
) -> SourcePartialError:
    mapped = state.mapped_count + (page.mapped_count if page else 0)
    dropped = state.dropped_count + (page.dropped_count if page else 0)
    page_ids = {post.post_id for post in page.items} if page else set()
    source = page.source if page is not None else state.source
    partial = SourcePartialError(
        mapped,
        dropped,
        reason=reason,
        source=source,
        restarted=state.restarted,
        page_count=state.returned_page_count,
        unique_count=len(state.ids | page_ids),
    )
    attach_source_evidence(partial, state.all_evidence_items())
    if page is not None:
        attach_source_evidence(partial, (*page.evidence_items, *page.items))
    return partial
