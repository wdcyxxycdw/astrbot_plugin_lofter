from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

if sys.version_info < (3, 12):
    pytest.skip("adapter contract requires Python 3.12+", allow_module_level=True)

astrbot = pytest.importorskip(
    "astrbot", reason="adapter contract requires the real AstrBot package"
)

if not isinstance(astrbot, types.ModuleType) or not getattr(astrbot, "__file__", None):
    pytest.skip("adapter contract requires the real AstrBot package", allow_module_level=True)

import astrbot.api  # noqa: F401
from aiocqhttp.api_impl import ResultStore
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.manager import PlatformManager
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
    AiocqhttpAdapter,
)
from astrbot.core.star.context import Context
from astrbot.core.utils.metrics import Metric

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "lofter_napcat_contract"
ADAPTER_ID = "contract-aiocqhttp"
SELF_ID = "10001"
GROUP_ID = 200000002
SESSION_ID = f"{ADAPTER_ID}:GroupMessage:{GROUP_ID}"
HEADER = "【标签「契约测试」有新内容】"
CANARY = "synthetic-backend-private-canary"

pytestmark = pytest.mark.adapter_contract


def _load_plugin():
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package
    module_name = f"{PACKAGE}.main"
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


main = _load_plugin()
parser_module = importlib.import_module(f"{PACKAGE}.core.parser")
delivery_module = importlib.import_module(f"{PACKAGE}.core.delivery")
scheduler_module = importlib.import_module(f"{PACKAGE}.core.scheduler")
author_block_module = importlib.import_module(f"{PACKAGE}.core.author_block")
session_gate_module = importlib.import_module(f"{PACKAGE}.core.session_gate")
storage_module = importlib.import_module(f"{PACKAGE}.core.storage")
subscription_module = importlib.import_module(f"{PACKAGE}.core.subscription_service")


def _post():
    return main.Post(
        post_id="1a_20",
        title="契约标题",
        summary="契约摘要",
        content="契约正文",
        images=["https://media.example.invalid/contract.jpg"],
        author="契约作者",
        author_username="contract-user",
        url="https://contract-user.lofter.com/post/1a_20",
        tags=["契约测试"],
        publish_time="2099-01-01 00:00:01",
        source="unknown",
        completeness=parser_module.POST_FIELDS,
    )


def _response(
    action: dict, *, fail_primary: bool = False, fail_media: bool = False,
) -> dict:
    segments = action.get("params", {}).get("message", [])
    segment_types = [item.get("type") for item in segments]
    valid_text = action["action"] == "send_group_msg" and segment_types == ["text"]
    valid_media = action["action"] == "send_group_forward_msg"
    if (valid_text and not fail_primary) or (valid_media and not fail_media):
        return {
            "status": "ok",
            "retcode": 0,
            "data": {"message_id": 42},
            "echo": action["echo"],
        }
    return {
        "status": "failed",
        "retcode": 1400,
        "data": None,
        "message": CANARY,
        "wording": CANARY,
        "echo": action["echo"],
    }


def _assert_text_action(action: dict, expected_text: str) -> None:
    assert action["action"] == "send_group_msg"
    assert action["params"] == {
        "group_id": GROUP_ID,
        "message": [{"type": "text", "data": {"text": expected_text}}],
    }
    assert isinstance(action["echo"]["seq"], int)


def _assert_forward_action(action: dict) -> None:
    assert action["action"] == "send_group_forward_msg"
    nodes = action["params"]["messages"]
    assert len(nodes) == 1
    image = nodes[0]["data"]["content"][0]
    assert image["type"] == "image"
    assert image["data"]["file"].startswith("base64://")
    assert isinstance(action["echo"]["seq"], int)


async def _wait_for_api_client(adapter) -> None:
    for _ in range(100):
        if SELF_ID in adapter.bot._wsr_api_clients:
            return
        await asyncio.sleep(0)
    pytest.fail("reverse WebSocket API client was not registered")


async def _exchange(
    runtime, operation, *, fail_primary: bool = False, fail_media: bool = False,
):
    task = None
    actions = []
    app = runtime.adapter.bot.server_app
    try:
        async with app.test_app():
            client = app.test_client()
            async with client.websocket(
                "/ws/api",
                headers={"X-Self-ID": SELF_ID, "X-Client-Role": "API"},
            ) as websocket:
                await _wait_for_api_client(runtime.adapter)
                task = asyncio.create_task(operation())
                while not task.done():
                    receiver = asyncio.create_task(websocket.receive_json())
                    done, _ = await asyncio.wait(
                        {task, receiver}, timeout=1,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if receiver not in done:
                        receiver.cancel()
                        await asyncio.gather(receiver, return_exceptions=True)
                        break
                    action = receiver.result()
                    actions.append(action)
                    await websocket.send_json(_response(
                        action,
                        fail_primary=fail_primary,
                        fail_media=fail_media,
                    ))
                result = await asyncio.wait_for(task, timeout=1)
                await asyncio.sleep(0)
            for _ in range(20):
                if SELF_ID not in runtime.adapter.bot._wsr_api_clients:
                    break
                await asyncio.sleep(0)
        return actions, result
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest_asyncio.fixture
async def runtime(monkeypatch):
    async def no_metric(*args, **kwargs):
        return None

    async def fake_base64(self):
        return "c3ludGhldGljLWltYWdl"

    monkeypatch.setattr(Metric, "upload", no_metric)
    monkeypatch.setattr(main.Comp.Image, "convert_to_base64", fake_base64)
    assert not ResultStore._futures

    event_queue = asyncio.Queue()
    adapter = AiocqhttpAdapter(
        {
            "id": ADAPTER_ID,
            "ws_reverse_host": "127.0.0.1",
            "ws_reverse_port": 6199,
        },
        {},
        event_queue,
    )
    config = AstrBotConfig.__new__(AstrBotConfig)
    dict.__init__(config, {"platform": [], "platform_settings": {}})
    platform_manager = PlatformManager(config, event_queue)
    platform_manager.platform_insts.append(adapter)
    unused = object()
    context = Context(
        event_queue,
        config,
        unused,
        unused,
        platform_manager,
        unused,
        unused,
        unused,
        unused,
        unused,
        unused,
    )
    plugin = object.__new__(main.LofterPlugin)
    plugin.context = context
    plugin._max_images = 3
    value = SimpleNamespace(adapter=adapter, context=context, plugin=plugin)
    try:
        yield value
    finally:
        await adapter.terminate()
        assert not adapter.bot._wsr_api_clients
        assert not adapter.bot._wsr_event_clients
        assert not ResultStore._futures


@pytest.mark.asyncio
async def test_plain_primary_succeeds_through_reverse_websocket(runtime):
    post = _post()
    expected = main.format_post(post, header=HEADER)
    actions, result = await _exchange(
        runtime,
        lambda: runtime.plugin._send_push_result(
            SESSION_ID, post, HEADER, frozenset({"tag"})
        ),
    )

    _assert_text_action(actions[0], expected)
    _assert_forward_action(actions[1])
    assert result.accepted is True
    assert result.primary_outcome == "accepted"
    assert result.primary_stage == "primary_send"
    assert result.media_outcome == "accepted"
    assert result.media_stage == "media_send"
    assert result.error() is None


@pytest.mark.asyncio
async def test_action_failed_is_reduced_to_safe_send_result(runtime, caplog):
    post = _post()
    expected = main.format_post(post, header=HEADER)
    actions, result = await _exchange(
        runtime,
        lambda: runtime.plugin._send_push_result(
            SESSION_ID, post, HEADER, frozenset({"tag"})
        ),
        fail_primary=True,
    )

    _assert_text_action(actions[0], expected)
    error = result.error()
    assert result.accepted is False
    assert result.primary_outcome == "error"
    assert result.primary_stage == "primary_send"
    assert result.primary_error_type == "ActionFailed"
    assert result.primary_error_retcode == 1400
    assert str(error) == "primary_send:error:ActionFailed:retcode=1400"
    assert CANARY not in repr(result)
    assert CANARY not in str(error)
    assert CANARY not in caplog.text


@pytest.mark.asyncio
async def test_media_failure_keeps_primary_accepted(runtime, caplog):
    post = _post()
    actions, result = await _exchange(
        runtime,
        lambda: runtime.plugin._send_push_result(
            SESSION_ID, post, HEADER, frozenset({"blog"})
        ),
        fail_media=True,
    )

    _assert_text_action(actions[0], main.format_post(post, header=HEADER))
    _assert_forward_action(actions[1])
    assert result.accepted is True
    assert result.media_outcome == "error"
    assert result.media_stage == "media_send"
    assert result.media_error_type == "ActionFailed"
    assert CANARY not in repr(result)
    assert CANARY not in caplog.text


async def _scheduler_stack(db, send_func):
    storage = storage_module.SubscriptionStorage(db)
    await storage.add(SESSION_ID, "tag", "契约测试")
    gates = session_gate_module.SessionGateRegistry()
    source = AsyncMock()
    service = subscription_module.SubscriptionService(db, source, gates)
    queue = delivery_module.DeliveryQueue(
        db,
        gates,
        clock=lambda: 1000,
        token_factory=lambda: "contract-lease",
    )
    scheduler = scheduler_module.SubscriptionScheduler(
        storage,
        source,
        db,
        send_func,
        block_storage=author_block_module.AuthorBlockStorage(db, gates),
        gates=gates,
        subscription_service=service,
        delivery_queue=queue,
    )
    snapshot = await service.capture_snapshot(SESSION_ID)
    batch = delivery_module.SourceBatch(
        delivery_module.source_for_subscription(snapshot.subscriptions[0]),
        (_post(),),
    )
    return scheduler, batch


async def _delivery_row(db):
    return await db.transaction(
        lambda conn: conn.execute(
            """
            SELECT status,lease_token,lease_until,next_attempt_at,attempts,
                   last_error_type,last_error,accepted_at
            FROM deliveries WHERE session_id=?
            """,
            (SESSION_ID,),
        ).fetchone()
    )


async def _database_dump(db) -> str:
    return await db.transaction(lambda conn: "\n".join(conn.iterdump()))


@pytest.mark.asyncio
async def test_scheduler_accepts_and_marks_seen_after_real_adapter_send(
    runtime, tmp_path
):
    db = main.LofterDB(str(tmp_path / "adapter-success.db"))
    await db.initialize()
    try:
        scheduler, batch = await _scheduler_stack(db, runtime.plugin._send_push)
        with patch.object(
            scheduler_module,
            "_fetch_snapshot_batches",
            AsyncMock(return_value=[batch]),
        ):
            actions, _ = await _exchange(
                runtime, lambda: scheduler._poll_single_session(SESSION_ID)
            )

        _assert_text_action(actions[0], main.format_post(_post(), header=HEADER))
        _assert_forward_action(actions[1])
        assert await _delivery_row(db) == (
            "accepted",
            None,
            None,
            None,
            0,
            None,
            None,
            1000,
        )
        assert await db.filter_unseen_session(
            SESSION_ID, "tag", [_post().post_id]
        ) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_scheduler_backs_off_without_seen_after_adapter_failure(
    runtime, tmp_path, caplog
):
    db = main.LofterDB(str(tmp_path / "adapter-failure.db"))
    await db.initialize()
    try:
        scheduler, batch = await _scheduler_stack(db, runtime.plugin._send_push)
        with patch.object(
            scheduler_module,
            "_fetch_snapshot_batches",
            AsyncMock(return_value=[batch]),
        ):
            actions, _ = await _exchange(
                runtime,
                lambda: scheduler._poll_single_session(SESSION_ID),
                fail_primary=True,
            )

        _assert_text_action(actions[0], main.format_post(_post(), header=HEADER))
        assert await _delivery_row(db) == (
            "pending",
            None,
            None,
            1060,
            1,
            "send_rejected",
            "adapter rejected delivery",
            None,
        )
        assert await db.filter_unseen_session(
            SESSION_ID, "tag", [_post().post_id]
        ) == [_post().post_id]
        assert CANARY not in await _database_dump(db)
        assert CANARY not in caplog.text
    finally:
        await db.close()
