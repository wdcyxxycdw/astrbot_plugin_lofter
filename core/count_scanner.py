from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable

from .errors import IDENTITY_SCHEMA_LOCATIONS, SourceError, SourceSchemaError
from .parser import Post, post_owner_identity
from .post_fields import PostEvidenceLedger, ensure_post_fields
from .post_identity import consistent_blog_owner, post_url_identity
from .source_scan import ContentSource, SourcePage

PostMatcher = Callable[[Post], bool]


@dataclass(frozen=True)
class TagScanResult:
    tag: str
    candidate_ids: set[str]
    matched_ids: set[str]
    tag_evidence: dict[str, frozenset[str]]
    owner_evidence: dict[str, str]
    scanned_pages: int
    warnings: list[str]
    complete: bool
    reliable: bool
    publish_time_evidence: dict[str, str] = field(default_factory=dict)
    url_evidence: dict[str, str] = field(default_factory=dict)
    evidence: PostEvidenceLedger = field(default_factory=PostEvidenceLedger)
    conflicted_ids: set[str] = field(default_factory=set)


async def scan_tags(
    tags: list[str],
    source: ContentSource,
    page_size: int,
    matches: PostMatcher,
    concurrency: int,
    deadline_at: float,
    monotonic: Callable[[], float],
) -> dict[str, TagScanResult]:
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run(tag: str) -> TagScanResult:
        async with semaphore:
            return await _scan_tag(
                tag,
                source,
                page_size,
                matches,
                deadline_at,
                monotonic,
            )

    results = await asyncio.gather(*(run(tag) for tag in tags))
    grouped: dict[str, list[TagScanResult]] = {}
    for result in results:
        grouped.setdefault(result.tag.casefold(), []).append(result)
    return {
        key: _merge_alias_results(values)
        for key, values in grouped.items()
    }


def _merge_alias_results(results: list[TagScanResult]) -> TagScanResult:
    first = results[0]
    evidence = _merge_post_evidence(results)
    conflicts = set(evidence.conflicted_ids)
    conflicts.update(*(result.conflicted_ids for result in results))
    tag_evidence = _merge_evidence(results, "tag_evidence", conflicts)
    time_evidence = _merge_evidence(
        results, "publish_time_evidence", conflicts
    )
    url_evidence = _merge_url_evidence(results, conflicts)
    owners = _merge_owner_evidence(results)
    matched = set().union(*(result.matched_ids for result in results))
    matched.difference_update(conflicts)
    warnings = _unique_warnings(results)
    if conflicts and "重复作品字段冲突" not in warnings:
        warnings.append("重复作品字段冲突")
    return TagScanResult(
        tag=first.tag,
        candidate_ids=set().union(*(result.candidate_ids for result in results)),
        matched_ids=matched,
        tag_evidence=tag_evidence,
        publish_time_evidence=time_evidence,
        url_evidence=url_evidence,
        owner_evidence=owners,
        evidence=evidence,
        conflicted_ids=conflicts,
        scanned_pages=sum(result.scanned_pages for result in results),
        warnings=warnings,
        complete=all(result.complete for result in results) and not conflicts,
        reliable=any(result.reliable for result in results),
    )


def _merge_post_evidence(
    results: list[TagScanResult],
) -> PostEvidenceLedger:
    ledger = PostEvidenceLedger()
    for result in results:
        ledger.merge(result.evidence, collect_conflicts=True)
    return ledger


def _merge_evidence(
    results: list[TagScanResult], field_name: str, conflicts: set[str]
) -> dict:
    ledger: dict = {}
    for result in results:
        for post_id, value in getattr(result, field_name).items():
            existing = ledger.get(post_id)
            if existing is not None and existing != value:
                conflicts.add(post_id)
                continue
            ledger[post_id] = value
    return ledger


def _merge_url_evidence(
    results: list[TagScanResult], conflicts: set[str]
) -> dict[str, str]:
    ledger: dict[str, str] = {}
    for result in results:
        for post_id, value in result.url_evidence.items():
            existing = ledger.get(post_id)
            if existing is None or existing == value:
                ledger[post_id] = value
                continue
            old_owner = post_url_identity(existing)[2]
            new_owner = post_url_identity(value)[2]
            if bool(old_owner) != bool(new_owner):
                ledger[post_id] = value if new_owner else existing
            else:
                conflicts.add(post_id)
    return ledger


def _merge_owner_evidence(results: list[TagScanResult]) -> dict[str, str]:
    ledger: dict[str, str] = {}
    for result in results:
        for post_id, owner in result.owner_evidence.items():
            try:
                resolved = consistent_blog_owner(ledger.get(post_id, ""), owner)
            except ValueError:
                raise SourceSchemaError("post.owner") from None
            if resolved:
                ledger[post_id] = resolved
    return ledger


def _unique_warnings(results: list[TagScanResult]) -> list[str]:
    warnings: list[str] = []
    for result in results:
        for warning in result.warnings:
            if warning not in warnings:
                warnings.append(warning)
    return warnings


async def _scan_tag(
    tag: str,
    source: ContentSource,
    page_size: int,
    matches: PostMatcher,
    deadline_at: float,
    monotonic: Callable[[], float],
) -> TagScanResult:
    state = _TagState(tag)
    cursor: str | None = None
    restarted = False
    while True:
        page = await _fetch_page(
            state, source, cursor, page_size, deadline_at, monotonic
        )
        if page is None:
            return state.result()
        state, cursor, restarted, valid = _apply_restart(
            state, cursor, restarted, page
        )
        state.observe_page(page.items)
        state.observe_evidence(page.evidence_items)
        if not valid:
            return state.result()
        page_accepted = state.accept_page(page, cursor, matches)
        evidence_consumed = await state.consume_evidence(
            page.evidence_items, matches, source, deadline_at, monotonic
        )
        if not page_accepted or not evidence_consumed:
            return state.result()
        consumed = await state.consume(
            page.items, matches, source, deadline_at, monotonic,
            verified=True,
        )
        if not consumed:
            return state.result()
        if not page.complete:
            state.fail("页面存在未映射项目")
            return state.result()
        if monotonic() >= deadline_at:
            state.fail("扫描超过统计 deadline")
            return state.result()
        if page.exhausted:
            if state.required_ids - state.verified_ids:
                state.fail("fallback 未覆盖已有可靠证据")
            state.complete = not state.warnings
            return state.result()
        cursor = page.next_cursor


async def _fetch_page(
    state: "_TagState",
    source: ContentSource,
    cursor: str | None,
    page_size: int,
    deadline_at: float,
    monotonic: Callable[[], float],
) -> SourcePage | None:
    remaining = deadline_at - monotonic()
    if remaining <= 0:
        state.fail("扫描超过统计 deadline")
        return None
    try:
        return await asyncio.wait_for(
            source.list_tag(state.tag, cursor, page_size, "new"),
            remaining,
        )
    except TimeoutError:
        state.fail("扫描超过统计 deadline")
    except SourceSchemaError as exc:
        if exc.location in IDENTITY_SCHEMA_LOCATIONS:
            raise
        state.observe_evidence(tuple(getattr(exc, "evidence_items", ())))
        state.fail(f"扫描失败：{exc}")
    except SourceError as exc:
        state.observe_evidence(tuple(getattr(exc, "evidence_items", ())))
        state.fail(f"扫描失败：{exc}")
    return None


def _apply_restart(
    state: "_TagState",
    cursor: str | None,
    restarted: bool,
    page: SourcePage,
) -> tuple["_TagState", str | None, bool, bool]:
    if not page.restarted:
        return state, cursor, restarted, True
    invalid = (
        restarted
        or not state.reliable
        or cursor is None
        or page.source == state.source_name
    )
    if invalid:
        state.fail("分页 restart 无效")
        return state, cursor, restarted, False
    return state.restart(), None, True, True


@dataclass
class _TagState:
    tag: str
    candidate_ids: set[str] = field(default_factory=set)
    matched_ids: set[str] = field(default_factory=set)
    seen_ids: set[str] = field(default_factory=set)
    verified_ids: set[str] = field(default_factory=set)
    known_tags: dict[str, frozenset[str]] = field(default_factory=dict)
    known_publish_times: dict[str, str] = field(default_factory=dict)
    known_urls: dict[str, str] = field(default_factory=dict)
    known_owners: dict[str, str] = field(default_factory=dict)
    evidence: PostEvidenceLedger = field(default_factory=PostEvidenceLedger)
    conflicted_ids: set[str] = field(default_factory=set)
    cursors: set[str] = field(default_factory=set)
    signatures: set[tuple[str, ...]] = field(default_factory=set)
    source_name: str = ""
    previous_oldest: str = ""
    scanned_pages: int = 0
    warnings: list[str] = field(default_factory=list)
    complete: bool = False
    reliable: bool = False
    required_ids: set[str] = field(default_factory=set)

    def restart(self) -> "_TagState":
        return _TagState(
            self.tag,
            known_tags=dict(self.known_tags),
            known_publish_times=dict(self.known_publish_times),
            known_urls=dict(self.known_urls),
            known_owners=dict(self.known_owners),
            evidence=self.evidence.copy(),
            conflicted_ids=set(self.conflicted_ids),
            required_ids=set(self.candidate_ids),
        )

    def observe_page(self, posts: tuple[Post, ...] | list[Post]) -> None:
        values = list(posts)
        self.candidate_ids.update(post.post_id for post in values)
        self._observe_values(values)

    def observe_evidence(self, posts: tuple[Post, ...]) -> None:
        self._observe_values(list(posts))

    def observe_error_evidence(self, error: SourceError) -> None:
        self.observe_evidence(tuple(getattr(error, "evidence_items", ())))

    def _observe_values(self, values: list[Post]) -> None:
        self.remember_identity(values)
        for post in values:
            self._remember_post_evidence(post)
            self._remember_tags(post)
            self._remember_publish_time(post)
            self._remember_url(post)

    def _remember_post_evidence(self, post: Post) -> None:
        self.evidence.observe(post, collect_conflicts=True)
        for post_id in self.evidence.conflicted_ids - self.conflicted_ids:
            self._mark_conflict(post_id)

    def remember_identity(self, posts: list[Post]) -> None:
        for post in posts:
            owner = post_owner_identity(post)
            existing = self.known_owners.get(post.post_id, "")
            try:
                resolved = consistent_blog_owner(existing, owner)
            except ValueError:
                raise SourceSchemaError("post.owner") from None
            if resolved:
                self.known_owners[post.post_id] = resolved

    def accept_page(
        self,
        page: SourcePage,
        cursor: str | None,
        matches: PostMatcher,
    ) -> bool:
        if not self._valid_contract(page, cursor):
            return False
        self.reliable = True
        if page.items:
            self.scanned_pages += 1
        missing_time = self._missing_sort_time(page)
        regressed = self._sort_regressed(page)
        if not missing_time and not regressed:
            self._remember_page_matches(page.items, matches)
        signature = tuple(post.post_id for post in page.items)
        repeated = bool(signature) and signature in self.signatures
        no_progress = bool(page.items) and not any(
            post.post_id not in self.seen_ids for post in page.items
        )
        if repeated or no_progress:
            self.fail("疑似分页未生效或接口返回重复页")
            return False
        if missing_time:
            self.fail("分页 sort=new 发布时间缺失")
            return False
        if regressed:
            self.fail("分页 sort=new 时间倒退")
            return False
        if signature:
            self.signatures.add(signature)
        if page.next_cursor:
            self.cursors.add(page.next_cursor)
        self._remember_oldest(page)
        return True

    def _remember_page_matches(
        self, posts: list[Post], matches: PostMatcher
    ) -> None:
        for post in posts:
            known = post.has_fields({"tags"})
            if known and post.post_id not in self.conflicted_ids and matches(post):
                self.matched_ids.add(post.post_id)

    def _valid_contract(
        self, page: SourcePage, cursor: str | None
    ) -> bool:
        invalid = page.sort != "new"
        invalid = invalid or bool(
            self.source_name and page.source != self.source_name
        )
        invalid = invalid or (not page.exhausted and not page.items)
        invalid = invalid or (
            not page.exhausted and page.next_cursor is None
        )
        invalid = invalid or (
            cursor is not None and page.next_cursor == cursor
        )
        invalid = invalid or bool(
            page.next_cursor and page.next_cursor in self.cursors
        )
        if invalid:
            self.fail("分页 source/sort/cursor 无进展")
            return False
        self.source_name = page.source
        return True

    def _missing_sort_time(self, page: SourcePage) -> bool:
        needs_comparison = page.sort == "new" and (
            bool(self.previous_oldest)
            or not page.exhausted
            or len(page.items) > 1
        )
        return needs_comparison and any(
            not post.has_fields({"publish_time"}) or not post.publish_time
            for post in page.items
        )

    def _sort_regressed(self, page: SourcePage) -> bool:
        times = [post.publish_time for post in page.items]
        within_page = any(
            current > previous
            for previous, current in zip(times, times[1:])
        )
        across_pages = bool(self.previous_oldest and times) and (
            max(times) > self.previous_oldest
        )
        return within_page or across_pages

    def _remember_oldest(self, page: SourcePage) -> None:
        times = [post.publish_time for post in page.items]
        if times:
            self.previous_oldest = min(times)

    def remember_duplicate_tags(self, posts: list[Post]) -> None:
        for post in posts:
            if post.post_id in self.seen_ids:
                self._remember_tags(post)

    def _remember_publish_time(self, post: Post) -> bool:
        if not post.has_fields({"publish_time"}) or not post.publish_time:
            return post.post_id not in self.conflicted_ids
        existing = self.known_publish_times.get(post.post_id)
        if existing is None:
            self.known_publish_times[post.post_id] = post.publish_time
        elif existing != post.publish_time:
            self._mark_conflict(post.post_id)
        return post.post_id not in self.conflicted_ids

    def _remember_url(self, post: Post) -> bool:
        if not post.has_fields({"url"}) or not post.url:
            return post.post_id not in self.conflicted_ids
        try:
            canonical, post_id, owner = post_url_identity(post.url)
        except ValueError:
            raise SourceSchemaError("post.url") from None
        if post_id != post.post_id:
            raise SourceSchemaError("post.id")
        existing = self.known_urls.get(post.post_id)
        if existing is None or existing == canonical:
            self.known_urls[post.post_id] = canonical
            return post.post_id not in self.conflicted_ids
        existing_owner = post_url_identity(existing)[2]
        if bool(existing_owner) != bool(owner):
            self.known_urls[post.post_id] = canonical if owner else existing
        else:
            self._mark_conflict(post.post_id)
        return post.post_id not in self.conflicted_ids

    def _mark_conflict(self, post_id: str) -> None:
        self.conflicted_ids.add(post_id)
        self.matched_ids.discard(post_id)
        self.fail("重复作品字段冲突")

    async def consume_evidence(
        self,
        posts: tuple[Post, ...],
        matches: PostMatcher,
        source: ContentSource,
        deadline_at: float,
        monotonic: Callable[[], float],
    ) -> bool:
        del matches
        evidence = list(posts)
        self.remember_identity(evidence)
        self.required_ids.update(post.post_id for post in evidence)
        for post in evidence:
            if post.has_fields({"tags"}):
                continue
            if not await self._enrich_evidence(
                post, source, deadline_at, monotonic
            ):
                return False
        return True

    async def _enrich_evidence(
        self,
        post: Post,
        source: ContentSource,
        deadline_at: float,
        monotonic: Callable[[], float],
    ) -> bool:
        remaining = deadline_at - monotonic()
        if remaining <= 0:
            self.fail("扫描超过统计 deadline")
            return False
        try:
            enriched = await asyncio.wait_for(
                ensure_post_fields(post, source, {"tags"}), remaining
            )
        except TimeoutError:
            self.fail("扫描超过统计 deadline")
            return False
        except SourceSchemaError as exc:
            if exc.location in IDENTITY_SCHEMA_LOCATIONS:
                raise
            self.observe_error_evidence(exc)
            self.fail(f"作品 {post.post_id} 标签字段未知：{exc}")
            return True
        except SourceError as exc:
            self.observe_error_evidence(exc)
            self.fail(f"作品 {post.post_id} 标签字段未知：{exc}")
            return True
        self._observe_values([enriched])
        return True

    async def consume(
        self,
        posts: list[Post],
        matches: PostMatcher,
        source: ContentSource,
        deadline_at: float,
        monotonic: Callable[[], float],
        *,
        verified: bool,
    ) -> bool:
        for post in posts:
            if verified and self._remember_verified(post, matches):
                continue
            self.candidate_ids.add(post.post_id)
            if not await self._match(
                post, matches, source, deadline_at, monotonic
            ):
                return False
        return True

    def _remember_verified(self, post: Post, matches: PostMatcher) -> bool:
        self.verified_ids.add(post.post_id)
        if post.post_id not in self.seen_ids:
            self.seen_ids.add(post.post_id)
            return False
        tags_known = self._remember_tags(post) and post.has_fields({"tags"})
        if tags_known and matches(post):
            self.matched_ids.add(post.post_id)
        return True

    async def _match(
        self,
        post: Post,
        matches: PostMatcher,
        source: ContentSource,
        deadline_at: float,
        monotonic: Callable[[], float],
    ) -> bool:
        remaining = deadline_at - monotonic()
        if remaining <= 0:
            self.fail("扫描超过统计 deadline")
            return False
        try:
            enriched = await asyncio.wait_for(
                ensure_post_fields(post, source, {"tags"}), remaining
            )
        except TimeoutError:
            self.fail("扫描超过统计 deadline")
            return False
        except SourceSchemaError as exc:
            if exc.location in IDENTITY_SCHEMA_LOCATIONS:
                raise
            self.observe_error_evidence(exc)
            self.fail(f"作品 {post.post_id} 标签字段未知：{exc}")
            return True
        except SourceError as exc:
            self.observe_error_evidence(exc)
            self.fail(f"作品 {post.post_id} 标签字段未知：{exc}")
            return True
        self.remember_identity([enriched])
        self._remember_post_evidence(enriched)
        self._remember_publish_time(enriched)
        self._remember_url(enriched)
        if not self._remember_tags(enriched):
            return True
        if matches(enriched):
            self.matched_ids.add(post.post_id)
        return True

    def _remember_tags(self, post: Post) -> bool:
        if not post.has_fields({"tags"}):
            return post.post_id not in self.conflicted_ids
        tags = frozenset(tag.casefold() for tag in post.tags)
        existing = self.known_tags.get(post.post_id)
        if existing is None:
            self.known_tags[post.post_id] = tags
        elif existing != tags:
            self._mark_conflict(post.post_id)
        return post.post_id not in self.conflicted_ids

    def fail(self, reason: str) -> None:
        self.complete = False
        warning = f"标签「{self.tag}」{reason}"
        if warning not in self.warnings:
            self.warnings.append(warning)

    def result(self) -> TagScanResult:
        return TagScanResult(
            tag=self.tag,
            candidate_ids=self.candidate_ids,
            matched_ids=self.matched_ids,
            tag_evidence=dict(self.known_tags),
            publish_time_evidence=dict(self.known_publish_times),
            url_evidence=dict(self.known_urls),
            owner_evidence=dict(self.known_owners),
            evidence=self.evidence.copy(),
            conflicted_ids=set(self.conflicted_ids),
            scanned_pages=self.scanned_pages,
            warnings=self.warnings,
            complete=self.complete,
            reliable=self.reliable,
        )
