from __future__ import annotations

import asyncio
import os
import socket
from types import SimpleNamespace

import pytest

from core import scheduler as scheduler_module
from core.author_block import AuthorBlockStorage
from core.client import LofterClient
from core.db import LofterDB
from core.delivery import decode_post
from core.e2e_test import _ObservingDeliveryQueue
from core.errors import SourceSchemaError
from core.mobile_adapter import MobileAdapter
from core.parser import POST_FIELDS, Post
from core.post_identity import mobile_decimal_ids, post_url_identity
from core.scheduler import SubscriptionScheduler
from core.session_gate import SessionGateRegistry
from core.source_scan import SourcePage
from core.storage import SubscriptionStorage
from core.subscription_service import SubscriptionService
from tests.test_command_permissions import _load_main_module

RUN_LIVE = os.getenv("LOFTER_RUN_LIVE") == "1"
IMAGE_POST_URL = os.getenv("LOFTER_IMAGE_POST_URL", "")
SESSION_ID = "__lofter_live_hybrid__"

pytestmark = [
    pytest.mark.real,
    pytest.mark.live_hybrid,
    pytest.mark.skipif(not RUN_LIVE, reason="需要设置 LOFTER_RUN_LIVE=1"),
    pytest.mark.skipif(not IMAGE_POST_URL, reason="缺少五图 canary 环境变量"),
]


class _SafeSchedulerLogger:
    _ALLOWED_MESSAGES = frozenset(
        {
            "获取博主帖子详情失败 %s: %s",
            "发送订阅推送超时 session=%s post=%s",
            "发送订阅推送失败 session=%s post=%s: %s",
        }
    )

    def __init__(self) -> None:
        self.messages: list[str] = []

    def error(self, message: str, *args: object, **kwargs: object) -> None:
        self._record(message, args, kwargs)

    def warning(self, message: str, *args: object, **kwargs: object) -> None:
        self._record(message, args, kwargs)

    def _record(
        self, message: str, args: tuple[object, ...], kwargs: dict[str, object]
    ) -> None:
        if message not in self._ALLOWED_MESSAGES:
            raise AssertionError("live_hybrid_scheduler_log_key")
        if args or kwargs:
            raise AssertionError("live_hybrid_scheduler_log_dynamic_args")
        self.messages.append(message)


def _install_safe_scheduler_logger(monkeypatch) -> _SafeSchedulerLogger:
    logger = _SafeSchedulerLogger()
    monkeypatch.setattr(scheduler_module, "logger", logger)
    return logger


class _ControlledBlogSource:
    def __init__(self, owner: str, post: Post) -> None:
        self.owner = owner
        self.post = post

    def use(self, post: Post) -> None:
        self.post = post

    async def get_post(self, url: str) -> Post:
        if url != self.post.url:
            raise SourceSchemaError("post.url")
        return self.post

    async def list_blog(
        self, username: str, cursor: str | None, limit: int
    ) -> SourcePage:
        if username != self.owner or cursor is not None:
            raise SourceSchemaError("response")
        return SourcePage(
            items=[self.post],
            source="mobile_blog",
            next_cursor=None,
            exhausted=True,
            sort="new",
            mapped_count=1,
            dropped_count=0,
            complete=True,
        )

    async def list_tag(self, tag, cursor, limit, sort) -> SourcePage:
        raise SourceSchemaError("response")


class _FakeQQContext:
    def __init__(self) -> None:
        self.calls = []

    def get_platform_inst(self, platform_id):
        return SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp"))

    async def send_message(self, session_id, chain):
        self.calls.append(chain)
        return True


async def _fetch_real_candidate() -> Post:
    try:
        _, post_id, _ = post_url_identity(IMAGE_POST_URL)
        ids = mobile_decimal_ids(post_id)
        if ids is None:
            raise ValueError
    except ValueError:
        pytest.fail("五图 canary URL 无效")
    client = LofterClient()
    await client.initialize()
    try:
        return await MobileAdapter(client).get_post(*ids)
    finally:
        await client.close()


def _block_outbound(monkeypatch) -> None:
    def blocked(*args, **kwargs):
        raise RuntimeError("live hybrid 网络阶段已经结束")

    async def blocked_async(*args, **kwargs):
        blocked()

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket.socket, "sendto", blocked)
    monkeypatch.setattr(asyncio.BaseEventLoop, "create_connection", blocked_async)
    monkeypatch.setattr(asyncio.BaseEventLoop, "create_datagram_endpoint", blocked_async)


def _baseline(candidate: Post) -> Post:
    post_id = "1a_1" if candidate.post_id != "1a_1" else "1a_2"
    owner = candidate.author_username
    return Post(
        post_id=post_id,
        title="baseline",
        summary="baseline",
        images=[],
        author=candidate.author or "Lofter",
        author_username=owner,
        url=f"https://{owner}.lofter.com/post/{post_id}",
        tags=["baseline"],
        publish_time="2000-01-01 00:00:00",
        content="baseline",
        source="live_hybrid_baseline",
        completeness=frozenset(POST_FIELDS),
        provenance={field: "live_hybrid_baseline" for field in POST_FIELDS},
    )


def _install_qq_components(main) -> None:
    main.Comp = SimpleNamespace(
        Plain=lambda text: SimpleNamespace(kind="plain", text=text),
        Image=SimpleNamespace(
            fromURL=lambda url: SimpleNamespace(kind="image", url=url)
        ),
        Node=lambda **kwargs: SimpleNamespace(kind="node", **kwargs),
        Nodes=lambda **kwargs: SimpleNamespace(kind="nodes", **kwargs),
    )
    main.MessageChain = lambda components: SimpleNamespace(chain=components)


async def _delivery_row(db: LofterDB):
    return await db.transaction(
        lambda conn: conn.execute(
            "SELECT status,payload_json,lease_token FROM deliveries "
            "WHERE session_id=?",
            (SESSION_ID,),
        ).fetchone()
    )


async def _build_scheduler(db, source, send_func):
    gates = SessionGateRegistry()
    storage = SubscriptionStorage(db)
    service = SubscriptionService(db, source, gates)
    queue = _ObservingDeliveryQueue(db, gates)
    scheduler = SubscriptionScheduler(
        storage,
        source,
        db,
        send_func,
        block_storage=AuthorBlockStorage(db, gates),
        gates=gates,
        subscription_service=service,
        delivery_queue=queue,
    )
    return service, queue, scheduler


def _require(condition: bool, message: str) -> None:
    if not condition:
        pytest.fail(message)


def _validate_candidate(candidate: Post) -> tuple[str, ...]:
    _require(candidate.source == "mobile_detail", "候选未来自 Mobile detail")
    _require(len(candidate.images) == 5, "候选图片数量不是五张")
    _require("images" in candidate.completeness, "候选图片字段不完整")
    _require(
        candidate.provenance.get("images") == "mobile_detail",
        "候选图片字段来源错误",
    )
    _require(bool(candidate.author_username), "候选缺少作者")
    return tuple(candidate.images)


async def _cleanup_files(db_path, errors: list[str]) -> None:
    for suffix, phase in (
        ("", "db_unlink"),
        ("-wal", "sidecar_unlink_wal"),
        ("-shm", "sidecar_unlink_shm"),
        (".lock", "sidecar_unlink_lock"),
    ):
        try:
            db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
        except BaseException:
            errors.append(phase)
    for suffix, phase in (
        ("", "db_residual"),
        ("-wal", "sidecar_residual_wal"),
        ("-shm", "sidecar_residual_shm"),
        (".lock", "sidecar_residual_lock"),
    ):
        try:
            exists = db_path.with_name(db_path.name + suffix).exists()
        except BaseException:
            errors.append(phase.replace("residual", "verify"))
            continue
        if exists:
            errors.append(phase)


def _raise_cleanup_errors(errors: list[str]) -> None:
    if errors:
        raise RuntimeError("live_hybrid_cleanup:" + ",".join(errors))


async def _cleanup_build(db: LofterDB, db_path) -> list[str]:
    errors: list[str] = []
    try:
        await db.close()
    except BaseException:
        errors.append("db_close")
    await _cleanup_files(db_path, errors)
    return errors


async def _create_runtime(tmp_path, candidate: Post, monkeypatch):
    _install_safe_scheduler_logger(monkeypatch)
    main = _load_main_module()
    _install_qq_components(main)
    context = _FakeQQContext()
    plugin = object.__new__(main.LofterPlugin)
    plugin._max_images = 1
    plugin.context = context
    send_entered = asyncio.Event()
    release_send = asyncio.Event()
    captured = {}

    async def send_func(session_id, post, header, source_types):
        captured.update(post=post, source_types=source_types)
        send_entered.set()
        await release_send.wait()
        result = await main.LofterPlugin._send_push_result(
            plugin, session_id, post, header, source_types
        )
        captured["result"] = result
        return result.accepted

    db_path = tmp_path / "live-hybrid.db"
    db = LofterDB(str(db_path))
    try:
        await db.initialize()
        source = _ControlledBlogSource(candidate.author_username, _baseline(candidate))
        service, queue, scheduler = await _build_scheduler(db, source, send_func)
    except BaseException as error:
        cleanup_errors = await _cleanup_build(db, db_path)
        if cleanup_errors:
            error.add_note("live_hybrid_cleanup:" + ",".join(cleanup_errors))
        raise
    return SimpleNamespace(
        db=db,
        db_path=db_path,
        source=source,
        service=service,
        queue=queue,
        scheduler=scheduler,
        context=context,
        send_entered=send_entered,
        release_send=release_send,
        captured=captured,
        poll_task=None,
    )


async def _assert_pending(runtime, candidate: Post, original: tuple[str, ...]):
    mutation = await runtime.service.subscribe_blog(SESSION_ID, runtime.source.owner)
    _require(len(mutation.added_subscribes) == 1, "预订阅创建数量错误")
    runtime.source.use(candidate)
    runtime.poll_task = asyncio.create_task(
        runtime.scheduler._poll_single_session(SESSION_ID)
    )
    await asyncio.wait_for(runtime.queue.persist_ready.wait(), 10)
    _require(runtime.queue.persist_error is None, "发现持久化失败")
    pending = await _delivery_row(runtime.db)
    _require(pending is not None, "未创建 pending delivery")
    _require(pending[0] == "pending", "delivery 未处于 pending")
    payload_post = decode_post(pending[1])
    _require(len(payload_post.images) == 5, "pending payload 图片数量错误")
    payload_order_preserved = all(
        left == right for left, right in zip(payload_post.images, original)
    )
    _require(payload_order_preserved, "pending payload 图片顺序错误")
    unseen = await runtime.db.filter_unseen_session(
        SESSION_ID, "blog", [candidate.post_id]
    )
    _require(len(unseen) == 1, "pending 阶段 seen 状态错误")


async def _assert_sending(runtime) -> None:
    runtime.queue.release_claim.set()
    await asyncio.wait_for(runtime.send_entered.wait(), 10)
    sending = await _delivery_row(runtime.db)
    _require(sending is not None, "未创建 sending delivery")
    _require(sending[0] == "sending", "delivery 未处于 sending")
    _require(isinstance(sending[2], str) and bool(sending[2]), "缺少 sending lease")
    _require(len(runtime.captured["post"].images) == 5, "发送图片数量错误")
    _require(
        runtime.captured["source_types"] == frozenset({"blog"}),
        "发送来源类型错误",
    )


async def _assert_accepted(runtime, candidate: Post, original: tuple[str, ...]):
    runtime.release_send.set()
    await asyncio.wait_for(runtime.poll_task, 10)
    _require(len(runtime.context.calls) == 2, "QQ 消息调用次数错误")
    primary, media = (call.chain for call in runtime.context.calls)
    _require([item.kind for item in primary] == ["plain"], "主消息类型错误")
    _require([item.kind for item in media] == ["nodes"], "图片消息类型错误")
    _require(len(media[0].nodes) == 5, "图片 Nodes 数量错误")
    nodes_order_preserved = all(
        node.content[0].url == original[index]
        for index, node in enumerate(media[0].nodes)
    )
    _require(nodes_order_preserved, "图片 Nodes 顺序错误")
    _require(runtime.captured["result"].accepted is True, "QQ primary 未 accepted")
    accepted = await _delivery_row(runtime.db)
    _require(accepted is not None, "未创建 accepted delivery")
    _require(accepted[0] == "accepted", "delivery 未处于 accepted")
    _require(accepted[2] is None, "accepted delivery 仍持有 lease")
    unseen = await runtime.db.filter_unseen_session(
        SESSION_ID, "blog", [candidate.post_id]
    )
    unsent = await runtime.db.filter_unsent(SESSION_ID, [candidate.post_id])
    _require(len(unseen) == 0, "accepted 后仍有 unseen")
    _require(len(unsent) == 0, "accepted 后仍有 unsent")


async def _cleanup_runtime(runtime) -> None:
    errors: list[str] = []
    runtime.queue.release_claim.set()
    runtime.release_send.set()
    task = runtime.poll_task
    if task is not None and not task.done():
        try:
            task.cancel()
        except BaseException:
            errors.append("task_cancel")
        try:
            outcomes = await asyncio.gather(task, return_exceptions=True)
            if any(
                isinstance(outcome, BaseException)
                and not isinstance(outcome, asyncio.CancelledError)
                for outcome in outcomes
            ):
                errors.append("task_gather")
        except BaseException:
            errors.append("task_gather")
    try:
        await runtime.db.close()
    except BaseException:
        errors.append("db_close")
    await _cleanup_files(runtime.db_path, errors)
    _raise_cleanup_errors(errors)


@pytest.mark.asyncio
async def test_real_five_image_blog_delivery_survives_full_hybrid(
    tmp_path, monkeypatch
):
    candidate = await _fetch_real_candidate()
    original = _validate_candidate(candidate)
    _block_outbound(monkeypatch)
    runtime = await _create_runtime(tmp_path, candidate, monkeypatch)
    try:
        await _assert_pending(runtime, candidate, original)
        await _assert_sending(runtime)
        await _assert_accepted(runtime, candidate, original)
    except BaseException as error:
        try:
            await _cleanup_runtime(runtime)
        except BaseException as cleanup_error:
            error.add_note(str(cleanup_error))
        raise
    else:
        await _cleanup_runtime(runtime)
