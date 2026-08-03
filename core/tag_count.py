from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Callable, Iterable, Literal

from .count_formatters import (
    build_count_csv,
    build_count_csv_path,
    format_count_result,
    format_scanned_pages,
)
from .count_scanner import TagScanResult, scan_tags
from .errors import SourceSchemaError
from .expression_planner import (
    BinaryNode,
    CountExpressionError,
    ExprNode,
    TagNode,
    UnaryNode,
    minimum_cover_alternatives,
    parse_count_expression,
)
from .parser import Post
from .post_fields import PostEvidenceLedger
from .post_identity import consistent_blog_owner, post_url_identity
from .source_scan import ContentSource, SCAN_DEADLINE_SECONDS

logger = logging.getLogger(__name__)
CountStatus = Literal["success", "partial", "failed"]


@dataclass(frozen=True)
class CountResult:
    name: str
    expression: str
    count: int
    status: str
    error: str
    counted_at: str
    candidates: int = 0
    scanned_pages: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CoverResult:
    tags: frozenset[str]
    candidate_ids: set[str]
    matched_ids: set[str]
    scanned_pages: dict[str, int]
    warnings: list[str]
    complete: bool
    reliable: bool


def parse_count_command_arg(raw: str) -> tuple[str, str]:
    normalized = raw.replace("＝", "=")
    if "=" not in normalized:
        raise CountExpressionError("请使用：名称 = 表达式")
    name, expression = (
        part.strip() for part in normalized.split("=", 1)
    )
    if not name or not expression:
        raise CountExpressionError("请使用：名称 = 表达式")
    return name, expression


def match_expression(expr: ExprNode, post: Post) -> bool:
    if not post.has_fields({"tags"}):
        raise SourceSchemaError("tags")
    return _match(expr, {tag.casefold() for tag in post.tags})


def _match(expr: ExprNode, tags: set[str]) -> bool:
    if isinstance(expr, TagNode):
        return expr.tag.casefold() in tags
    if isinstance(expr, UnaryNode):
        return not _match(expr.child, tags)
    if expr.op == "and":
        return _match(expr.left, tags) and _match(expr.right, tags)
    return _match(expr.left, tags) or _match(expr.right, tags)


def extract_positive_tags(expr: ExprNode) -> list[str]:
    result: list[str] = []
    _collect_positive(expr, False, set(), result)
    return result


def _collect_positive(
    expr: ExprNode,
    negated: bool,
    seen: set[str],
    result: list[str],
) -> None:
    if isinstance(expr, TagNode):
        key = expr.tag.casefold()
        if not negated and key not in seen:
            seen.add(key)
            result.append(expr.tag)
        return
    if isinstance(expr, UnaryNode):
        _collect_positive(expr.child, not negated, seen, result)
        return
    _collect_positive(expr.left, negated, seen, result)
    _collect_positive(expr.right, negated, seen, result)


async def count_posts(
    expression: str,
    source: ContentSource,
    *,
    page_size: int = 20,
    tag_concurrency: int = 5,
    _deadline: float = SCAN_DEADLINE_SECONDS,
    _monotonic: Callable[[], float] = time.monotonic,
) -> CountResult:
    expr = parse_count_expression(expression)
    positives = extract_positive_tags(expr)
    aliases = _tag_aliases(expr)
    if not positives:
        return _empty_failed_result(
            expression,
            "表达式无法由正向标签提供可靠扫描证据",
        )
    planned = minimum_cover_alternatives(expr)
    if planned == (frozenset(),):
        return _exact_zero_result(expression)
    attempts = planned or (frozenset(positives),)
    deadline_at = _monotonic() + max(0.0, _deadline)
    scans = await scan_tags(
        _cover_tags(attempts, aliases),
        source,
        page_size,
        lambda post: match_expression(expr, post),
        tag_concurrency,
        deadline_at,
        _monotonic,
    )
    _validate_owner_evidence(scans.values())
    conflicts = _conflicting_post_evidence(scans.values())
    results = [
        _evaluate_cover(cover, scans, bool(planned), conflicts)
        for cover in attempts
    ]
    selected = _select_cover(results)
    _log_attempts(expression, results, selected)
    return _build_result(expression, selected)


def _tag_aliases(expr: ExprNode) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    _collect_tag_aliases(expr, result)
    return result


def _collect_tag_aliases(
    expr: ExprNode, result: dict[str, list[str]]
) -> None:
    if isinstance(expr, TagNode):
        aliases = result.setdefault(expr.tag.casefold(), [])
        if expr.tag not in aliases:
            aliases.append(expr.tag)
        return
    if isinstance(expr, UnaryNode):
        _collect_tag_aliases(expr.child, result)
        return
    _collect_tag_aliases(expr.left, result)
    _collect_tag_aliases(expr.right, result)


def _cover_tags(
    covers: tuple[frozenset[str], ...], aliases: dict[str, list[str]]
) -> list[str]:
    result: list[str] = []
    included: set[str] = set()
    for cover in covers:
        for tag in sorted(cover):
            key = tag.casefold()
            if key in included:
                continue
            included.add(key)
            result.extend(aliases.get(key, [tag]))
    return result


def _validate_owner_evidence(scans: Iterable[TagScanResult]) -> None:
    evidence: dict[str, str] = {}
    for scan in scans:
        for post_id, owner in scan.owner_evidence.items():
            try:
                resolved = consistent_blog_owner(evidence.get(post_id, ""), owner)
            except ValueError:
                raise SourceSchemaError("post.owner") from None
            if resolved:
                evidence[post_id] = resolved


def _conflicting_post_evidence(
    scans: Iterable[TagScanResult],
) -> set[str]:
    values = list(scans)
    ledger = PostEvidenceLedger()
    for scan in values:
        ledger.merge(scan.evidence, collect_conflicts=True)
    conflicts = set(ledger.conflicted_ids)
    conflicts.update(*(scan.conflicted_ids for scan in values))
    conflicts.update(_conflicting_evidence_field(values, "tag_evidence"))
    conflicts.update(
        _conflicting_evidence_field(values, "publish_time_evidence")
    )
    conflicts.update(_conflicting_url_evidence(values))
    return conflicts


def _conflicting_url_evidence(scans: list[TagScanResult]) -> set[str]:
    evidence: dict[str, str] = {}
    conflicts: set[str] = set()
    for scan in scans:
        for post_id, value in scan.url_evidence.items():
            existing = evidence.get(post_id)
            if existing is None or existing == value:
                evidence[post_id] = value
                continue
            old_owner = _url_owner(existing)
            new_owner = _url_owner(value)
            if bool(old_owner) != bool(new_owner):
                evidence[post_id] = value if new_owner else existing
            else:
                conflicts.add(post_id)
    return conflicts


def _url_owner(url: str) -> str:
    try:
        return post_url_identity(url)[2]
    except ValueError:
        raise SourceSchemaError("post.url") from None


def _conflicting_evidence_field(
    scans: list[TagScanResult], field_name: str
) -> set[str]:
    evidence: dict[str, object] = {}
    conflicts: set[str] = set()
    for scan in scans:
        for post_id, value in getattr(scan, field_name).items():
            existing = evidence.setdefault(post_id, value)
            if existing != value:
                conflicts.add(post_id)
    return conflicts


def _evaluate_cover(
    cover: frozenset[str],
    scans: dict[str, TagScanResult],
    enumerable: bool,
    global_conflicts: set[str],
) -> CoverResult:
    selected = [scans[tag.casefold()] for tag in cover]
    candidates: set[str] = set()
    matched: set[str] = set()
    pages: dict[str, int] = {}
    warnings: list[str] = []
    for scan in selected:
        candidates.update(scan.candidate_ids)
        matched.update(scan.matched_ids)
        pages[scan.tag] = scan.scanned_pages
        warnings.extend(scan.warnings)
    if any("重复作品字段冲突" in warning for warning in warnings):
        if "重复作品字段冲突" not in warnings:
            warnings.append("重复作品字段冲突")
    matched.difference_update(global_conflicts)
    if global_conflicts and "重复作品字段冲突" not in warnings:
        warnings.append("重复作品字段冲突")
    return CoverResult(
        tags=cover,
        candidate_ids=candidates,
        matched_ids=matched,
        scanned_pages=pages,
        warnings=warnings,
        complete=bool(selected) and not global_conflicts and enumerable and all(
            scan.complete for scan in selected
        ),
        reliable=any(scan.reliable for scan in selected),
    )


def _select_cover(results: list[CoverResult]) -> CoverResult:
    reliable = [result for result in results if result.reliable]
    proven_ids: set[str] = set()
    for result in reliable:
        proven_ids.update(result.matched_ids)
    complete = [
        result for result in results
        if result.complete and proven_ids <= result.matched_ids
    ]
    if complete:
        return min(complete, key=_success_score)
    if reliable:
        selected = max(reliable, key=_partial_score)
        warnings = selected.warnings
        complete = selected.complete
        if complete:
            complete = False
            warnings = [
                *warnings,
                "完整 cover 未覆盖其他可靠扫描证据",
            ]
        return replace(
            selected,
            matched_ids=proven_ids,
            complete=complete,
            warnings=warnings,
        )
    return min(results, key=_failed_score)


def _success_score(
    result: CoverResult,
) -> tuple[int, int, int, tuple[str, ...]]:
    return (
        sum(result.scanned_pages.values()),
        len(result.candidate_ids),
        len(result.tags),
        tuple(sorted(result.tags)),
    )


def _partial_score(
    result: CoverResult,
) -> tuple[int, int, int, int]:
    return (
        len(result.matched_ids),
        len(result.candidate_ids),
        -sum(result.scanned_pages.values()),
        -len(result.tags),
    )


def _failed_score(
    result: CoverResult,
) -> tuple[int, tuple[str, ...]]:
    return len(result.tags), tuple(sorted(result.tags))


def _build_result(
    expression: str, cover: CoverResult
) -> CountResult:
    status, error = _result_status(cover)
    return CountResult(
        name="",
        expression=expression,
        count=len(cover.matched_ids),
        status=status,
        error=error,
        counted_at=_now_text(),
        candidates=len(cover.candidate_ids),
        scanned_pages=cover.scanned_pages,
        warnings=cover.warnings,
    )


def _exact_zero_result(expression: str) -> CountResult:
    return CountResult(
        name="",
        expression=expression,
        count=0,
        status="success",
        error="",
        counted_at=_now_text(),
    )


def _empty_failed_result(
    expression: str, error: str
) -> CountResult:
    return CountResult(
        name="",
        expression=expression,
        count=0,
        status="failed",
        error=error,
        counted_at=_now_text(),
    )


def _result_status(
    cover: CoverResult,
) -> tuple[CountStatus, str]:
    if cover.complete:
        return "success", ""
    if cover.reliable:
        return "partial", "没有完整 cover 扫描完成"
    return "failed", "没有获得可靠扫描证据"


def _log_attempts(
    expression: str,
    results: list[CoverResult],
    selected: CoverResult,
) -> None:
    attempts = [
        {
            "tags": sorted(item.tags),
            "complete": item.complete,
            "reliable": item.reliable,
            "candidates": len(item.candidate_ids),
        }
        for item in results
    ]
    logger.info(
        "count expression=%r attempts=%r selected=%r",
        expression,
        attempts,
        sorted(selected.tags),
    )


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


__all__ = [
    "BinaryNode",
    "CountExpressionError",
    "CountResult",
    "ExprNode",
    "TagNode",
    "UnaryNode",
    "build_count_csv",
    "build_count_csv_path",
    "count_posts",
    "extract_positive_tags",
    "format_count_result",
    "format_scanned_pages",
    "match_expression",
    "parse_count_command_arg",
    "parse_count_expression",
]
