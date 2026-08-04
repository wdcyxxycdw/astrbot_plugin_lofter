from __future__ import annotations

import asyncio

from .e2e_steps_network import FixtureBundle
from .parser import Post


class FlowStepsMixin:
    async def _step_07_warmup_pending(self) -> object:
        key = "warmup_pending"
        name = "订阅 warmup 与 pending"
        started = self._timed_start()
        blockers = self._dependency_blockers("runtime", "fixture_detail")
        if blockers:
            return self._skip(key, name, blockers)
        runtime = self._runtime
        bundle = self._artifacts.get("fixture_bundle")
        if runtime is None or not isinstance(bundle, FixtureBundle):
            return self._skip(key, name, blockers or ("runtime", "fixture_detail"))
        self._artifacts["pending_verified"] = False
        details: list[str] = []
        try:
            runtime.source.install(bundle)
            runtime.source.use(bundle.baseline)
            result = await runtime.subscriptions.subscribe_tags(
                runtime.session_id, [self.TEST_TAG], []
            )
            if result.added_subscribes != (self.TEST_TAG,):
                raise RuntimeError("subscription warmup did not add target")
            state = await _warmup_state(
                runtime, bundle.baseline, bundle.candidate
            )
            if state != ("active", 1, 0):
                raise RuntimeError("subscription warmup state mismatch")
            details.append("fetch-first 订阅已激活")
            details.append("baseline 已写入具体 subscription seen")
            details.append("candidate 在 discovery 前保持 unseen")

            runtime.source.use(bundle.candidate)
            self._poll_task = asyncio.create_task(
                runtime.scheduler._poll_single_session(runtime.session_id)
            )
            ready = await self._wait_event_or_poll(runtime.queue.persist_ready)
            if runtime.queue.persist_error is not None:
                raise runtime.queue.persist_error
            if not ready:
                return self._inconclusive(
                    key,
                    name,
                    self._timed_end(started),
                    [*details, "scheduler 未在期限内完成 discovery 持久化"],
                    {"pending_verified": False},
                )
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
                key,
                name,
                self._timed_end(started),
                details,
                {"pending_verified": True},
            )
        except Exception as exc:
            return self._fail(
                key,
                name,
                self._timed_end(started),
                exc,
                details,
                facts={"pending_verified": False},
            )

    async def _step_08_claim_send_ack_seen(self) -> object:
        key = "claim_send_ack_seen"
        name = "claim、adapter、ack 与 seen"
        started = self._timed_start()
        blockers = self._dependency_blockers("warmup_pending")
        if blockers:
            return self._skip(key, name, blockers)
        runtime = self._runtime
        if runtime is None or self._poll_task is None:
            return self._skip(key, name, ("warmup_pending",))
        details: list[str] = []
        runtime.queue.release_claim.set()
        try:
            entered = await self._wait_event_or_poll(self._send_entered)
            if not entered:
                return self._inconclusive(
                    key,
                    name,
                    self._timed_end(started),
                    ["scheduler 未在期限内进入 adapter"],
                    _send_facts(self),
                )
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
            if self._send_entries != 1 or self._send_attempts > 1:
                raise RuntimeError("adapter bridge invoked more than once")
            if self._send_error is not None:
                raise self._send_error
            if self._send_result is None:
                return self._inconclusive(
                    key,
                    name,
                    self._timed_end(started),
                    [*details, "adapter 结果未知，delivery 保持 lease recovery 语义"],
                    _send_facts(self),
                )

            row = await self._delivery_row()
            delivery_accepted = bool(
                row is not None and row[0] == "accepted" and row[1] is None
            )
            seen_written = await self._candidate_seen()
            facts = _send_facts(
                self,
                delivery_accepted=delivery_accepted,
                seen_written=seen_written,
            )
            if not self._send_result.accepted:
                if delivery_accepted or seen_written:
                    raise RuntimeError("rejected delivery changed accepted or seen state")
                error = self._send_result.error()
                if error is None:
                    raise RuntimeError("primary rejection diagnostic is missing")
                return self._fail(
                    key,
                    name,
                    self._timed_end(started),
                    error,
                    details,
                    health="degraded",
                    facts=facts,
                )
            if not delivery_accepted:
                raise RuntimeError("delivery was not accepted")
            if not seen_written:
                raise RuntimeError("accepted delivery was not marked seen")

            details.append("主要消息已被 adapter 接受")
            details.append("production ack_success 已写入 accepted")
            details.append("candidate 已写入 subscription-level seen")
            error = self._send_result.error()
            if error is not None:
                details.append(
                    "图片转发失败，主要消息不会重试"
                )
                return self._fail(
                    key,
                    name,
                    self._timed_end(started),
                    error,
                    details,
                    health="degraded",
                    facts=facts,
                )
            return self._pass(
                key,
                name,
                self._timed_end(started),
                details,
                facts,
            )
        except asyncio.TimeoutError as exc:
            return self._fail(
                key,
                name,
                self._timed_end(started),
                exc,
                details,
                health="inconclusive",
                facts=_send_facts(self),
            )
        except Exception as exc:
            return self._fail(
                key,
                name,
                self._timed_end(started),
                exc,
                details,
                facts=_send_facts(self),
            )


def _send_facts(
    runner,
    *,
    delivery_accepted: bool | str = "unknown",
    seen_written: bool | str = "unknown",
) -> dict[str, str | int | bool]:
    result = runner._send_result
    return {
        "send_attempts": runner._send_attempts,
        "primary_outcome": result.primary_outcome if result else "unknown",
        "primary_stage": result.primary_stage if result else "unknown",
        "primary_error_type": result.primary_error_type or "none"
        if result
        else "unknown",
        "media_outcome": result.media_outcome if result else "unknown",
        "media_stage": result.media_stage or "none" if result else "unknown",
        "media_error_type": result.media_error_type or "none"
        if result
        else "unknown",
        "delivery_accepted": delivery_accepted,
        "seen_written": seen_written,
    }


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
