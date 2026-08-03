from __future__ import annotations

import asyncio

from .parser import Post


class FlowStepsMixin:
    async def _step_05_warmup_pending(self) -> object:
        name = "订阅 warmup 与 pending"
        started = self._timed_start()
        runtime = self._runtime
        baseline = self._artifacts.get("baseline")
        candidate = self._artifacts.get("candidate")
        if runtime is None:
            return self._skip(name, "临时运行时未就绪")
        if not isinstance(baseline, Post) or not isinstance(candidate, Post):
            return self._skip(name, "实时 fixture 未就绪")
        self._artifacts["pending_verified"] = False
        details: list[str] = []
        try:
            runtime.source.use(baseline)
            result = await runtime.subscriptions.subscribe_tags(
                runtime.session_id, [self.TEST_TAG], []
            )
            if result.added_subscribes != (self.TEST_TAG,):
                raise RuntimeError("subscription warmup did not add target")
            state = await _warmup_state(runtime, baseline, candidate)
            if state != ("active", 1, 0):
                raise RuntimeError("subscription warmup state mismatch")
            details.append("fetch-first 订阅已激活")
            details.append("baseline 已写入具体 subscription seen")
            details.append("candidate 在 discovery 前保持 unseen")

            runtime.source.use(candidate)
            self._poll_task = asyncio.create_task(
                runtime.scheduler._poll_single_session(runtime.session_id)
            )
            ready = await self._wait_event_or_poll(runtime.queue.persist_ready)
            if runtime.queue.persist_error is not None:
                raise runtime.queue.persist_error
            if not ready:
                raise RuntimeError("scheduler did not persist discovery")
            discovery = runtime.queue.discovery_result
            if discovery is None or discovery.admitted != 1:
                raise RuntimeError("candidate was not admitted")
            row = await self._delivery_row()
            if row is None:
                raise RuntimeError("pending delivery is missing")
            status, lease, attempts, source_count = row
            if status != "pending" or lease is not None:
                raise RuntimeError("delivery is not pending")
            if attempts != 0 or source_count < 1:
                raise RuntimeError("pending delivery provenance is invalid")
            if await self._candidate_seen():
                raise RuntimeError("candidate became seen before ack")
            self._artifacts["pending_verified"] = True
            details.append("production persist_discovery 已写入 pending")
            details.append("delivery_sources 已记录实际订阅来源")
            return self._pass(
                name,
                self._timed_end(started),
                details,
                {"pending_verified": True},
            )
        except Exception as exc:
            return self._fail(
                name,
                self._timed_end(started),
                exc,
                details,
                facts={"pending_verified": False},
            )

    async def _step_06_delivery_acceptance(self) -> object:
        name = "claim、adapter、ack 与 seen"
        started = self._timed_start()
        runtime = self._runtime
        if runtime is None or self._poll_task is None:
            return self._skip(name, "pending 链路未就绪")
        if self._artifacts.get("pending_verified") is not True:
            return self._skip(name, "pending 状态未通过验证")
        details: list[str] = []
        runtime.queue.release_claim.set()
        try:
            entered = await self._wait_event_or_poll(self._send_entered)
            if not entered:
                raise RuntimeError("scheduler did not enter adapter")
            row = await self._delivery_row()
            if row is None:
                raise RuntimeError("sending delivery is missing")
            status, lease, attempts, source_count = row
            if status != "sending" or not isinstance(lease, str) or not lease:
                raise RuntimeError("delivery lease was not established")
            if attempts != 0 or source_count < 1:
                raise RuntimeError("sending delivery provenance is invalid")
            if await self._candidate_seen():
                raise RuntimeError("candidate became seen before ack")
            details.append("production claim_next 已建立 sending lease")

            self._release_send.set()
            await asyncio.wait_for(
                asyncio.shield(self._poll_task), self.POLL_SECONDS
            )
            facts = {
                "send_attempts": self._send_attempts,
                "adapter_accepted": self._send_result
                if self._send_result is not None
                else "unknown",
            }
            if self._send_entries != 1 or self._send_attempts > 1:
                raise RuntimeError("adapter bridge invoked more than once")
            if self._send_error is not None:
                raise self._send_error
            if self._send_result is None:
                return self._inconclusive(
                    name,
                    self._timed_end(started),
                    [*details, "adapter 结果未知，delivery 保持 lease recovery 语义"],
                    facts,
                )
            if self._send_result is not True:
                return self._fail(
                    name,
                    self._timed_end(started),
                    RuntimeError("adapter rejected delivery"),
                    details,
                    health="degraded",
                    facts=facts,
                )
            row = await self._delivery_row()
            if row is None or row[0] != "accepted" or row[1] is not None:
                raise RuntimeError("delivery was not accepted")
            if not await self._candidate_seen():
                raise RuntimeError("accepted delivery was not marked seen")
            details.append("真实 adapter 严格返回 True")
            details.append("production ack_success 已写入 accepted")
            details.append("candidate 已写入 subscription-level seen")
            return self._pass(
                name,
                self._timed_end(started),
                details,
                facts,
            )
        except asyncio.TimeoutError as exc:
            return self._fail(
                name,
                self._timed_end(started),
                exc,
                details,
                health="inconclusive",
                facts={
                    "send_attempts": self._send_attempts,
                    "adapter_accepted": "unknown",
                },
            )
        except Exception as exc:
            return self._fail(
                name,
                self._timed_end(started),
                exc,
                details,
                facts={
                    "send_attempts": self._send_attempts,
                    "adapter_accepted": self._send_result
                    if self._send_result is not None
                    else "unknown",
                },
            )


async def _warmup_state(runtime, baseline: Post, candidate: Post) -> tuple:
    return await runtime.db.transaction(
        lambda conn: conn.execute(
            """
            SELECT s.state,
                   EXISTS(
                       SELECT 1 FROM seen_posts sp
                       WHERE sp.subscription_id=s.id AND sp.post_id=?
                   ),
                   EXISTS(
                       SELECT 1 FROM seen_posts sp
                       WHERE sp.subscription_id=s.id AND sp.post_id=?
                   )
            FROM subscriptions s
            WHERE s.session_id=? AND s.type='tag'
              AND s.role='subscribe' AND s.target=?
            """,
            (
                baseline.post_id,
                candidate.post_id,
                runtime.session_id,
                runtime.source._tag,
            ),
        ).fetchone()
    )
