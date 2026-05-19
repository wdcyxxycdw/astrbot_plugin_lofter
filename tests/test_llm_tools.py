import importlib
import sys
import types

import pytest

from core.author_block import AuthorBlock, normalize_author_query
from core.llm_tools import LofterLLMToolsMixin
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
        self.is_admin = is_admin

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


class LLMToolRunner(LofterLLMToolsMixin):
    def __init__(self):
        self._storage = FakeStorage()
        self._author_blocks = FakeAuthorBlocks()
        self._warm_tags = []
        self._warm_blogs = []

    async def _add_tag_entries(self, session_id, subscribes, excludes):
        added_subs = []
        added_excls = []
        for tag in subscribes:
            if await self._storage.add(session_id, "tag", tag, "subscribe"):
                added_subs.append(tag)
        for tag in excludes:
            if await self._storage.add(session_id, "tag", tag, "exclude"):
                added_excls.append(tag)
        return added_subs, added_excls

    async def _warmup_new_subscribes(self, session_id, new_tags):
        self._warm_tags.extend((session_id, tag) for tag in new_tags)

    async def _warmup_blog(self, session_id, username):
        self._warm_blogs.append((session_id, username))


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
    assert runner._warm_tags == [("sess", "原神")]


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


class FakeClient:
    async def search_tag(self, keyword, limit=20):
        assert keyword == "原神"
        assert limit == 2
        return "fake-dwr"


class FakeContentAuthorBlocks:
    async def list_by_session(self, session_id):
        from core.author_block import AuthorBlock

        return [AuthorBlock(session_id, "username", "blocked", "blocked")]


class ContentRunner(LofterLLMToolsMixin):
    def __init__(self):
        self._client = FakeClient()
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
async def test_llm_content_search_filters_blocked_authors(monkeypatch):
    async def fake_parse_dwr_response(raw):
        assert raw == "fake-dwr"
        return [
            Post("1", "可见标题", "摘要", ["img1", "img2", "img3"], "作者A", "visible", "https://a.lofter.com/post/1", ["原神"], "2026-05-15"),
            Post("2", "屏蔽标题", "摘要", [], "作者B", "blocked", "https://b.lofter.com/post/2", ["原神"], "2026-05-15"),
        ]

    import core.llm_tools as llm_tools

    monkeypatch.setattr(llm_tools, "parse_dwr_response", fake_parse_dwr_response)
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
        self._client = object()


@pytest.mark.asyncio
async def test_llm_count_rejects_non_admin():
    runner = CountRunner()
    event = LLMToolEvent(is_admin=False)

    result = await runner.lofter_count(event, "list")

    assert result == "只有管理员可以使用统计命令"


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

    async def fake_count_posts(expression, client):
        assert expression == "原神 -R18"
        return CountResult("", expression, 12, "成功", "", "", candidates=20, scanned_pages={"原神": 2})

    monkeypatch.setattr(llm_tools, "count_posts", fake_count_posts)
    runner = CountRunner()
    event = LLMToolEvent(is_admin=True)

    result = await runner.lofter_count(event, "run", name="米哈游安全", expression="原神 -R18")

    assert runner._db.upserts == [("米哈游安全", "原神 -R18")]
    assert "「米哈游安全」统计完成：12 个作品" in result
    assert "条件：原神 -R18" in result


def test_plugin_inherits_llm_tools_mixin():
    import importlib.util
    from pathlib import Path
    from unittest.mock import MagicMock

    stub_names = (
        "astrbot.api",
        "astrbot.api.message_components",
        "astrbot.api.event",
        "astrbot.api.star",
        "astrbot.core.star",
        "main",
        "lofter_plugin_test",
        "lofter_plugin_test.main",
    )
    original_modules = {name: sys.modules.get(name) for name in stub_names}

    try:
        package = types.ModuleType("lofter_plugin_test")
        package.__path__ = [str(Path(__file__).resolve().parents[1])]
        sys.modules["lofter_plugin_test"] = package

        for name in ("main", "lofter_plugin_test.main"):
            sys.modules.pop(name, None)

        api_mod = types.ModuleType("astrbot.api")
        api_mod.AstrBotConfig = MagicMock
        api_mod.logger = MagicMock()
        components_mod = types.ModuleType("astrbot.api.message_components")
        components_mod.Plain = MagicMock
        components_mod.Image = MagicMock
        components_mod.Node = MagicMock
        components_mod.Nodes = MagicMock
        event_mod = types.ModuleType("astrbot.api.event")
        event_mod.AstrMessageEvent = MagicMock
        event_mod.MessageChain = MagicMock
        event_mod.filter = MagicMock()
        event_mod.filter.EventMessageType.ALL = "all"
        event_mod.filter.event_message_type.side_effect = lambda *args, **kwargs: lambda func: func

        def fake_command_group(*args, **kwargs):
            def decorate(func):
                func.command = lambda *cmd_args, **cmd_kwargs: lambda command_func: command_func
                return func

            return decorate

        event_mod.filter.command_group.side_effect = fake_command_group
        star_mod = types.ModuleType("astrbot.api.star")
        star_mod.Context = MagicMock
        star_mod.Star = object
        star_mod.register = lambda *args, **kwargs: lambda cls: cls
        core_star_mod = types.ModuleType("astrbot.core.star")
        core_star_mod.StarTools = MagicMock

        sys.modules.update(
            {
                "astrbot.api": api_mod,
                "astrbot.api.message_components": components_mod,
                "astrbot.api.event": event_mod,
                "astrbot.api.star": star_mod,
                "astrbot.core.star": core_star_mod,
            }
        )

        spec = importlib.util.spec_from_file_location(
            "lofter_plugin_test.main",
            Path(__file__).resolve().parents[1] / "main.py",
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["lofter_plugin_test.main"] = module
        sys.modules["main"] = module
        spec.loader.exec_module(module)

        from main import LofterPlugin

        assert hasattr(LofterPlugin, "lofter_content")
        assert hasattr(LofterPlugin, "lofter_subscription")
        assert hasattr(LofterPlugin, "lofter_author_block")
        assert hasattr(LofterPlugin, "lofter_count")
        mro = LofterPlugin.__mro__
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
