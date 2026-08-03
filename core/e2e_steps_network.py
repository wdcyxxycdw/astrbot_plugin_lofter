from __future__ import annotations

from dataclasses import dataclass

from .errors import SourceError, SourcePartialError, SourceSchemaError
from .parser import Post
from .post_consumers import ensure_subscription_posts
from .post_fields import merge_post_fields, validate_post_evidence
from .post_time import parse_publish_time
from .source_scan import SourcePage, collect_pages

_MOBILE_REASONS = frozenset({
    "mobile_incomplete",
    "mobile_sort_mismatch",
    "mobile_publish_time_missing",
    "mobile_publish_time_invalid",
    "mobile_order_regressed",
    "mobile_http",
    "mobile_timeout",
    "mobile_retry_exhausted",
    "mobile_challenge",
    "mobile_business",
    "mobile_schema",
    "mobile_partial",
    "mobile_limit",
    "mobile_source_error",
    "mobile_cursor_restart",
})


@dataclass(frozen=True)
class FixtureBundle:
    baseline: Post
    candidate: Post


class NetworkStepsMixin:
    async def _step_01_runtime(self) -> object:
        key = "runtime"
        name = "运行时与隔离存储"
        started = self._timed_start()
        details: list[str] = []
        try:
            self._runtime = await self._create_runtime()
            details.append("临时 SQLite 已初始化")
            details.append("生产数据库未注入健康检查运行时")
            task = self._production_scheduler._task
            if task is None or task.done():
                raise RuntimeError("production scheduler is not running")
            if self._production_scheduler._interval <= 0:
                raise RuntimeError("production scheduler interval is invalid")
            details.append("生产 scheduler task 正在运行")
            return self._pass(
                key,
                name,
                self._timed_end(started),
                details,
                {"runtime_isolated": True},
            )
        except Exception as exc:
            return self._fail(
                key,
                name,
                self._timed_end(started),
                exc,
                details,
                facts={"runtime_isolated": self._runtime is not None},
            )

    async def _step_02_mobile_direct(self) -> object:
        key = "mobile_direct"
        name = "Mobile 标签直连"
        started = self._timed_start()
        details: list[str] = []
        diagnose = getattr(self._source, "diagnose_mobile_tag", None)
        if not callable(diagnose):
            return self._fail(
                key,
                name,
                self._timed_end(started),
                SourceSchemaError("response"),
                ["source 未提供 Mobile-only 诊断入口"],
                facts={
                    "mobile_eligible": False,
                    "mobile_fallback_reason": "mobile_source_error",
                },
            )
        try:
            diagnostic = await diagnose(self.TEST_TAG, 20, "new")
            reason = _safe_mobile_reason(diagnostic.fallback_reason)
            facts = {
                "mobile_eligible": reason is None,
                "mobile_fallback_reason": reason or "无",
            }
            if diagnostic.error is not None:
                return self._fail(
                    key,
                    name,
                    self._timed_end(started),
                    diagnostic.error,
                    [f"fallback={reason}"],
                    facts=facts,
                )
            page = diagnostic.page
            if page is None:
                raise SourceSchemaError("response")
            if page.source != "mobile_tag":
                raise SourceSchemaError("response")
            if reason is not None:
                return self._fail(
                    key,
                    name,
                    self._timed_end(started),
                    SourceSchemaError("response"),
                    [f"fallback={reason}"],
                    facts=facts,
                )
            _validate_page(page)
            self._artifacts["mobile_page"] = page
            details.append("source=mobile_tag")
            details.append(f"映射 {page.mapped_count}，丢弃 {page.dropped_count}")
            return self._pass(
                key,
                name,
                self._timed_end(started),
                details,
                facts,
            )
        except Exception as exc:
            return self._fail(
                key,
                name,
                self._timed_end(started),
                exc,
                details,
                facts={
                    "mobile_eligible": False,
                    "mobile_fallback_reason": _exception_mobile_reason(exc),
                },
            )

    async def _step_03_dwr_direct(self) -> object:
        key = "dwr_direct"
        name = "DWR 标签直连"
        started = self._timed_start()
        details: list[str] = []
        try:
            page = await self._source.list_tag(
                self.TEST_TAG, "v1:dwr:0", 20, "new"
            )
            _validate_page(page)
            if page.source != "dwr":
                raise SourceSchemaError("response")
            self._artifacts["dwr_page"] = page
            details.append("source=dwr")
            details.append(f"映射 {page.mapped_count}，丢弃 {page.dropped_count}")
            return self._pass(
                key,
                name,
                self._timed_end(started),
                details,
                {"dwr_verified": True},
            )
        except Exception as exc:
            return self._fail(
                key,
                name,
                self._timed_end(started),
                exc,
                details,
                facts={"dwr_verified": False},
            )

    async def _step_04_production_orchestration(self) -> object:
        key = "production_orchestration"
        name = "生产标签编排"
        started = self._timed_start()
        details: list[str] = []
        try:
            page = await collect_pages(
                lambda cursor: self._source.list_tag(
                    self.TEST_TAG, cursor, 20, "new"
                ),
                limit=20,
            )
            _validate_page(page)
            if page.source not in {"mobile_tag", "dwr"}:
                raise SourceSchemaError("response")
            reason = _page_mobile_reason(page)
            self._artifacts["production_page"] = page
            source = _safe_source(page.source)
            details.append(f"source={source}")
            details.append(f"restarted={'yes' if page.restarted else 'no'}")
            details.append(f"fallback={reason or '无'}")
            return self._pass(
                key,
                name,
                self._timed_end(started),
                details,
                {
                    "production_source": source,
                    "production_restarted": page.restarted,
                    "production_fallback_reason": reason or "无",
                },
            )
        except Exception as exc:
            reason = _exception_mobile_reason(exc)
            if reason == "无":
                reason = "mobile_source_error" if hasattr(
                    exc, "mobile_fallback_reason"
                ) else "无"
            return self._fail(
                key,
                name,
                self._timed_end(started),
                exc,
                details,
                facts={
                    "production_source": "未验证",
                    "production_restarted": False,
                    "production_fallback_reason": reason,
                },
            )

    async def _step_05_fixture_detail(self) -> object:
        key = "fixture_detail"
        name = "Fixture 与单帖补全"
        started = self._timed_start()
        details: list[str] = []
        provider = "未建立"
        try:
            selected = _select_fixture_candidates(self._artifacts)
            if selected is None:
                provider_pages = _provider_pages(self._artifacts)
                if not provider_pages:
                    blockers = self._dependency_blockers(
                        "production_orchestration", "mobile_direct", "dwr_direct"
                    )
                    return self._skip(key, name, blockers)
                return self._inconclusive(
                    key,
                    name,
                    self._timed_end(started),
                    ["健康来源合计不足两个不同帖子"],
                    {"fixture_provider": "未建立"},
                )
            provider, candidates, evidence = selected
            enriched: list[Post] = []
            for candidate in candidates:
                detail = await self._source.get_post(candidate.url)
                merged = merge_post_fields(candidate, detail)
                validate_post_evidence((*evidence, candidate, detail, merged))
                checked = await ensure_subscription_posts(
                    [merged], _MemoryPostSource(merged), {"images"}
                )
                post = checked[0]
                if not post.author_username:
                    raise SourceSchemaError("author")
                enriched.append(post)
            baseline, candidate = _order_fixture_posts(enriched)
            bundle = FixtureBundle(baseline, candidate)
            self._artifacts["fixture_bundle"] = bundle
            details.append("两个不同帖子已通过真实单帖入口补全")
            details.append(f"provider={provider}")
            return self._pass(
                key,
                name,
                self._timed_end(started),
                details,
                {"fixture_ready": True, "fixture_provider": provider},
            )
        except Exception as exc:
            return self._fail(
                key,
                name,
                self._timed_end(started),
                exc,
                details,
                facts={"fixture_ready": False, "fixture_provider": provider},
            )

    async def _step_06_blog(self) -> object:
        key = "blog"
        name = "博主页生产编排"
        started = self._timed_start()
        blockers = self._dependency_blockers("fixture_detail")
        if blockers:
            return self._skip(key, name, blockers)
        bundle = self._artifacts.get("fixture_bundle")
        if not isinstance(bundle, FixtureBundle):
            return self._skip(key, name, ("fixture_detail",))
        owner = bundle.baseline.author_username
        if not owner:
            return self._fail(
                key,
                name,
                self._timed_end(started),
                SourceSchemaError("author"),
                [],
            )
        try:
            page = await collect_pages(
                lambda cursor: self._source.list_blog(owner, cursor, 20),
                limit=20,
            )
            _validate_page(page)
            if page.source not in {"mobile_blog", "html_blog"}:
                raise SourceSchemaError("response")
            source = _safe_source(page.source)
            self._artifacts["blog_page"] = page
            return self._pass(
                key,
                name,
                self._timed_end(started),
                [f"source={source}", f"映射 {page.mapped_count}"],
                {"blog_source": source},
            )
        except Exception as exc:
            return self._fail(
                key,
                name,
                self._timed_end(started),
                exc,
                [],
            )


class _MemoryPostSource:
    def __init__(self, post: Post) -> None:
        self._post = post

    async def get_post(self, url: str) -> Post:
        if url != self._post.url:
            raise SourceSchemaError("post.url")
        return self._post


def _validate_page(page: SourcePage) -> None:
    if not isinstance(page, SourcePage):
        raise SourceSchemaError("response")
    if not page.complete:
        raise SourcePartialError(page.mapped_count, page.dropped_count)
    if page.sort != "new":
        raise SourceSchemaError("sort")


def _provider_pages(
    artifacts: dict[str, object]
) -> list[tuple[str, SourcePage]]:
    providers = (
        ("production", "production_page"),
        ("mobile", "mobile_page"),
        ("dwr", "dwr_page"),
    )
    return [
        (name, page)
        for name, artifact in providers
        if isinstance((page := artifacts.get(artifact)), SourcePage)
    ]


def _select_fixture_candidates(
    artifacts: dict[str, object]
) -> tuple[str, list[Post], tuple[Post, ...]] | None:
    pages = _provider_pages(artifacts)
    for provider, page in pages:
        posts = _unique_posts([page])
        if len(posts) >= 2:
            validate_post_evidence((*page.evidence_items, *page.items))
            return provider, posts[:2], (*page.evidence_items, *page.items)
    if not pages:
        return None
    all_pages = [page for _, page in pages]
    observed = tuple(
        post
        for page in all_pages
        for post in (*page.evidence_items, *page.items)
    )
    validate_post_evidence(observed)
    posts = _unique_posts(all_pages)
    if len(posts) < 2:
        return None
    return "combined", posts[:2], observed


def _order_fixture_posts(posts: list[Post]) -> tuple[Post, Post]:
    if len(posts) != 2:
        raise SourceSchemaError("response")
    ordered = sorted(
        posts,
        key=lambda post: parse_publish_time(post.publish_time) or -1,
    )
    if parse_publish_time(ordered[0].publish_time) is None:
        raise SourceSchemaError("publish_time")
    if parse_publish_time(ordered[1].publish_time) is None:
        raise SourceSchemaError("publish_time")
    return ordered[0], ordered[1]


def _unique_posts(pages: list[SourcePage]) -> list[Post]:
    posts: dict[str, Post] = {}
    for page in pages:
        for post in page.items:
            if not post.post_id or not post.url:
                continue
            current = posts.get(post.post_id)
            posts[post.post_id] = (
                post if current is None else merge_post_fields(current, post)
            )
    return list(posts.values())


def _page_mobile_reason(page: SourcePage) -> str | None:
    for diagnostic in page.diagnostics:
        prefix = "mobile_fallback:"
        if diagnostic.startswith(prefix):
            return _safe_mobile_reason(diagnostic[len(prefix):])
    return None


def _exception_mobile_reason(error: BaseException) -> str:
    return _safe_mobile_reason(
        getattr(error, "mobile_fallback_reason", None)
    ) or "无"


def _safe_mobile_reason(value: object) -> str | None:
    return value if isinstance(value, str) and value in _MOBILE_REASONS else None


def _safe_source(value: str) -> str:
    known = {"dwr", "html_blog", "mobile_blog", "mobile_tag"}
    return value if value in known else "unknown"
