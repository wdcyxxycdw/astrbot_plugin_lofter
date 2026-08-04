from __future__ import annotations

import asyncio
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Literal

from .author_block import AuthorBlockStorage
from .db import LofterDB
from .delivery import DeliveryQueue, DiscoveryResult
from .e2e_steps_flow import FlowStepsMixin
from .e2e_steps_network import FixtureBundle, NetworkStepsMixin
from .errors import (
    DWREvidenceError,
    DWRIdentityError,
    PostEvidenceError,
    SourceChallengeError,
    SourceError,
    SourceHTTPError,
    SourceRetryExhaustedError,
    SourceSchemaError,
    SourceTimeoutError,
)
from .parser import Post
from .scheduler import SendFunc, SubscriptionScheduler
from .session_gate import SessionGateRegistry
from .source_scan import ContentSource, SourcePage
from .storage import SubscriptionStorage
from .subscription_service import SubscriptionService

Health = Literal["healthy", "degraded", "inconclusive"]


@dataclass
class StepResult:
    name: str
    status: Literal["pass", "fail", "skip"]
    key: str = ""
    duration_ms: int = 0
    details: list[str] = field(default_factory=list)
    error: str | None = None
    health: Health = "healthy"
    facts: dict[str, str | int | bool] = field(default_factory=dict)
    blocked_by: tuple[str, ...] = ()


class _ControlledSource:
    def __init__(self, tag: str) -> None:
        self._tag = tag
        self._posts: dict[str, Post] = {}
        self._post: Post | None = None

    def install(self, bundle: FixtureBundle) -> None:
        self._posts = {
            bundle.baseline.url: bundle.baseline,
            bundle.candidate.url: bundle.candidate,
        }
        self._post = None

    def use(self, post: Post) -> None:
        if self._posts.get(post.url) is not post:
            raise SourceSchemaError("post.url")
        self._post = post

    async def get_post(self, url: str) -> Post:
        post = self._posts.get(url)
        if post is None:
            raise SourceSchemaError("post.url")
        return post

    async def list_blog(
        self, username: str, cursor: str | None, limit: int
    ) -> SourcePage:
        raise SourceSchemaError("blog")

    async def list_tag(
        self, tag: str, cursor: str | None, limit: int, sort: str
    ) -> SourcePage:
        if tag != self._tag or cursor is not None or sort != "new":
            raise SourceSchemaError("response")
        items = [self._post] if self._post is not None else []
        return SourcePage(
            items=items,
            source="e2e_controlled",
            next_cursor=None,
            exhausted=True,
            sort="new",
            mapped_count=len(items),
            dropped_count=0,
            complete=True,
        )


class _ObservingDeliveryQueue(DeliveryQueue):
    def __init__(self, db: LofterDB, gates: SessionGateRegistry) -> None:
        super().__init__(db, gates)
        self.persist_ready = asyncio.Event()
        self.release_claim = asyncio.Event()
        self.discovery_result: DiscoveryResult | None = None
        self.persist_error: BaseException | None = None

    async def persist_discovery(self, snapshot, batches):
        try:
            result = await super().persist_discovery(snapshot, batches)
        except BaseException as exc:
            self.persist_error = exc
            self.persist_ready.set()
            raise
        self.discovery_result = result
        self.persist_ready.set()
        await self.release_claim.wait()
        return result


@dataclass
class _E2ERuntime:
    temporary: tempfile.TemporaryDirectory[str]
    db: LofterDB
    source: _ControlledSource
    storage: SubscriptionStorage
    subscriptions: SubscriptionService
    queue: _ObservingDeliveryQueue
    scheduler: SubscriptionScheduler
    session_id: str = "__lofter_e2e_health__"


class E2ETestRunner(NetworkStepsMixin, FlowStepsMixin):
    TEST_TAG = "摄影"
    WAIT_SECONDS = 10
    POLL_SECONDS = 70
    STEP_ORDER = (
        "runtime",
        "mobile_direct",
        "dwr_direct",
        "production_orchestration",
        "fixture_detail",
        "blog",
        "warmup_pending",
        "claim_send_ack_seen",
        "cleanup",
    )

    def __init__(
        self,
        source: ContentSource,
        scheduler: SubscriptionScheduler,
        send_push: SendFunc,
    ) -> None:
        self._source = source
        self._production_scheduler = scheduler
        self._send_push = send_push
        self._runtime: _E2ERuntime | None = None
        self._real_session_id = ""
        self._poll_task: asyncio.Task | None = None
        self._bridge_task: asyncio.Task | None = None
        self._send_entered = asyncio.Event()
        self._release_send = asyncio.Event()
        self._send_entries = 0
        self._send_attempts = 0
        self._send_result: bool | None = None
        self._send_error: BaseException | None = None
        self._artifacts: dict[str, object] = {}
        self._results_by_key: dict[str, StepResult] = {}
        self._cleanup_complete = False

    async def run_all(self, real_session_id: str) -> list[StepResult]:
        self._real_session_id = real_session_id
        results: list[StepResult] = []
        try:
            for step in (
                self._step_01_runtime,
                self._step_02_mobile_direct,
                self._step_03_dwr_direct,
                self._step_04_production_orchestration,
                self._step_05_fixture_detail,
                self._step_06_blog,
                self._step_07_warmup_pending,
                self._step_08_claim_send_ack_seen,
            ):
                result = await step()
                results.append(result)
                self._results_by_key[result.key] = result
            return results
        finally:
            cleanup = await self._cleanup()
            results.append(cleanup)
            self._results_by_key[cleanup.key] = cleanup

    async def _create_runtime(self) -> _E2ERuntime:
        temporary = tempfile.TemporaryDirectory(prefix="lofter-e2e-")
        db = LofterDB(os.path.join(temporary.name, "health.db"))
        try:
            await db.initialize()
        except BaseException:
            try:
                await db.close()
            finally:
                temporary.cleanup()
            raise
        gates = SessionGateRegistry()
        source = _ControlledSource(self.TEST_TAG)
        storage = SubscriptionStorage(db)
        blocks = AuthorBlockStorage(db, gates)
        subscriptions = SubscriptionService(db, source, gates)
        queue = _ObservingDeliveryQueue(db, gates)
        scheduler = SubscriptionScheduler(
            storage,
            source,
            db,
            self._send_bridge,
            block_storage=blocks,
            gates=gates,
            subscription_service=subscriptions,
            delivery_queue=queue,
        )
        return _E2ERuntime(
            temporary, db, source, storage, subscriptions, queue, scheduler
        )

    async def _send_bridge(
        self,
        session_id: str,
        post: Post,
        header: str,
        source_types: frozenset[str],
    ) -> bool:
        self._send_entries += 1
        entry = self._send_entries
        self._bridge_task = asyncio.current_task()
        self._send_entered.set()
        await self._release_send.wait()
        if entry > 1:
            return False
        self._send_attempts += 1
        try:
            result = await self._send_push(
                self._real_session_id,
                post,
                f"【Lofter E2E 测试】{header}",
                source_types,
            )
        except BaseException as exc:
            self._send_error = exc
            raise
        self._send_result = result is True
        return result is True

    async def _wait_event_or_poll(self, event: asyncio.Event) -> bool:
        if self._poll_task is None:
            return False
        waiter = asyncio.create_task(event.wait())
        try:
            done, _ = await asyncio.wait(
                (waiter, self._poll_task),
                timeout=self.WAIT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            return waiter in done and event.is_set()
        finally:
            if not waiter.done():
                waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    async def _delivery_row(self) -> tuple | None:
        runtime = self._runtime
        bundle = self._artifacts.get("fixture_bundle")
        if runtime is None or not isinstance(bundle, FixtureBundle):
            return None
        return await runtime.db.transaction(
            lambda conn: conn.execute(
                """
                SELECT d.status,d.lease_token,d.attempts,COUNT(ds.delivery_id)
                FROM deliveries d
                LEFT JOIN delivery_sources ds ON ds.delivery_id=d.id
                WHERE d.session_id=? AND d.post_id=?
                GROUP BY d.id
                """,
                (runtime.session_id, bundle.candidate.post_id),
            ).fetchone()
        )

    async def _candidate_seen(self) -> bool:
        runtime = self._runtime
        bundle = self._artifacts.get("fixture_bundle")
        if runtime is None or not isinstance(bundle, FixtureBundle):
            return False
        row = await runtime.db.transaction(
            lambda conn: conn.execute(
                """
                SELECT 1 FROM seen_posts sp
                JOIN subscriptions s ON s.id=sp.subscription_id
                WHERE s.session_id=? AND s.type='tag' AND s.role='subscribe'
                  AND sp.post_id=?
                """,
                (runtime.session_id, bundle.candidate.post_id),
            ).fetchone()
        )
        return row is not None

    def _dependency_blockers(self, *keys: str) -> tuple[str, ...]:
        blockers: set[str] = set()
        for key in keys:
            result = self._results_by_key.get(key)
            if result is not None and result.status == "pass":
                continue
            if result is not None and result.status == "skip" and result.blocked_by:
                blockers.update(result.blocked_by)
            else:
                blockers.add(key)
        return tuple(key for key in self.STEP_ORDER if key in blockers)

    def _timed_start(self) -> float:
        return time.perf_counter()

    def _timed_end(self, started: float) -> int:
        return int((time.perf_counter() - started) * 1000)

    def _pass(
        self,
        key: str,
        name: str,
        ms: int,
        details: list[str],
        facts: dict[str, str | int | bool] | None = None,
    ) -> StepResult:
        return StepResult(
            key=key,
            name=name,
            status="pass",
            duration_ms=ms,
            details=details,
            facts=facts or {},
        )

    def _fail(
        self,
        key: str,
        name: str,
        ms: int,
        error: BaseException,
        details: list[str],
        *,
        health: Health | None = None,
        facts: dict[str, str | int | bool] | None = None,
    ) -> StepResult:
        return StepResult(
            key=key,
            name=name,
            status="fail",
            duration_ms=ms,
            details=details,
            error=_safe_error(error),
            health=health or _error_health(error),
            facts=facts or {},
        )

    def _skip(
        self, key: str, name: str, blocked_by: tuple[str, ...]
    ) -> StepResult:
        return StepResult(
            key=key,
            name=name,
            status="skip",
            details=["依赖步骤未就绪"],
            health="inconclusive",
            blocked_by=blocked_by,
        )

    def _inconclusive(
        self,
        key: str,
        name: str,
        ms: int,
        details: list[str],
        facts: dict[str, str | int | bool] | None = None,
    ) -> StepResult:
        return StepResult(
            key=key,
            name=name,
            status="fail",
            duration_ms=ms,
            details=details,
            health="inconclusive",
            facts=facts or {},
        )

    async def _cleanup(self) -> StepResult:
        key = "cleanup"
        name = "临时资源清理"
        started = self._timed_start()
        runtime = self._runtime
        if runtime is None:
            self._cleanup_complete = True
            facts = _cleanup_facts(True, True, True)
            return self._pass(key, name, self._timed_end(started), ["未创建临时 SQLite"], facts)

        tasks_cancelled = True
        db_closed = True
        temp_dir_cleaned = True
        failed_phases: list[str] = []
        runtime.queue.release_claim.set()
        self._release_send.set()
        tasks = [
            task
            for task in (self._poll_task, self._bridge_task)
            if task is not None
        ]
        try:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        except BaseException:
            tasks_cancelled = False
            failed_phases.append("tasks")

        try:
            await runtime.db.close()
        except BaseException:
            db_closed = False
            failed_phases.append("db")

        path = runtime.temporary.name
        try:
            runtime.temporary.cleanup()
            temp_dir_cleaned = not os.path.exists(path)
        except BaseException:
            temp_dir_cleaned = False
        if not temp_dir_cleaned:
            failed_phases.append("temp-dir")

        facts = _cleanup_facts(tasks_cancelled, db_closed, temp_dir_cleaned)
        self._cleanup_complete = bool(facts["temp_db_cleaned"] and tasks_cancelled)
        details = [
            f"tasks={'ok' if tasks_cancelled else 'failed'}",
            f"db={'ok' if db_closed else 'failed'}",
            f"temp-dir={'ok' if temp_dir_cleaned else 'failed'}",
        ]
        if failed_phases:
            return self._fail(
                key,
                name,
                self._timed_end(started),
                RuntimeError("cleanup failed"),
                details,
                health="degraded",
                facts=facts,
            )
        return self._pass(key, name, self._timed_end(started), details, facts)


_TEMPORARY_ERRORS = (
    asyncio.TimeoutError,
    OSError,
    SourceChallengeError,
    SourceHTTPError,
    SourceRetryExhaustedError,
    SourceTimeoutError,
)


def _cleanup_facts(
    tasks_cancelled: bool, db_closed: bool, temp_dir_cleaned: bool
) -> dict[str, bool]:
    return {
        "tasks_cancelled": tasks_cancelled,
        "db_closed": db_closed,
        "temp_dir_cleaned": temp_dir_cleaned,
        "temp_db_cleaned": db_closed and temp_dir_cleaned,
    }


def _error_health(error: BaseException) -> Health:
    if isinstance(error, _TEMPORARY_ERRORS):
        return "inconclusive"
    return "degraded"


def _safe_error(error: BaseException) -> str:
    if isinstance(error, DWREvidenceError):
        return f"DWR 证据冲突（{error.diagnostic}）"
    if isinstance(error, DWRIdentityError):
        return f"DWR 身份冲突（{error.diagnostic}）"
    if isinstance(error, PostEvidenceError):
        return f"帖子证据冲突（{error.diagnostic}）"
    if isinstance(error, SourceSchemaError):
        return f"内容源响应结构无效（{error.location}）"
    if isinstance(error, SourceError):
        return str(error)
    if isinstance(error, asyncio.TimeoutError):
        return "操作超时"
    return type(error).__name__


_STATUS_ICON = {"pass": "✓", "fail": "✗", "skip": "○"}
_HEALTH_LABEL = {
    "healthy": "HEALTHY",
    "degraded": "DEGRADED",
    "inconclusive": "INCONCLUSIVE",
}


def _overall_health(results: list[StepResult]) -> Health:
    if any(result.health == "degraded" for result in results):
        return "degraded"
    if any(result.health == "inconclusive" for result in results):
        return "inconclusive"
    return "healthy"


def _fact(results: list[StepResult], key: str, default):
    for result in results:
        if key in result.facts:
            return result.facts[key]
    return default


def _label_bool(value: object) -> str:
    return {True: "是", False: "否", "unknown": "未知"}.get(value, "未知")


def format_report(results: list[StepResult]) -> str:
    health = _overall_health(results)
    mobile = _label_bool(_fact(results, "mobile_eligible", "unknown"))
    mobile_reason = _fact(results, "mobile_fallback_reason", "无")
    dwr = "已真实验证" if _fact(results, "dwr_verified", False) else "未验证"
    production_source = _fact(results, "production_source", "未验证")
    production_restarted = _label_bool(
        _fact(results, "production_restarted", "unknown")
    )
    production_reason = _fact(results, "production_fallback_reason", "无")
    production_partial = _fact(results, "production_partial_reason", "无")
    production_pages = _fact(results, "production_page_count", 0)
    production_unique = _fact(results, "production_unique_count", 0)
    production_evidence = _fact(results, "production_evidence_count", 0)
    provider = _fact(results, "fixture_provider", "未建立")
    attempts = _fact(results, "send_attempts", 0)
    accepted = _label_bool(_fact(results, "adapter_accepted", "unknown"))
    tasks = _label_bool(_fact(results, "tasks_cancelled", False))
    db = _label_bool(_fact(results, "db_closed", False))
    temp_dir = _label_bool(_fact(results, "temp_dir_cleaned", False))

    production_line = (
        "生产标签编排："
        f"source={production_source}，"
        f"restarted={production_restarted}，"
        f"fallback={production_reason}，"
        f"partial={production_partial}"
    )
    if production_partial not in {"无", "unknown"}:
        production_line += (
            f"，pages={production_pages}，"
            f"unique={production_unique}，"
            f"evidence={production_evidence}"
        )

    lines = [
        "━━━ Lofter 实时健康检查 ━━━",
        f"总体状态：{_HEALTH_LABEL[health]}",
        f"Mobile：eligible={mobile}，fallback={mobile_reason}",
        f"DWR：{dwr}",
        production_line,
        f"Fixture：provider={provider}",
        f"真实发送：尝试 {attempts}，adapter accepted={accepted}",
        f"清理：tasks={tasks}，db={db}，temp-dir={temp_dir}",
        "",
    ]
    total = len(results)
    for index, result in enumerate(results, 1):
        icon = _STATUS_ICON.get(result.status, "?")
        suffix = "" if result.status == "skip" else f" ({result.duration_ms} ms)"
        lines.append(f"[{index}/{total} {result.key}] {icon} {result.name}{suffix}")
        for detail in result.details:
            lines.append(f"  · {detail}")
        if result.blocked_by:
            lines.append(f"  · 阻断：{', '.join(result.blocked_by)}")
        if result.error:
            lines.append(f"  · 错误：{result.error}")
        lines.append("")

    passed = sum(result.status == "pass" for result in results)
    failed = sum(result.status == "fail" for result in results)
    skipped = sum(result.status == "skip" for result in results)
    total_ms = sum(result.duration_ms for result in results)
    lines.append("━━━ 结果 ━━━")
    lines.append(f"通过 {passed}，失败 {failed}，跳过 {skipped}")
    lines.append(f"总耗时 {total_ms / 1000:.1f}s")
    return "\n".join(lines)
