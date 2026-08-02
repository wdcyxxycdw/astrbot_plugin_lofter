from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.instance_lock import InstanceLockHeldError
from core.llm_tools import LofterLLMToolsMixin
from core.parser import Post
from core.source_scan import SourcePage
from tests.test_command_permissions import _load_main_module


class _AdminCommandEvent:
    unified_msg_origin = "sess"

    def __init__(self, message_str: str):
        self.message_str = message_str

    def is_admin(self):
        return True

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return chain


class _FakeSubscriptionSource:
    def __init__(self):
        self.tags = {}

    async def list_tag(self, tag, cursor, limit, sort):
        posts = list(self.tags.get(tag, []))
        return SourcePage(
            items=posts,
            source="fake",
            next_cursor=None,
            exhausted=True,
            sort=sort,
            mapped_count=len(posts),
            dropped_count=0,
            complete=True,
        )

    async def list_blog(self, username, cursor, limit):
        return SourcePage(
            items=[],
            source="fake",
            next_cursor=None,
            exhausted=True,
            sort="new",
            mapped_count=0,
            dropped_count=0,
            complete=True,
        )

    async def get_post(self, url):
        raise AssertionError(f"unexpected detail fetch: {url}")


async def _make_command_plugin(main, tmp_path):
    plugin = object.__new__(main.LofterPlugin)
    plugin._db = main.LofterDB(str(tmp_path / "commands.db"))
    await plugin._db.initialize()
    plugin._storage = main.SubscriptionStorage(plugin._db)
    plugin._session_gates = main.SessionGateRegistry()
    plugin._source = _FakeSubscriptionSource()
    plugin._subscriptions = main.SubscriptionService(
        plugin._db, plugin._source, plugin._session_gates
    )
    plugin._author_blocks = main.AuthorBlockStorage(
        plugin._db, plugin._session_gates
    )
    plugin._max_images = 2
    return plugin


@pytest.mark.asyncio
async def test_plugin_acquires_instance_lock_before_database_and_scheduler():
    main = _load_main_module()
    plugin = object.__new__(main.LofterPlugin)
    order = []
    plugin._instance_lock = SimpleNamespace(
        acquire=lambda: order.append("lock"),
        release=lambda: order.append("release"),
    )
    plugin._db = SimpleNamespace(
        initialize=AsyncMock(side_effect=lambda: order.append("db")),
        get_config=AsyncMock(return_value=None),
        close=AsyncMock(side_effect=lambda: order.append("db-close")),
    )
    plugin._migrate_json_once = AsyncMock(side_effect=lambda: order.append("json"))
    plugin._initialize_locked = main.LofterPlugin._initialize_locked.__get__(plugin)
    plugin._config_cookie = ""
    plugin._source = SimpleNamespace(
        update_cookie=lambda cookie: order.append("cookie"),
        initialize=AsyncMock(side_effect=lambda: order.append("http")),
        close=AsyncMock(side_effect=lambda: order.append("http-close")),
    )
    plugin._scheduler = SimpleNamespace(
        start=lambda: order.append("scheduler"),
        stop=AsyncMock(side_effect=lambda: order.append("scheduler-stop")),
    )

    await main.LofterPlugin.initialize(plugin)

    assert order[:6] == ["lock", "db", "json", "cookie", "http", "scheduler"]


@pytest.mark.asyncio
async def test_second_plugin_lock_failure_creates_no_db_or_scheduler_activity():
    main = _load_main_module()
    plugin = object.__new__(main.LofterPlugin)
    plugin._instance_lock = SimpleNamespace(
        acquire=MagicMock(side_effect=InstanceLockHeldError("held")),
        release=MagicMock(),
    )
    plugin._db = SimpleNamespace(initialize=AsyncMock(), close=AsyncMock())
    plugin._initialize_locked = main.LofterPlugin._initialize_locked.__get__(plugin)
    plugin._source = SimpleNamespace(initialize=AsyncMock(), close=AsyncMock())
    plugin._scheduler = SimpleNamespace(start=MagicMock(), stop=AsyncMock())

    with pytest.raises(InstanceLockHeldError):
        await main.LofterPlugin.initialize(plugin)

    plugin._db.initialize.assert_not_awaited()
    plugin._source.initialize.assert_not_awaited()
    plugin._source.close.assert_not_awaited()
    plugin._scheduler.start.assert_not_called()
    plugin._scheduler.stop.assert_not_awaited()
    plugin._db.close.assert_not_awaited()
    plugin._instance_lock.release.assert_not_called()


@pytest.mark.asyncio
async def test_send_push_returns_adapter_acceptance_and_false_on_error():
    main = _load_main_module()
    plugin = object.__new__(main.LofterPlugin)
    plugin._max_images = 2
    plugin.context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    assert await main.LofterPlugin._send_push(plugin, "sess", "text", []) is True
    plugin.context.send_message.return_value = False
    assert await main.LofterPlugin._send_push(plugin, "sess", "text", []) is False
    plugin.context.send_message.return_value = None
    assert await main.LofterPlugin._send_push(plugin, "sess", "text", []) is False
    plugin.context.send_message.side_effect = RuntimeError("send failed")
    assert await main.LofterPlugin._send_push(plugin, "sess", "text", []) is False


@pytest.mark.asyncio
async def test_scheduler_uses_production_send_push_acceptance_boundary(tmp_path):
    main = _load_main_module()
    context = SimpleNamespace(send_message=AsyncMock(return_value=False))
    config = {"max_images": 2, "search_limit": 3, "poll_interval": 30}
    with patch.object(
        main.StarTools, "get_data_dir", return_value=str(tmp_path), create=True
    ):
        plugin = main.LofterPlugin(context, config)
    scheduler = plugin._scheduler
    assert scheduler._send_func.__self__ is plugin
    assert scheduler._send_func.__func__ is main.LofterPlugin._send_push

    await plugin._db.initialize()
    post = Post(
        post_id="new", title="new", summary="", tags=["tag"],
        url="https://u.lofter.com/post/new",
        publish_time="2026-07-29 05:00:00",
    )
    scheduler_module = __import__(
        scheduler.__class__.__module__, fromlist=["fetch_tag_posts"]
    )
    try:
        await plugin._storage.add("sess", "tag", "tag")
        await plugin._db.mark_seen_session("sess", "tag", ["warmup"])
        with patch.object(
            scheduler_module, "fetch_tag_posts", AsyncMock(return_value=[post])
        ):
            await scheduler._poll_all()
            assert await plugin._db.filter_unseen_session(
                "sess", "tag", ["new"]
            ) == ["new"]
            assert await plugin._db.filter_unsent("sess", ["new"]) == ["new"]

            context.send_message.return_value = True
            await plugin._db.transaction(
                lambda conn: conn.execute(
                    "UPDATE deliveries SET next_attempt_at=NULL "
                    "WHERE session_id='sess' AND post_id='new'"
                )
            )
            await scheduler._poll_all()
        assert await plugin._db.filter_unseen_session("sess", "tag", ["new"]) == []
        assert await plugin._db.filter_unsent("sess", ["new"]) == []
    finally:
        await plugin._db.close()


@pytest.mark.asyncio
async def test_subscription_replacement_keeps_inherited_seen(tmp_path):
    main = _load_main_module()
    plugin = await _make_command_plugin(main, tmp_path)
    old = Post(
        post_id="old",
        title="old",
        summary="",
        url="https://u.lofter.com/post/old",
        publish_time="2026-07-29 05:00:00",
    )
    plugin._source.tags["A"] = [old]
    try:
        await plugin._subscriptions.subscribe_tags("sess", ["A"], [])
        await plugin._subscriptions.subscribe_tags("sess", ["B"], [])
        await plugin._subscriptions.remove("sess", "tag", "A")
        assert await plugin._db.filter_unseen_session(
            "sess", "tag", ["old"]
        ) == []
    finally:
        await plugin._db.close()


@pytest.mark.asyncio
async def test_cli_preview_then_subscription_replacement_keeps_seen(tmp_path):
    main = _load_main_module()
    plugin = await _make_command_plugin(main, tmp_path)
    post = Post(
        post_id="old",
        title="old",
        summary="",
        url="https://u.lofter.com/post/old",
        publish_time="2026-07-29 05:00:00",
    )
    plugin._source.tags["A"] = [post]
    try:
        event = _AdminCommandEvent("/lofter subtagpreview A")
        results = [
            item async for item in main.LofterPlugin.sub_tag_preview(
                plugin, event
            )
        ]
        assert results
        await plugin._subscriptions.subscribe_tags("sess", ["B"], [])
        await plugin._subscriptions.remove("sess", "tag", "A")
        assert await plugin._db.filter_unseen_session(
            "sess", "tag", ["old"]
        ) == []
    finally:
        await plugin._db.close()


@pytest.mark.asyncio
async def test_llm_preview_then_subscription_replacement_keeps_seen(tmp_path):
    main = _load_main_module()
    plugin = await _make_command_plugin(main, tmp_path)
    post = Post(
        post_id="old",
        title="old",
        summary="",
        url="https://u.lofter.com/post/old",
        publish_time="2026-07-29 05:00:00",
    )
    plugin._source.tags["A"] = [post]
    try:
        event = _AdminCommandEvent("")
        result = await LofterLLMToolsMixin.lofter_subscription(
            plugin, event, "preview_tag", target="A"
        )
        assert "已订阅标签" in result
        await plugin._subscriptions.subscribe_tags("sess", ["B"], [])
        await plugin._subscriptions.remove("sess", "tag", "A")
        assert await plugin._db.filter_unseen_session(
            "sess", "tag", ["old"]
        ) == []
    finally:
        await plugin._db.close()


@pytest.mark.asyncio
async def test_initialize_failure_and_terminate_release_instance_lock():
    main = _load_main_module()
    plugin = object.__new__(main.LofterPlugin)
    plugin._instance_lock = SimpleNamespace(acquire=MagicMock(), release=MagicMock())
    plugin._db = SimpleNamespace(
        initialize=AsyncMock(side_effect=RuntimeError("db failed")),
        close=AsyncMock(),
    )
    plugin._initialize_locked = main.LofterPlugin._initialize_locked.__get__(plugin)
    plugin._source = SimpleNamespace(close=AsyncMock())
    plugin._scheduler = SimpleNamespace(start=MagicMock(), stop=AsyncMock())

    with pytest.raises(RuntimeError, match="db failed"):
        await main.LofterPlugin.initialize(plugin)
    plugin._scheduler.stop.assert_awaited_once()
    plugin._source.close.assert_awaited_once()
    plugin._db.close.assert_awaited_once()
    plugin._instance_lock.release.assert_called_once()

    plugin._scheduler.stop.reset_mock()
    plugin._source.close.reset_mock()
    plugin._db.close.reset_mock()
    plugin._instance_lock.release.reset_mock()
    await main.LofterPlugin.terminate(plugin)
    plugin._scheduler.stop.assert_awaited_once()
    plugin._source.close.assert_awaited_once()
    plugin._db.close.assert_awaited_once()
    plugin._instance_lock.release.assert_called_once()
