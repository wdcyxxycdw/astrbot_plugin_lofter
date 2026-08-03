from __future__ import annotations

from .errors import SourcePartialError, SourceSchemaError
from .parser import Post
from .post_consumers import ensure_subscription_posts
from .post_fields import merge_post_fields
from .source_scan import SourcePage


class NetworkStepsMixin:
    async def _step_01_runtime(self) -> object:
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
                name,
                self._timed_end(started),
                details,
                {"runtime_isolated": True},
            )
        except Exception as exc:
            return self._fail(
                name,
                self._timed_end(started),
                exc,
                details,
                facts={"runtime_isolated": self._runtime is not None},
            )

    async def _step_02_normal_tag(self) -> object:
        name = "真实标签来源"
        started = self._timed_start()
        details: list[str] = []
        try:
            page = await self._source.list_tag(
                self.TEST_TAG, None, 20, "new"
            )
            _validate_page(page)
            self._artifacts["normal_page"] = page
            source = _safe_source(page.source)
            details.append(f"source={source}")
            details.append(
                f"映射 {page.mapped_count}，丢弃 {page.dropped_count}"
            )
            return self._pass(
                name,
                self._timed_end(started),
                details,
                {"normal_source": source},
            )
        except Exception as exc:
            return self._fail(
                name,
                self._timed_end(started),
                exc,
                details,
            )

    async def _step_03_forced_dwr(self) -> object:
        name = "真实 DWR fallback"
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
            details.append(
                f"映射 {page.mapped_count}，丢弃 {page.dropped_count}"
            )
            return self._pass(
                name,
                self._timed_end(started),
                details,
                {"dwr_verified": True},
            )
        except Exception as exc:
            return self._fail(
                name,
                self._timed_end(started),
                exc,
                details,
                facts={"dwr_verified": False},
            )

    async def _step_04_live_fixture(self) -> object:
        name = "实时 fixture、单帖与博主页"
        started = self._timed_start()
        pages = _available_pages(self._artifacts)
        if not pages:
            return self._skip(name, "标签来源未就绪，无法建立实时 fixture")
        candidates = _unique_posts(pages)
        if len(candidates) < 2:
            return self._inconclusive(
                name,
                self._timed_end(started),
                ["实时结果不足两个不同帖子"],
            )
        details: list[str] = []
        try:
            enriched = []
            for candidate in candidates[:2]:
                detail = await self._source.get_post(candidate.url)
                merged = merge_post_fields(candidate, detail)
                values = await ensure_subscription_posts(
                    [merged], self._source, {"images"}
                )
                enriched.append(values[0])
            owner = enriched[0].author_username
            if not owner:
                raise SourceSchemaError("author")
            blog_page = await self._source.list_blog(owner, None, 20)
            _validate_page(blog_page)
            self._artifacts["baseline"] = enriched[0]
            self._artifacts["candidate"] = enriched[1]
            self._artifacts["blog_page"] = blog_page
            details.append("两个不同帖子已通过真实单帖入口补全")
            details.append(
                f"博主页 source={_safe_source(blog_page.source)}，"
                f"映射 {blog_page.mapped_count}"
            )
            return self._pass(
                name,
                self._timed_end(started),
                details,
                {"fixture_ready": True},
            )
        except Exception as exc:
            return self._fail(
                name,
                self._timed_end(started),
                exc,
                details,
                facts={"fixture_ready": False},
            )


def _validate_page(page: SourcePage) -> None:
    if not isinstance(page, SourcePage):
        raise SourceSchemaError("response")
    if not page.complete:
        raise SourcePartialError(page.mapped_count, page.dropped_count)
    if page.sort != "new":
        raise SourceSchemaError("sort")


def _available_pages(artifacts: dict[str, object]) -> list[SourcePage]:
    return [
        page
        for key in ("normal_page", "dwr_page")
        if isinstance((page := artifacts.get(key)), SourcePage)
    ]


def _unique_posts(pages: list[SourcePage]) -> list[Post]:
    posts: dict[str, Post] = {}
    for page in pages:
        for post in page.items:
            if post.post_id and post.url:
                posts.setdefault(post.post_id, post)
    return list(posts.values())


def _safe_source(value: str) -> str:
    known = {
        "dwr",
        "html_blog",
        "mobile_blog",
        "mobile_tag",
    }
    return value if value in known else "unknown"
