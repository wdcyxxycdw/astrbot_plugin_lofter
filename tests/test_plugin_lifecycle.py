from dataclasses import replace
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

    def __init__(self, message_str: str, platform_name: str = "test"):
        self.message_str = message_str
        self._platform_name = platform_name

    def is_admin(self):
        return True

    def get_platform_name(self):
        return self._platform_name

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
        post = next((
            post
            for posts in self.tags.values()
            for post in posts
            if post.url == url
        ), None)
        if post is None:
            raise AssertionError(f"missing fake detail post: {url}")
        return replace(
            post,
            images=[f"https://img.example/{post.post_id}.jpg"],
            completeness=post.completeness | {"images"},
        )


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
async def test_send_push_normalizes_framework_completion_and_false_on_error():
    main = _load_main_module()
    plugin = object.__new__(main.LofterPlugin)
    plugin._max_images = 2
    platform = SimpleNamespace(meta=lambda: SimpleNamespace(name="test"))
    plugin.context = SimpleNamespace(
        get_platform_inst=MagicMock(return_value=platform),
        send_message=AsyncMock(return_value=True),
    )
    post = Post(
        post_id="send", title="text", summary="",
        url="https://u.lofter.com/post/send",
    )
    args = ("adapter:FriendMessage:sess", post, "【header】", frozenset({"tag"}))
    assert await main.LofterPlugin._send_push(plugin, *args) is True
    plugin.context.get_platform_inst.assert_called_with("adapter")
    plugin.context.send_message.return_value = False
    assert await main.LofterPlugin._send_push(plugin, *args) is False
    plugin.context.send_message.return_value = None
    assert await main.LofterPlugin._send_push(plugin, *args) is True
    plugin.context.send_message.side_effect = RuntimeError("send failed")
    assert await main.LofterPlugin._send_push(plugin, *args) is False


@pytest.mark.asyncio
async def test_send_push_qq_tag_uses_share_and_all_image_nodes():
    main = _load_main_module()
    main.Comp = SimpleNamespace(
        Plain=lambda text: SimpleNamespace(kind="plain", text=text),
        Image=SimpleNamespace(
            fromURL=lambda url: SimpleNamespace(kind="image", url=url)
        ),
        Share=lambda **kwargs: SimpleNamespace(kind="share", **kwargs),
        Node=lambda **kwargs: SimpleNamespace(kind="node", **kwargs),
        Nodes=lambda **kwargs: SimpleNamespace(kind="nodes", **kwargs),
    )
    main.MessageChain = lambda components: SimpleNamespace(chain=components)
    platform = SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp"))
    context = SimpleNamespace(
        get_platform_inst=MagicMock(return_value=platform),
        send_message=AsyncMock(return_value=True),
    )
    plugin = object.__new__(main.LofterPlugin)
    plugin._max_images = 1
    plugin.context = context
    images = [f"https://img.example/{index}.jpg" for index in range(3)]
    post = Post(
        post_id="send", title="标题", summary="摘要", author="作者",
        tags=["标签"], images=images,
        url="https://u.lofter.com/post/send",
        completeness=frozenset({
            "title", "summary", "author", "tags", "images", "url"
        }),
    )

    accepted = await main.LofterPlugin._send_push(
        plugin, "qq-id:GroupMessage:sess", post, "【标签新内容】",
        frozenset({"tag"}),
    )

    assert accepted is True
    assert context.send_message.await_count == 2
    primary = context.send_message.await_args_list[0].args[1].chain
    media = context.send_message.await_args_list[1].args[1].chain
    assert [item.kind for item in primary] == ["share"]
    assert primary[0].url == post.url
    assert primary[0].title == "标题"
    assert primary[0].image == images[0]
    assert "【标签新内容】" in primary[0].content
    assert "作者：作者" in primary[0].content
    assert "#标签" in primary[0].content
    assert "摘要" in primary[0].content
    assert post.url not in primary[0].content
    assert [item.kind for item in media] == ["nodes"]
    assert [node.content[0].url for node in media[0].nodes] == images


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("media_result", "outcome", "error_type"),
    [
        (False, "rejected", None),
        (RuntimeError("private media canary"), "error", "RuntimeError"),
    ],
)
async def test_send_push_qq_media_failure_keeps_primary_accepted(
    media_result, outcome, error_type, caplog
):
    main = _load_main_module()
    main.Comp = SimpleNamespace(
        Image=SimpleNamespace(
            fromURL=lambda url: SimpleNamespace(kind="image", url=url)
        ),
        Share=lambda **kwargs: SimpleNamespace(kind="share", **kwargs),
        Node=lambda **kwargs: SimpleNamespace(kind="node", **kwargs),
        Nodes=lambda **kwargs: SimpleNamespace(kind="nodes", **kwargs),
    )
    main.MessageChain = lambda components: SimpleNamespace(chain=components)
    platform = SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp"))
    context = SimpleNamespace(
        get_platform_inst=MagicMock(return_value=platform),
        send_message=AsyncMock(side_effect=[True, media_result]),
    )
    plugin = object.__new__(main.LofterPlugin)
    plugin._max_images = 1
    plugin.context = context
    post = Post(
        post_id="send",
        title="标题",
        summary="",
        images=["https://img.example/private.jpg"],
        url="https://u.lofter.com/post/send",
        completeness=frozenset({"title", "images", "url"}),
    )

    result = await main.LofterPlugin._send_push_result(
        plugin,
        "qq-id:GroupMessage:private-session",
        post,
        "【标签新内容】",
        frozenset({"tag"}),
    )

    assert result.accepted is True
    assert result.primary_outcome == "accepted"
    assert result.media_outcome == outcome
    assert result.media_stage == "media_send"
    assert result.media_error_type == error_type
    assert context.send_message.await_count == 2
    assert "private media canary" not in caplog.text
    assert "private-session" not in caplog.text
    assert post.url not in caplog.text


@pytest.mark.asyncio
async def test_send_push_primary_error_reports_safe_stage(caplog):
    main = _load_main_module()
    main.Comp = SimpleNamespace(
        Plain=lambda text: SimpleNamespace(kind="plain", text=text),
        Image=SimpleNamespace(
            fromURL=lambda url: SimpleNamespace(kind="image", url=url)
        ),
    )
    main.MessageChain = lambda components: SimpleNamespace(chain=components)
    platform = SimpleNamespace(meta=lambda: SimpleNamespace(name="test"))
    context = SimpleNamespace(
        get_platform_inst=MagicMock(return_value=platform),
        send_message=AsyncMock(
            side_effect=RuntimeError("private primary canary")
        ),
    )
    plugin = object.__new__(main.LofterPlugin)
    plugin._max_images = 1
    plugin.context = context
    post = Post(
        post_id="send",
        title="标题",
        summary="",
        url="https://u.lofter.com/post/send",
    )

    result = await main.LofterPlugin._send_push_result(
        plugin,
        "adapter:FriendMessage:private-session",
        post,
        "【标签新内容】",
        frozenset({"tag"}),
    )

    assert result.accepted is False
    assert result.primary_outcome == "error"
    assert result.primary_stage == "primary_send"
    assert result.primary_error_type == "RuntimeError"
    assert result.media_outcome == "not_applicable"
    assert "private primary canary" not in caplog.text
    assert "private-session" not in caplog.text
    assert post.url not in caplog.text


@pytest.mark.asyncio
async def test_send_push_qq_tag_without_images_uses_share_only():
    main = _load_main_module()
    main.Comp = SimpleNamespace(
        Share=lambda **kwargs: SimpleNamespace(kind="share", **kwargs),
    )
    main.MessageChain = lambda components: SimpleNamespace(chain=components)
    platform = SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp"))
    context = SimpleNamespace(
        get_platform_inst=MagicMock(return_value=platform),
        send_message=AsyncMock(return_value=True),
    )
    plugin = object.__new__(main.LofterPlugin)
    plugin._max_images = 1
    plugin.context = context
    post = Post(
        post_id="send", title="标题", summary="摘要",
        url="https://u.lofter.com/post/send",
        completeness=frozenset({"title", "summary", "images", "url"}),
    )

    await main.LofterPlugin._send_push(
        plugin, "qq-id:GroupMessage:sess", post, "【标签新内容】",
        frozenset({"tag"}),
    )

    chain = context.send_message.await_args.args[1].chain
    assert len(chain) == 1
    assert chain[0].kind == "share"


@pytest.mark.asyncio
async def test_send_push_qq_blog_uses_share_only():
    main = _load_main_module()
    main.Comp = SimpleNamespace(
        Share=lambda **kwargs: SimpleNamespace(kind="share", **kwargs),
    )
    main.MessageChain = lambda components: SimpleNamespace(chain=components)
    platform = SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp"))
    context = SimpleNamespace(
        get_platform_inst=MagicMock(return_value=platform),
        send_message=AsyncMock(return_value=True),
    )
    plugin = object.__new__(main.LofterPlugin)
    plugin._max_images = 1
    plugin.context = context
    post = Post(
        post_id="send", title="标题", summary="摘要",
        images=["https://img.example/1.jpg"],
        url="https://u.lofter.com/post/send",
        completeness=frozenset({"title", "summary", "images", "url"}),
    )

    await main.LofterPlugin._send_push(
        plugin, "qq-id:GroupMessage:sess", post, "【博主新内容】",
        frozenset({"blog"}),
    )

    chain = context.send_message.await_args.args[1].chain
    assert len(chain) == 1
    assert chain[0].kind == "share"


@pytest.mark.asyncio
async def test_send_push_non_qq_uses_plain_and_image_limit():
    main = _load_main_module()
    main.Comp = SimpleNamespace(
        Plain=lambda text: SimpleNamespace(kind="plain", text=text),
        Image=SimpleNamespace(
            fromURL=lambda url: SimpleNamespace(kind="image", url=url)
        ),
    )
    main.MessageChain = lambda components: SimpleNamespace(chain=components)
    platform = SimpleNamespace(meta=lambda: SimpleNamespace(name="telegram"))
    context = SimpleNamespace(
        get_platform_inst=MagicMock(return_value=platform),
        send_message=AsyncMock(return_value=True),
    )
    plugin = object.__new__(main.LofterPlugin)
    plugin._max_images = 2
    plugin.context = context
    images = [f"https://img.example/{index}.jpg" for index in range(3)]
    post = Post(
        post_id="send", title="标题", summary="摘要", images=images,
        url="https://u.lofter.com/post/send",
        completeness=frozenset({"title", "summary", "images", "url"}),
    )

    await main.LofterPlugin._send_push(
        plugin, "tg-id:GroupMessage:sess", post, "【标签新内容】",
        frozenset({"tag"}),
    )

    chain = context.send_message.await_args.args[1].chain
    assert chain[0].kind == "plain"
    assert [item.url for item in chain[1:]] == images[:2]


@pytest.mark.asyncio
async def test_scheduler_uses_production_send_push_acceptance_boundary(tmp_path):
    main = _load_main_module()
    context = SimpleNamespace(
        get_platform_inst=MagicMock(
            return_value=SimpleNamespace(
                meta=lambda: SimpleNamespace(name="test")
            )
        ),
        send_message=AsyncMock(return_value=False),
    )
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

            context.send_message.return_value = None
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
async def test_scheduler_accepts_qq_primary_when_media_send_fails(tmp_path):
    main = _load_main_module()
    main.Comp = SimpleNamespace(
        Plain=lambda text: SimpleNamespace(kind="plain", text=text),
        Image=SimpleNamespace(
            fromURL=lambda url: SimpleNamespace(kind="image", url=url)
        ),
        Share=lambda **kwargs: SimpleNamespace(kind="share", **kwargs),
        Node=lambda **kwargs: SimpleNamespace(kind="node", **kwargs),
        Nodes=lambda **kwargs: SimpleNamespace(kind="nodes", **kwargs),
    )
    main.MessageChain = lambda components: SimpleNamespace(chain=components)
    platform = SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp"))
    context = SimpleNamespace(
        get_platform_inst=MagicMock(return_value=platform),
        send_message=AsyncMock(
            side_effect=[True, RuntimeError("private media canary")]
        ),
    )
    config = {"max_images": 2, "search_limit": 3, "poll_interval": 30}
    with patch.object(
        main.StarTools, "get_data_dir", return_value=str(tmp_path), create=True
    ):
        plugin = main.LofterPlugin(context, config)

    session_id = "qq-id:GroupMessage:private-session"
    post = Post(
        post_id="new",
        title="new",
        summary="",
        tags=["tag"],
        images=["https://img.example/private.jpg"],
        url="https://u.lofter.com/post/new",
        publish_time="2026-07-29 05:00:00",
        completeness=frozenset({
            "title", "summary", "tags", "images", "url", "publish_time"
        }),
    )
    scheduler = plugin._scheduler
    scheduler_module = __import__(
        scheduler.__class__.__module__, fromlist=["fetch_tag_posts"]
    )

    await plugin._db.initialize()
    try:
        await plugin._storage.add(session_id, "tag", "tag")
        await plugin._db.mark_seen_session(session_id, "tag", ["warmup"])
        with patch.object(
            scheduler_module, "fetch_tag_posts", AsyncMock(return_value=[post])
        ):
            await scheduler._poll_single_session(session_id)
            await scheduler._poll_single_session(session_id)

        assert context.send_message.await_count == 2
        primary = context.send_message.await_args_list[0].args[1].chain
        media = context.send_message.await_args_list[1].args[1].chain
        assert [item.kind for item in primary] == ["share"]
        assert [item.kind for item in media] == ["nodes"]
        assert await plugin._db.transaction(
            lambda conn: conn.execute(
                "SELECT status,attempts,lease_token FROM deliveries "
                "WHERE session_id=? AND post_id=?",
                (session_id, post.post_id),
            ).fetchone()
        ) == ("accepted", 0, None)
        assert await plugin._db.filter_unseen_session(
            session_id, "tag", [post.post_id]
        ) == []
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
async def test_cli_preview_qq_reuses_share_and_all_image_nodes(tmp_path):
    main = _load_main_module()
    main.Comp = SimpleNamespace(
        Plain=lambda text: SimpleNamespace(kind="plain", text=text),
        Image=SimpleNamespace(
            fromURL=lambda url: SimpleNamespace(kind="image", url=url)
        ),
        Share=lambda **kwargs: SimpleNamespace(kind="share", **kwargs),
        Node=lambda **kwargs: SimpleNamespace(kind="node", **kwargs),
        Nodes=lambda **kwargs: SimpleNamespace(kind="nodes", **kwargs),
    )
    plugin = await _make_command_plugin(main, tmp_path)
    images = [f"https://img.example/{index}.jpg" for index in range(3)]
    post = Post(
        post_id="old", title="标题", summary="摘要", images=images,
        url="https://u.lofter.com/post/old",
        publish_time="2026-07-29 05:00:00",
        completeness=frozenset({
            "title", "summary", "images", "url", "publish_time"
        }),
    )
    plugin._source.tags["A"] = [post]
    plugin._source.get_post = AsyncMock(return_value=post)
    try:
        event = _AdminCommandEvent(
            "/lofter subtagpreview A", platform_name="aiocqhttp"
        )
        results = [
            item async for item in main.LofterPlugin.sub_tag_preview(
                plugin, event
            )
        ]
    finally:
        await plugin._db.close()

    chain = results[1]
    assert [item.kind for item in chain] == ["share", "nodes"]
    assert [node.content[0].url for node in chain[1].nodes] == images


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
