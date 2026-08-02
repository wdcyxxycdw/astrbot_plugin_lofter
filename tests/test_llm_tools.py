import importlib
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.author_block import AuthorBlock, normalize_author_query
from core.llm_tools import LofterLLMToolsMixin, MAX_TOOL_INTEGER
from core.permissions import ADMIN_ONLY_MESSAGE
from core.source_scan import SourcePage
from core.storage import Subscription


def test_llm_tool_parameter_annotations_are_runtime_types():
    expected = {
        "lofter_subscription": {"action": str, "target": str, "index": int},
        "lofter_author_block": {"action": str, "author": str},
        "lofter_content": {"action": str, "query": str, "limit": int},
        "lofter_count": {"action": str, "name": str, "expression": str, "target": str},
    }

    for method_name, annotations in expected.items():
        actual = getattr(LofterLLMToolsMixin, method_name).__annotations__
        for param_name, param_type in annotations.items():
            assert actual[param_name] is param_type
            assert not isinstance(actual[param_name], str)


def test_llm_tool_docstrings_use_astrbot_arg_type_format():
    expected = {
        "lofter_subscription": ("action(string):", "target(string):", "index(number):"),
        "lofter_author_block": ("action(string):", "author(string):"),
        "lofter_content": ("action(string):", "query(string):", "limit(number):"),
        "lofter_count": ("action(string):", "name(string):", "expression(string):", "target(string):"),
    }

    for method_name, declarations in expected.items():
        doc = getattr(LofterLLMToolsMixin, method_name).__doc__ or ""
        for declaration in declarations:
            assert declaration in doc


def test_llm_tool_handler_modules_map_to_plugin_main_module():
    import core.llm_tools as llm_tools

    assert llm_tools._plugin_main_module_path("core.llm_tools") == "main"
    assert (
        llm_tools._plugin_main_module_path("data.plugins.astrbot_plugin_lofter.core.llm_tools")
        == "data.plugins.astrbot_plugin_lofter.main"
    )

    for method_name in ("lofter_subscription", "lofter_author_block", "lofter_content", "lofter_count"):
        assert getattr(LofterLLMToolsMixin, method_name).__module__ == "main"


def test_llm_tools_does_not_swallow_astrbot_import_chain_errors():
    import core.llm_tools as llm_tools

    original_modules = {name: sys.modules.get(name) for name in ("astrbot", "astrbot.api", "astrbot.api.event")}
    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")

    def fail_imported_attr(name):
        if name in {"AstrMessageEvent", "filter"}:
            raise RuntimeError("astrbot api import chain failed")
        raise AttributeError(name)

    event_mod.__getattr__ = fail_imported_attr
    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.event"] = event_mod
    try:
        with pytest.raises(RuntimeError, match="astrbot api import chain failed"):
            importlib.reload(llm_tools)
    finally:
        for name, module in original_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        importlib.reload(llm_tools)


class LLMToolEvent:
    def __init__(self, session_id="sess", is_admin=True):
        self.unified_msg_origin = session_id
        self._is_admin = is_admin

    def is_admin(self):
        return self._is_admin

    def plain_result(self, text: str):
        return text


class FakeStorage:
    def __init__(self):
        self.subs = []
        self.removed_ids = []
        self.removed = []
        self.next_id = 1

    async def add(self, session_id, sub_type, target, role="subscribe"):
        for sub in self.subs:
            if (sub.session_id, sub.type, sub.role, sub.target) == (session_id, sub_type, role, target):
                return False
        self.subs.append(Subscription(self.next_id, session_id, sub_type, role, target))
        self.next_id += 1
        return True

    async def remove(self, session_id, sub_type, target, role="subscribe"):
        for sub in list(self.subs):
            if (sub.session_id, sub.type, sub.role, sub.target) == (session_id, sub_type, role, target):
                self.subs.remove(sub)
                self.removed.append((session_id, sub_type, target, role))
                return True
        return False

    async def remove_by_id(self, sub_id):
        for sub in list(self.subs):
            if sub.id == sub_id:
                self.subs.remove(sub)
                self.removed_ids.append(sub_id)
                return True
        return False

    async def list_by_session(self, session_id):
        return [sub for sub in self.subs if sub.session_id == session_id]


class FakeAuthorBlocks:
    def __init__(self):
        self.blocks = []

    async def add(self, session_id, raw):
        added = False
        for kind, value, display in normalize_author_query(raw):
            item = (session_id, kind, value, display)
            if item not in self.blocks:
                self.blocks.append(item)
                added = True
        return added

    async def remove(self, session_id, raw):
        removed = False
        for kind, value, _ in normalize_author_query(raw):
            for item in list(self.blocks):
                if item[:3] == (session_id, kind, value):
                    self.blocks.remove(item)
                    removed = True
        return removed

    async def list_by_session(self, session_id):
        return [AuthorBlock(sid, kind, value, display) for sid, kind, value, display in self.blocks if sid == session_id]


class FakeSubscriptionService:
    def __init__(self, storage):
        self._storage = storage
        self.tag_calls = []
        self.blog_calls = []

    async def subscribe_tags(
        self, session_id, subscribes, excludes, *, preview=False
    ):
        self.tag_calls.append(
            (session_id, list(subscribes), list(excludes), preview)
        )
        added_subs = []
        added_excls = []
        for tag in subscribes:
            if await self._storage.add(
                session_id, "tag", tag, "subscribe"
            ):
                added_subs.append(tag)
        for tag in excludes:
            if await self._storage.add(
                session_id, "tag", tag, "exclude"
            ):
                added_excls.append(tag)
        return types.SimpleNamespace(
            added_subscribes=tuple(added_subs),
            added_excludes=tuple(added_excls),
            preview_posts=(),
        )

    async def subscribe_blog(self, session_id, username):
        self.blog_calls.append((session_id, username))
        added = await self._storage.add(session_id, "blog", username)
        return types.SimpleNamespace(
            added_subscribes=(username,) if added else (),
            added_excludes=(),
            preview_posts=(),
        )

    async def remove(
        self, session_id, sub_type, target, role="subscribe"
    ):
        return await self._storage.remove(
            session_id, sub_type, target, role
        )

    async def remove_by_index(self, session_id, index):
        subs = await self._storage.list_by_session(session_id)
        if index < 1 or index > len(subs):
            return None, len(subs)
        sub = subs[index - 1]
        await self._storage.remove_by_id(sub.id)
        return sub, len(subs)


class LLMToolRunner(LofterLLMToolsMixin):
    def __init__(self):
        self._storage = FakeStorage()
        self._subscriptions = FakeSubscriptionService(self._storage)
        self._author_blocks = FakeAuthorBlocks()
        self._max_images = 3


@pytest.mark.asyncio
async def test_all_llm_tools_reject_before_parameter_use_and_side_effects():
    runner = LLMToolRunner()
    event = LLMToolEvent(is_admin=False)

    results = [
        await runner.lofter_subscription(event, None, target=[]),
        await runner.lofter_author_block(event, None, author={}),
        await ContentRunner().lofter_content(event, None, query=[], limit=False),
        await CountRunner().lofter_count(event, None, name=[], expression={}, target=1),
    ]

    assert results == [ADMIN_ONLY_MESSAGE] * 4
    assert runner._storage.subs == []
    assert runner._author_blocks.blocks == []


@pytest.mark.asyncio
async def test_llm_tools_fail_closed_when_admin_check_missing_or_raises():
    class MissingAdminEvent:
        unified_msg_origin = "sess"

    class RaisingAdminEvent:
        unified_msg_origin = "sess"

        def is_admin(self):
            raise RuntimeError("boom")

    runner = LLMToolRunner()
    for event in (MissingAdminEvent(), RaisingAdminEvent()):
        assert await runner.lofter_subscription(event, "list") == ADMIN_ONLY_MESSAGE
        assert await runner.lofter_author_block(event, "list") == ADMIN_ONLY_MESSAGE
        assert await CountRunner().lofter_count(event, "list") == ADMIN_ONLY_MESSAGE


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, True, 1, 1.5, [], {}])
async def test_llm_string_parameters_reject_non_strings(value):
    event = LLMToolEvent()

    assert "必须是字符串" in await LLMToolRunner().lofter_subscription(event, value)
    assert "必须是字符串" in await LLMToolRunner().lofter_author_block(event, value)
    assert "必须是字符串" in await ContentRunner().lofter_content(event, value)
    assert "必须是字符串" in await CountRunner().lofter_count(event, value)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, True, 1.5, "2", [], {}])
async def test_llm_limit_and_index_reject_non_integer_json_types(value):
    event = LLMToolEvent()

    index_result = await LLMToolRunner().lofter_subscription(
        event, "unsubscribe_index", index=value
    )
    limit_result = await ContentRunner().lofter_content(
        event, "search", query="原神", limit=value
    )

    assert index_result == "参数「index」必须是整数"
    assert limit_result == "参数「limit」必须是整数"


@pytest.mark.asyncio
async def test_llm_limit_and_index_reject_extreme_integers():
    event = LLMToolEvent()
    extreme = MAX_TOOL_INTEGER + 1

    index_result = await LLMToolRunner().lofter_subscription(
        event, "unsubscribe_index", index=extreme
    )
    limit_result = await ContentRunner().lofter_content(
        event, "search", query="原神", limit=-extreme
    )

    assert index_result == "参数「index」超出允许范围"
    assert limit_result == "参数「limit」超出允许范围"


@pytest.mark.asyncio
async def test_llm_subscription_subscribes_tag_with_exclude_and_lists():
    runner = LLMToolRunner()
    event = LLMToolEvent()

    result = await runner.lofter_subscription(event, "subscribe_tag", target="原神 -R18 -剧透")
    listed = await runner.lofter_subscription(event, "list")

    assert result == "新增订阅：原神\n新增排除：R18, 剧透"
    assert "1. [标签｜订阅] 原神" in listed
    assert "2. [标签｜排除] R18" in listed
    assert "3. [标签｜排除] 剧透" in listed
    assert runner._subscriptions.tag_calls == [
        ("sess", ["原神"], ["R18", "剧透"], False)
    ]


@pytest.mark.asyncio
async def test_llm_subscription_unsubscribe_index_uses_list_number():
    runner = LLMToolRunner()
    event = LLMToolEvent()
    await runner._storage.add("sess", "tag", "原神")
    await runner._storage.add("sess", "tag", "R18", "exclude")

    result = await runner.lofter_subscription(event, "unsubscribe_index", index=2)

    assert result == "已删除第 2 条：[标签｜排除] R18"
    assert runner._storage.removed_ids == [2]


@pytest.mark.asyncio
async def test_llm_subscription_unexclude_tag_is_separate_from_unsubscribe_tag():
    runner = LLMToolRunner()
    event = LLMToolEvent()
    await runner._storage.add("sess", "tag", "R18", "exclude")

    result = await runner.lofter_subscription(event, "unexclude_tag", target="R18")

    assert result == "已取消排除标签「R18」"
    assert runner._storage.removed == [("sess", "tag", "R18", "exclude")]


@pytest.mark.asyncio
async def test_llm_author_block_block_list_unblock():
    runner = LLMToolRunner()
    event = LLMToolEvent()

    blocked = await runner.lofter_author_block(event, "block", author="https://SomeUser.lofter.com")
    listed = await runner.lofter_author_block(event, "list")
    unblocked = await runner.lofter_author_block(event, "unblock", author="https://SomeUser.lofter.com")

    assert blocked == "已屏蔽作者「https://SomeUser.lofter.com」"
    assert "当前屏蔽作者列表：" in listed
    assert "[用户名] SomeUser" in listed
    assert unblocked == "已解除屏蔽作者「https://SomeUser.lofter.com」"
    assert await runner.lofter_author_block(event, "list") == "当前没有屏蔽作者"


from core.parser import Post


class FakeSource:
    async def list_tag(self, keyword, cursor, limit, sort):
        assert (keyword, cursor, limit, sort) == ("原神", None, 2, "new")
        return SourcePage(
            items=[
                Post("1", "可见标题", "摘要", ["img1", "img2", "img3"], "作者A", "visible", "https://visible.lofter.com/post/1", ["原神"], "2026-05-15"),
                Post("2", "屏蔽标题", "摘要", [], "作者B", "blocked", "https://blocked.lofter.com/post/2", ["原神"], "2026-05-15"),
            ],
            source="mobile_tag",
            next_cursor=None,
            exhausted=True,
            sort="new",
            mapped_count=2,
            dropped_count=0,
            complete=True,
        )


class FakeContentAuthorBlocks:
    async def list_by_session(self, session_id):
        from core.author_block import AuthorBlock

        return [AuthorBlock(session_id, "username", "blocked", "blocked")]


class ContentRunner(LofterLLMToolsMixin):
    def __init__(self):
        self._source = FakeSource()
        self._author_blocks = FakeContentAuthorBlocks()
        self._search_limit = 3
        self._max_images = 2


@pytest.mark.asyncio
async def test_llm_content_requires_search_query():
    runner = ContentRunner()
    event = LLMToolEvent()

    result = await runner.lofter_content(event, "search", query="")

    assert result == "请提供标签名，例如：action=search, query=原创"


@pytest.mark.asyncio
async def test_llm_content_search_filters_blocked_authors():
    runner = ContentRunner()
    event = LLMToolEvent()

    result = await runner.lofter_content(event, "search", query="原神", limit=2)

    assert "「原神」标签搜索结果，共 1 条：" in result
    assert "可见标题" in result
    assert "图片：img1" in result
    assert "图片：img2" in result
    assert "img3" not in result
    assert "屏蔽标题" not in result


class FakeCountDB:
    def __init__(self):
        self.rows = [("米哈游", "原神"), ("方舟", "明日方舟")]
        self.upserts = []

    async def list_count_conditions(self):
        return list(self.rows)

    async def delete_count_condition(self, name):
        for row in list(self.rows):
            if row[0] == name:
                self.rows.remove(row)
                return True
        return False

    async def upsert_count_condition(self, name, expression):
        self.upserts.append((name, expression))


class CountRunner(LofterLLMToolsMixin):
    def __init__(self):
        self._db = FakeCountDB()
        self._source = object()


@pytest.mark.asyncio
async def test_llm_count_rejects_non_admin():
    runner = CountRunner()
    event = LLMToolEvent(is_admin=False)

    result = await runner.lofter_count(event, "list")

    assert result == ADMIN_ONLY_MESSAGE


@pytest.mark.asyncio
async def test_llm_count_lists_conditions_for_admin():
    runner = CountRunner()
    event = LLMToolEvent(is_admin=True)

    result = await runner.lofter_count(event, "list")

    assert "全局统计条件：" in result
    assert "1. 米哈游 = 原神" in result


@pytest.mark.asyncio
async def test_llm_count_deletes_by_index():
    runner = CountRunner()
    event = LLMToolEvent(is_admin=True)

    result = await runner.lofter_count(event, "delete", target="2")

    assert result == "已删除第 2 条统计条件「方舟」"
    assert runner._db.rows == [("米哈游", "原神")]


@pytest.mark.asyncio
async def test_llm_count_run_saves_and_formats_result(monkeypatch):
    from core.tag_count import CountResult
    import core.llm_tools as llm_tools

    async def fake_count_posts(expression, source):
        assert expression == "原神 -R18"
        assert source is runner._source
        return CountResult("", expression, 12, "成功", "", "", candidates=20, scanned_pages={"原神": 2})

    monkeypatch.setattr(llm_tools, "count_posts", fake_count_posts)
    runner = CountRunner()
    event = LLMToolEvent(is_admin=True)

    result = await runner.lofter_count(event, "run", name="米哈游安全", expression="原神 -R18")

    assert runner._db.upserts == [("米哈游安全", "原神 -R18")]
    assert "「米哈游安全」统计成功：12 个作品" in result
    assert "条件：原神 -R18" in result


def _main_import_stubs():
    api_mod = types.ModuleType("astrbot.api")
    api_mod.AstrBotConfig = MagicMock
    api_mod.logger = MagicMock()
    components_mod = types.ModuleType("astrbot.api.message_components")
    components_mod.Plain = components_mod.Image = MagicMock
    components_mod.Node = components_mod.Nodes = MagicMock
    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = event_mod.MessageChain = MagicMock
    event_mod.filter = MagicMock()
    event_mod.filter.EventMessageType.ALL = "all"
    event_mod.filter.PermissionType.ADMIN = "admin"
    event_mod.filter.event_message_type.side_effect = _identity_decorator
    event_mod.filter.permission_type.side_effect = _identity_decorator
    event_mod.filter.command_group.side_effect = _fake_command_group
    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = MagicMock
    star_mod.Star = object
    star_mod.register = _identity_decorator
    core_star_mod = types.ModuleType("astrbot.core.star")
    core_star_mod.StarTools = MagicMock
    return api_mod, components_mod, event_mod, star_mod, core_star_mod


def _identity_decorator(*args, **kwargs):
    return lambda value: value


def _fake_command_group(*args, **kwargs):
    def decorate(func):
        func.command = lambda *cmd_args, **cmd_kwargs: lambda value: value
        return func

    return decorate


def _load_test_main_module():
    package = types.ModuleType("lofter_plugin_test")
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules["lofter_plugin_test"] = package
    modules = _main_import_stubs()
    sys.modules.update(dict(zip((
        "astrbot.api", "astrbot.api.message_components", "astrbot.api.event",
        "astrbot.api.star", "astrbot.core.star",
    ), modules)))
    spec = importlib.util.spec_from_file_location(
        "lofter_plugin_test.main", Path(__file__).resolve().parents[1] / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["lofter_plugin_test.main"] = module
    sys.modules["main"] = module
    spec.loader.exec_module(module)
    return module, modules[3]


def test_plugin_inherits_llm_tools_mixin():
    stub_names = (
        "astrbot.api", "astrbot.api.message_components", "astrbot.api.event",
        "astrbot.api.star", "astrbot.core.star", "main",
        "lofter_plugin_test", "lofter_plugin_test.main",
    )
    original_modules = {name: sys.modules.get(name) for name in stub_names}
    try:
        for name in ("main", "lofter_plugin_test.main"):
            sys.modules.pop(name, None)
        module, star_mod = _load_test_main_module()
        plugin = module.LofterPlugin
        assert all(hasattr(plugin, name) for name in (
            "lofter_content", "lofter_subscription", "lofter_author_block", "lofter_count"
        ))
        mro = plugin.__mro__
        assert mro.index(module.LofterLLMToolsMixin) < mro.index(module.LofterCountCommandsMixin)
        assert mro.index(module.LofterLLMToolsMixin) < mro.index(star_mod.Star)
    finally:
        for name in list(sys.modules):
            if name.startswith("lofter_plugin_test.") and name not in original_modules:
                sys.modules.pop(name, None)
        for name, module in original_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
