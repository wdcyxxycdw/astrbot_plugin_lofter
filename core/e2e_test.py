from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Literal

from .client import LofterClient
from .db import LofterDB
from .scheduler import SubscriptionScheduler
from .storage import SubscriptionStorage
from .e2e_steps_network import NetworkStepsMixin
from .e2e_steps_flow import FlowStepsMixin


@dataclass
class StepResult:
    name: str
    status: Literal["pass", "fail", "skip"]
    duration_ms: int = 0
    details: list[str] = field(default_factory=list)
    error: str | None = None


class E2ETestRunner(NetworkStepsMixin, FlowStepsMixin):
    TEST_SESSION = "__lofter_e2e_test__"
    TEST_TAG = "摄影"
    # TODO: 如 lofter-news 不可用，替换为任意长期活跃博主
    TEST_BLOG = "lofter-news"
    TEST_CONFIG_KEY = "__e2e_test_kv__"

    def __init__(
        self,
        db: LofterDB,
        client: LofterClient,
        storage: SubscriptionStorage,
        scheduler: SubscriptionScheduler,
        send_push: Callable[[str, str, list], Awaitable[None]],
    ):
        self._db = db
        self._client = client
        self._storage = storage
        self._scheduler = scheduler
        self._send_push = send_push
        self._artifacts: dict = {}

    async def run_all(self, real_session_id: str) -> list[StepResult]:
        results: list[StepResult] = []

        steps = [
            self._step_01_config_rw,
            self._step_02_dwr_engine,
            self._step_03_http_get,
            self._step_04_dwr_search,
            self._step_05_dwr_parse,
            self._step_06_blog_fetch,
            self._step_07_blog_parse,
            self._step_08_post_parse,
            self._step_09_auto_parse,
            self._step_10_filter,
            self._step_11_format,
            self._step_12_search_flow,
            self._step_13_subscription_crud,
            self._step_14_subtag_full,
            self._step_15_subblog_full,
        ]
        for fn in steps:
            results.append(await fn())

        results.append(await self._step_16_subtagpreview(real_session_id))
        results.append(await self._step_17_seen_sent())
        results.append(await self._step_18_scheduler_state())
        results.append(await self._step_19_manual_poll())
        results.append(await self._step_20_push_blog(real_session_id))

        cleanup = await self._cleanup()
        results.append(cleanup)
        return results

    def _timed_start(self) -> float:
        return time.perf_counter()

    def _timed_end(self, t0: float) -> int:
        return int((time.perf_counter() - t0) * 1000)

    def _pass(self, name: str, ms: int, details: list[str]) -> StepResult:
        return StepResult(name=name, status="pass", duration_ms=ms, details=details)

    def _fail(self, name: str, ms: int, e: Exception, details: list[str]) -> StepResult:
        return StepResult(name=name, status="fail", duration_ms=ms, details=details, error=str(e))

    def _skip(self, name: str, reason: str) -> StepResult:
        return StepResult(name=name, status="skip", details=[reason])

    async def _cleanup(self) -> StepResult:
        name = "测试清理"
        t0 = self._timed_start()
        details: list[str] = []
        s = self.TEST_SESSION
        try:
            subs = await self._storage.list_by_session(s)
            for sub in subs:
                await self._storage.remove_by_id(sub.id)
            details.append(f"删除 {len(subs)} 条订阅")

            await self._db.clear_session(s)
            details.append("seen/sent 记录已清理")

            await self._db.delete_config(self.TEST_CONFIG_KEY)
            details.append(f"配置键 {self.TEST_CONFIG_KEY} 已删除")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)


_STATUS_ICON = {"pass": "✓", "fail": "✗", "skip": "○"}


def format_report(results: list[StepResult]) -> str:
    lines = ["━━━ Lofter 端到端测试 ━━━", ""]

    total = len(results)
    for i, r in enumerate(results, 1):
        icon = _STATUS_ICON.get(r.status, "?")
        if r.status == "skip":
            lines.append(f"[{i}/{total}] {icon} {r.name}")
        else:
            lines.append(f"[{i}/{total}] {icon} {r.name} ({r.duration_ms} ms)")
        for d in r.details:
            lines.append(f"  · {d}")
        if r.error:
            lines.append(f"  · 错误：{r.error}")
        lines.append("")

    passed = sum(1 for r in results if r.status == "pass")
    failed = sum(1 for r in results if r.status == "fail")
    skipped = sum(1 for r in results if r.status == "skip")
    total_ms = sum(r.duration_ms for r in results)

    skip_refs = []
    for i, r in enumerate(results, 1):
        if r.status == "skip" and r.details:
            skip_refs.append(f"[{i}] {r.details[0]}")

    lines.append("━━━ 结果 ━━━")
    skip_note = f"，跳过 {skipped}（{'；'.join(skip_refs)}）" if skipped else ""
    lines.append(f"通过 {passed}，失败 {failed}{skip_note}")
    lines.append(f"总耗时 {total_ms / 1000:.1f}s")

    cleanup = next((r for r in results if r.name == "测试清理"), None)
    if cleanup and cleanup.status == "pass":
        lines.append("测试 session 清理完成")
    else:
        lines.append("注意：测试 session 清理可能未完成")

    return "\n".join(lines)
