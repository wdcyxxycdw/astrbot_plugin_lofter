from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, replace

from .client import LofterClient
from .dwr_parser import parse_dwr_response_result
from .errors import (
    IDENTITY_SCHEMA_LOCATIONS,
    SourceBusinessError,
    SourceChallengeError,
    SourceClosingError,
    SourceError,
    SourceHTTPError,
    SourceLimitError,
    SourcePartialError,
    SourceRetryExhaustedError,
    SourceSchemaError,
    SourceTimeoutError,
    attach_source_evidence,
    limit_identity_complete,
)
from .mobile_adapter import MobileAdapter
from .parser import Post, parse_blog_posts, parse_embedded_post, parse_post_page
from .post_fields import merge_post_fields, validate_post_evidence
from .post_identity import (
    canonical_post_url,
    consistent_blog_owner,
    mobile_decimal_ids,
    post_id_from_url,
    post_url_identity,
)
from .post_time import parse_publish_time
from .source_limits import MAX_ITEMS, MAX_URL_BYTES, validate_text_bytes
from .source_scan import ContentSource, SourcePage, collect_pages

_DNS_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")


@dataclass(frozen=True)
class MobileTagDiagnostic:
    page: SourcePage | None
    evidence_items: tuple[Post, ...]
    fallback_reason: str | None
    error: SourceError | None
    item_count: int = 0
    time_count: int = 0
    regression_count: int = 0
    equal_count: int = 0
    first_regression_pair_ordinal: int = 0


class DefaultContentSource:
    def __init__(self, client: LofterClient | None = None):
        self._client = client or LofterClient()
        self._mobile = MobileAdapter(self._client)

    async def initialize(self) -> None:
        await self._client.initialize()

    def update_cookie(self, cookie: str) -> None:
        self._client.update_cookie(cookie)

    async def close(self) -> None:
        await self._client.close()

    async def get_post(self, url: str) -> Post:
        post_id, ids = _post_identity(url)
        evidence: tuple[Post, ...] = ()
        if ids is not None:
            try:
                post = await self._mobile.get_post(*ids)
                _validate_post_owner(url, post_id, post)
                return post
            except SourceClosingError:
                raise
            except SourceLimitError as exc:
                if not limit_identity_complete(exc):
                    raise
                _validate_post_error_evidence(url, post_id, exc)
                evidence = _error_evidence(exc)
            except SourceSchemaError as exc:
                _validate_post_error_evidence(url, post_id, exc)
                _raise_identity_error(exc)
                evidence = _error_evidence(exc)
            except SourceError as exc:
                _validate_post_error_evidence(url, post_id, exc)
                evidence = _error_evidence(exc)
        try:
            post = await self._post_fallback(url, post_id)
        except Exception as exc:
            attach_source_evidence(exc, evidence)
            raise
        return _validated_fallback_post(post, evidence)

    async def list_blog(
        self, username: str, cursor: str | None, limit: int
    ) -> SourcePage:
        _validate_username(username)
        _validate_limit(limit)
        source, value = _decode_cursor(cursor, "mobile_blog", {"mobile_blog"})
        if source == "html_blog":
            raise SourceSchemaError("cursor")
        primary, evidence = await self._mobile_blog_primary(
            username, value, limit
        )
        if primary is not None and primary.complete:
            return primary
        try:
            fallback = await self._blog_fallback(
                username, cursor is not None, limit
            )
        except SourceClosingError:
            raise
        except SourceLimitError as exc:
            attach_source_evidence(exc, evidence)
            if not limit_identity_complete(exc):
                raise
            if primary is not None:
                return _with_evidence(primary, _error_evidence(exc))
            raise
        except SourceSchemaError as exc:
            attach_source_evidence(exc, evidence)
            _raise_identity_error(exc)
            if primary is not None:
                return _with_evidence(primary, _error_evidence(exc))
            raise
        except SourceError as exc:
            attach_source_evidence(exc, evidence)
            if primary is not None:
                return _with_evidence(primary, _error_evidence(exc))
            raise
        except Exception as exc:
            attach_source_evidence(exc, evidence)
            raise
        return _with_evidence(fallback, evidence)

    async def _mobile_blog_primary(
        self, username: str, cursor: str | None, limit: int
    ) -> tuple[SourcePage | None, tuple[Post, ...]]:
        primary: SourcePage | None = None
        evidence: tuple[Post, ...] = ()
        try:
            page = await self._mobile.list_blog(username, cursor, limit)
            evidence = _mobile_identity_records(page)
            primary = _from_mobile(page, "mobile_blog")
            _validate_blog_page_owner(username, primary)
        except SourceClosingError:
            raise
        except SourceLimitError as exc:
            if not limit_identity_complete(exc):
                raise
            evidence = _error_evidence(exc)
            _validate_blog_posts_owner(username, evidence)
        except SourceSchemaError as exc:
            evidence = _error_evidence(exc)
            _validate_blog_posts_owner(username, evidence)
            _raise_identity_error(exc)
        except SourceError as exc:
            evidence = _error_evidence(exc)
        return primary, evidence

    async def diagnose_mobile_tag(
        self, tag: str, limit: int, sort: str = "new"
    ) -> MobileTagDiagnostic:
        _validate_limit(limit)
        if sort != "new":
            raise SourceSchemaError("sort")
        return await self._diagnose_mobile_tag(tag, None, sort)

    async def list_tag(
        self, tag: str, cursor: str | None, limit: int, sort: str
    ) -> SourcePage:
        _validate_limit(limit)
        if sort != "new":
            raise SourceSchemaError("sort")
        source, value = _decode_cursor(cursor, "mobile_tag", {"mobile_tag", "dwr"})
        if source == "dwr":
            return await self._dwr_page(tag, value, limit, restarted=False)
        if cursor is None:
            diagnostic = await self._diagnose_mobile_tag(tag, value, sort)
            primary = diagnostic.page
            evidence = diagnostic.evidence_items
            fallback_reason = diagnostic.fallback_reason
            if fallback_reason is None and primary is not None:
                return primary
        else:
            primary = None
            evidence = ()
            fallback_reason = "mobile_cursor_restart"
        fallback_primary = (
            primary if primary is not None and not primary.complete else None
        )
        try:
            fallback = await self._dwr_page(
                tag,
                "0",
                limit,
                restarted=cursor is not None,
                restart_requires_prior_coverage=cursor is None,
            )
        except SourceClosingError:
            raise
        except SourceLimitError as exc:
            _set_mobile_fallback_reason(exc, fallback_reason)
            attach_source_evidence(exc, evidence)
            if not limit_identity_complete(exc):
                raise
            if fallback_primary is not None:
                return _tag_fallback_page(
                    _with_evidence(fallback_primary, _error_evidence(exc)),
                    fallback_reason,
                )
            raise
        except SourceSchemaError as exc:
            _set_mobile_fallback_reason(exc, fallback_reason)
            attach_source_evidence(exc, evidence)
            _raise_identity_error(exc)
            if fallback_primary is not None:
                return _tag_fallback_page(
                    _with_evidence(fallback_primary, _error_evidence(exc)),
                    fallback_reason,
                )
            raise
        except SourceError as exc:
            _set_mobile_fallback_reason(exc, fallback_reason)
            attach_source_evidence(exc, evidence)
            if fallback_primary is not None:
                return _tag_fallback_page(
                    _with_evidence(fallback_primary, _error_evidence(exc)),
                    fallback_reason,
                )
            raise
        try:
            result = _finish_tag_fallback(
                fallback_primary, fallback, evidence, cursor
            )
        except SourceError as exc:
            _set_mobile_fallback_reason(exc, fallback_reason)
            raise
        return _tag_fallback_page(result, fallback_reason)

    async def _diagnose_mobile_tag(
        self, tag: str, cursor: str | None, sort: str
    ) -> MobileTagDiagnostic:
        try:
            page = await self._mobile.list_tag(tag, cursor)
            evidence = _mobile_identity_records(page)
            primary = _from_mobile(page, "mobile_tag")
        except SourceClosingError:
            raise
        except SourceLimitError as exc:
            if not limit_identity_complete(exc):
                raise
            return MobileTagDiagnostic(
                None,
                _error_evidence(exc),
                _mobile_error_reason(exc),
                exc,
            )
        except SourceSchemaError as exc:
            _raise_identity_error(exc)
            return MobileTagDiagnostic(
                None,
                _error_evidence(exc),
                _mobile_error_reason(exc),
                exc,
            )
        except SourceError as exc:
            return MobileTagDiagnostic(
                None,
                _error_evidence(exc),
                _mobile_error_reason(exc),
                exc,
            )
        reason, counts = _mobile_tag_diagnostic(primary, sort)
        if reason == "mobile_order_regressed":
            primary = replace(
                primary,
                items=sorted(
                    primary.items,
                    key=lambda post: parse_publish_time(post.publish_time),
                    reverse=True,
                ),
            )
            reason = None
        return MobileTagDiagnostic(
            primary,
            evidence,
            reason,
            None,
            *counts,
        )

    async def collect_tag(self, tag: str, limit: int, sort: str = "new") -> SourcePage:
        return await collect_pages(
            lambda cursor: self.list_tag(tag, cursor, min(limit, MAX_ITEMS), sort),
            limit=limit,
        )

    async def _post_fallback(self, url: str, post_id: str) -> Post:
        evidence: tuple[Post, ...] = ()
        try:
            html = await self._client.get(url, credentialed=False)
        except SourceClosingError:
            raise
        except SourceLimitError:
            raise
        except SourceError as exc:
            return await self._credentialed_post(url, post_id, _error_evidence(exc))
        try:
            embedded = await asyncio.to_thread(
                parse_embedded_post,
                html,
                url,
                expected_post_id=post_id,
            )
        except SourceClosingError:
            raise
        except SourceLimitError as exc:
            if not limit_identity_complete(exc):
                raise
            evidence = _error_evidence(exc)
        except SourceSchemaError as exc:
            _raise_identity_error(exc)
            evidence = _error_evidence(exc)
        except SourceError as exc:
            evidence = _error_evidence(exc)
        else:
            return embedded
        try:
            post = await parse_post_page(html, url, expected_post_id=post_id)
        except SourceClosingError:
            raise
        except SourceLimitError as exc:
            if not limit_identity_complete(exc):
                raise
            attach_source_evidence(exc, evidence)
            evidence = _error_evidence(exc)
        except SourceSchemaError as exc:
            attach_source_evidence(exc, evidence)
            _raise_identity_error(exc)
            evidence = _error_evidence(exc)
        except SourceError as exc:
            attach_source_evidence(exc, evidence)
            evidence = _error_evidence(exc)
        else:
            return _validated_fallback_post(post, evidence)
        return await self._credentialed_post(url, post_id, evidence)

    async def _credentialed_post(
        self, url: str, post_id: str, evidence: tuple[Post, ...]
    ) -> Post:
        try:
            legacy = await self._client.get(url, credentialed=True)
            post = await parse_post_page(
                legacy, url, expected_post_id=post_id
            )
        except Exception as exc:
            attach_source_evidence(exc, evidence)
            raise
        return _validated_fallback_post(post, evidence)

    async def _blog_fallback(
        self, username: str, restarted: bool, limit: int
    ) -> SourcePage:
        url = f"https://{username}.lofter.com"
        evidence: tuple[Post, ...] = ()
        try:
            html = await self._client.get(url, credentialed=False)
            posts = await parse_blog_posts(html, expected_owner=username)
        except SourceClosingError:
            raise
        except SourceLimitError as exc:
            if not limit_identity_complete(exc):
                raise
            evidence = _error_evidence(exc)
            _validate_blog_posts_owner(username, evidence)
            posts = await self._credentialed_blog(url, username, evidence)
        except SourceSchemaError as exc:
            _raise_identity_error(exc)
            evidence = _error_evidence(exc)
            _validate_blog_posts_owner(username, evidence)
            posts = await self._credentialed_blog(url, username, evidence)
        except SourceError as exc:
            evidence = _error_evidence(exc)
            posts = await self._credentialed_blog(url, username, evidence)
        try:
            posts = await _complete_html_blog_posts(self, posts, restarted)
        except Exception as exc:
            attach_source_evidence(exc, evidence)
            raise
        _validate_blog_fallback(username, evidence, posts, limit, restarted)
        return _html_blog_page(posts, evidence, restarted)

    async def _credentialed_blog(
        self, url: str, username: str, evidence: tuple[Post, ...]
    ) -> list[Post]:
        try:
            html = await self._client.get(url, credentialed=True)
            return await parse_blog_posts(html, expected_owner=username)
        except Exception as exc:
            attach_source_evidence(exc, evidence)
            raise

    async def _dwr_page(
        self,
        tag: str,
        offset: str | None,
        limit: int,
        *,
        restarted: bool,
        restart_requires_prior_coverage: bool = True,
    ) -> SourcePage:
        numeric_offset = _offset_value(offset)
        raw = await self._client.search_tag(tag, numeric_offset, limit)
        result = await parse_dwr_response_result(raw)
        exhausted = result.is_empty
        next_cursor = None if exhausted else _encode_cursor(
            "dwr", numeric_offset + limit
        )
        return SourcePage(
            items=result.items,
            source="dwr",
            next_cursor=next_cursor,
            exhausted=exhausted,
            sort="new",
            mapped_count=result.mapped_count,
            dropped_count=result.dropped_count,
            complete=result.complete,
            restarted=restarted,
            restart_requires_prior_coverage=restart_requires_prior_coverage,
            evidence_items=result.evidence_items,
        )


def _validate_blog_fallback(
    username: str,
    evidence: tuple[Post, ...],
    posts: list[Post],
    limit: int,
    restarted: bool,
) -> None:
    _validate_blog_posts_owner(username, (*evidence, *posts))
    validate_post_evidence((*evidence, *posts))
    required = []
    seen: set[str] = set()
    for post in evidence:
        if post.post_id not in seen:
            seen.add(post.post_id)
            required.append(post.post_id)
    visible_ids = {post.post_id for post in posts[:limit]}
    if set(required[:limit]) <= visible_ids:
        return
    error = SourcePartialError(
        len(posts),
        0,
        reason="evidence_shortfall",
        source="html_blog",
        restarted=restarted,
        page_count=1,
        unique_count=len({post.post_id for post in posts}),
    )
    attach_source_evidence(error, evidence)
    raise error


def _html_blog_page(
    posts: list[Post], evidence: tuple[Post, ...], restarted: bool
) -> SourcePage:
    return SourcePage(
        items=posts,
        source="html_blog",
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=len(posts),
        dropped_count=0,
        complete=True,
        diagnostics=("fallback_html",),
        restarted=restarted,
        evidence_items=evidence,
    )


async def _complete_html_blog_posts(
    source: DefaultContentSource, posts: list[Post], restarted: bool
) -> list[Post]:
    completed: list[Post] = []
    observed: list[Post] = list(posts)
    try:
        for post in posts:
            value = post
            if not post.has_fields({"publish_time"}) or not post.publish_time:
                detail = await source.get_post(post.url)
                observed.append(detail)
                value = merge_post_fields(post, detail)
            if not value.has_fields({"publish_time"}) or not value.publish_time:
                raise SourceSchemaError("publishTime")
            completed.append(value)
        _validate_newest_first(completed, restarted)
        return completed
    except Exception as exc:
        attach_source_evidence(exc, observed)
        raise


def _mobile_tag_diagnostic(
    page: SourcePage, sort: str
) -> tuple[str | None, tuple[int, int, int, int, int]]:
    item_count = len(page.items)
    empty_counts = (item_count, 0, 0, 0, 0)
    if not page.complete:
        return "mobile_incomplete", empty_counts
    if page.sort != sort:
        return "mobile_sort_mismatch", empty_counts
    if any(
        not post.has_fields({"publish_time"}) or not post.publish_time
        for post in page.items
    ):
        return "mobile_publish_time_missing", empty_counts
    times = [parse_publish_time(post.publish_time) for post in page.items]
    time_count = sum(value is not None for value in times)
    if time_count != item_count:
        return "mobile_publish_time_invalid", (
            item_count, time_count, 0, 0, 0
        )
    normalized = [value for value in times if value is not None]
    regression_ordinals = [
        ordinal
        for ordinal, (previous, current) in enumerate(
            zip(normalized, normalized[1:]), 1
        )
        if current > previous
    ]
    equal_count = sum(
        current == previous
        for previous, current in zip(normalized, normalized[1:])
    )
    counts = (
        item_count,
        time_count,
        len(regression_ordinals),
        equal_count,
        regression_ordinals[0] if regression_ordinals else 0,
    )
    reason = "mobile_order_regressed" if regression_ordinals else None
    return reason, counts


def _mobile_error_reason(error: SourceError) -> str:
    if isinstance(error, SourceChallengeError):
        return "mobile_challenge"
    if isinstance(error, SourceHTTPError):
        return "mobile_http"
    if isinstance(error, SourceTimeoutError):
        return "mobile_timeout"
    if isinstance(error, SourceRetryExhaustedError):
        return "mobile_retry_exhausted"
    if isinstance(error, SourceBusinessError):
        return "mobile_business"
    if isinstance(error, SourceSchemaError):
        return "mobile_schema"
    if isinstance(error, SourcePartialError):
        return "mobile_partial"
    if isinstance(error, SourceLimitError):
        return "mobile_limit"
    return "mobile_source_error"


def _set_mobile_fallback_reason(
    error: SourceError, reason: str | None
) -> None:
    error.mobile_fallback_reason = reason or "mobile_source_error"


def _tag_fallback_page(
    page: SourcePage, reason: str | None
) -> SourcePage:
    diagnostic = f"mobile_fallback:{reason or 'mobile_source_error'}"
    return replace(
        page,
        diagnostics=(*page.diagnostics, "fallback_dwr", diagnostic),
    )


def _validate_newest_first(posts: list[Post], restarted: bool) -> None:
    regressed = any(
        current.publish_time > previous.publish_time
        for previous, current in zip(posts, posts[1:])
    )
    if regressed:
        raise SourcePartialError(
            len(posts),
            0,
            reason="order_regressed_within_page",
            source="html_blog",
            restarted=restarted,
            page_count=1,
            unique_count=len({post.post_id for post in posts}),
        )


def _from_mobile(page, source: str) -> SourcePage:
    next_cursor = None
    if not page.exhausted:
        if page.next_cursor is None:
            raise SourceSchemaError("cursor")
        next_cursor = _encode_cursor(source, page.next_cursor)
    return SourcePage(
        items=page.items,
        source=source,
        next_cursor=next_cursor,
        exhausted=page.exhausted,
        sort=page.sort,
        mapped_count=page.mapped_count,
        dropped_count=page.dropped_count,
        complete=page.complete,
        evidence_items=tuple(getattr(page, "evidence_items", ())),
    )


def _mobile_identity_records(page) -> tuple[Post, ...]:
    records = tuple(getattr(page, "identity_records", ()))
    if records:
        return records
    return (*tuple(page.items), *tuple(getattr(page, "evidence_items", ())))


def _error_evidence(error: SourceError) -> tuple[Post, ...]:
    return tuple(getattr(error, "evidence_items", ()))


def _validated_fallback_post(
    post: Post, evidence: tuple[Post, ...]
) -> Post:
    validate_post_evidence((*evidence, post))
    return post


def _with_evidence(page: SourcePage, evidence: tuple[Post, ...]) -> SourcePage:
    if not evidence:
        return page
    return replace(page, evidence_items=(*page.evidence_items, *evidence))


def _finish_tag_fallback(
    primary: SourcePage | None,
    fallback: SourcePage,
    evidence: tuple[Post, ...],
    cursor: str | None,
) -> SourcePage:
    if cursor is not None and primary is None:
        if fallback.exhausted and not fallback.items:
            error = SourcePartialError(
                0,
                0,
                reason="evidence_shortfall",
                source="dwr",
                restarted=True,
                page_count=1,
                unique_count=0,
            )
            attach_source_evidence(error, evidence)
            raise error
    if primary is not None and fallback.exhausted and not fallback.items:
        return primary
    return _with_evidence(fallback, evidence)


def _raise_identity_error(error: SourceSchemaError) -> None:
    if error.location in IDENTITY_SCHEMA_LOCATIONS:
        raise error


def _validate_blog_page_owner(username: str, page: SourcePage) -> None:
    _validate_blog_posts_owner(username, (*page.evidence_items, *page.items))


def _validate_blog_posts_owner(
    username: str, posts: tuple[Post, ...] | list[Post]
) -> None:
    expected = username.casefold()
    for post in posts:
        owner = _post_owner(post)
        if not owner or owner.casefold() != expected:
            raise SourceSchemaError("post.owner")


def _post_owner(post: Post) -> str:
    owners: list[str] = []
    if post.has_fields({"url"}) and post.url:
        try:
            owners.append(post_url_identity(post.url)[2])
        except ValueError:
            raise SourceSchemaError("post.url") from None
    if post.has_fields({"author_username"}):
        owners.append(post.author_username)
    try:
        return consistent_blog_owner(*owners)
    except ValueError:
        raise SourceSchemaError("post.owner") from None


def _validate_post_error_evidence(
    request_url: str, post_id: str, error: SourceError
) -> None:
    for post in _error_evidence(error):
        _validate_post_owner(request_url, post_id, post)


def _validate_post_owner(request_url: str, post_id: str, post: Post) -> None:
    owners = [post_url_identity(request_url)[2]]
    if post.post_id != post_id:
        raise SourceSchemaError("post.id")
    if post.has_fields({"url"}) and post.url:
        try:
            _, response_id, owner = post_url_identity(post.url)
        except ValueError:
            raise SourceSchemaError("post.url") from None
        if response_id != post_id:
            raise SourceSchemaError("post.id")
        owners.append(owner)
    if post.has_fields({"author_username"}):
        owners.append(post.author_username)
    try:
        consistent_blog_owner(*owners)
    except ValueError:
        raise SourceSchemaError("post.owner") from None


def _post_identity(url: str) -> tuple[str, tuple[str, str] | None]:
    validate_text_bytes(url, "url", MAX_URL_BYTES)
    try:
        canonical = canonical_post_url(url)
    except ValueError:
        raise SourceSchemaError("url") from None
    post_id = post_id_from_url(canonical)
    return post_id, mobile_decimal_ids(post_id)


def _validate_username(username: str) -> None:
    if not isinstance(username, str) or not _DNS_LABEL.fullmatch(username):
        raise SourceSchemaError("blogName")


def _validate_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_ITEMS:
        raise SourceLimitError("items", MAX_ITEMS)


def _encode_cursor(source: str, value: str | int) -> str:
    return f"v1:{source}:{value}"


def _decode_cursor(
    cursor: str | None, default_source: str, allowed_sources: set[str]
) -> tuple[str, str | None]:
    if cursor is None:
        return default_source, None
    if not isinstance(cursor, str) or len(cursor) > 256:
        raise SourceSchemaError("cursor")
    parts = cursor.split(":", 2)
    if len(parts) != 3 or parts[0] != "v1":
        raise SourceSchemaError("cursor")
    if parts[1] not in allowed_sources or not parts[2]:
        raise SourceSchemaError("cursor")
    return parts[1], parts[2]


def _offset_value(value: str | None) -> int:
    text = value or "0"
    if not text.isdecimal():
        raise SourceSchemaError("cursor")
    return int(text)


__all__ = ["ContentSource", "DefaultContentSource", "SourcePage", "collect_pages"]
