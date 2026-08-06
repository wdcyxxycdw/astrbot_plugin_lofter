import ast
import importlib.util
import socket
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.permissions import ADMIN_ONLY_MESSAGE

ROOT = Path(__file__).resolve().parents[1]
ADMIN_HANDLERS = {
    "search",
    "set_cookie",
    "block_author",
    "unblock_author",
    "count",
    "count_del",
    "count_all",
    "sub_tag",
    "sub_tag_preview",
    "sub_blog",
    "unsub_tag",
    "unexclude_tag",
    "unsub_blog",
    "unsub_by_index",
    "run_e2e_test",
}
PUBLIC_HANDLERS = {"sub_list", "block_list", "count_list"}


def _load_main_module():
    module_name = "lofter_permission_test.main"
    if module_name in sys.modules:
        return sys.modules[module_name]
    package = types.ModuleType("lofter_permission_test")
    package.__path__ = [str(ROOT)]
    sys.modules["lofter_permission_test"] = package

    components = types.ModuleType("astrbot.api.message_components")
    components.Plain = components.Image = components.Node = MagicMock
    components.Nodes = components.Share = MagicMock
    event_module = types.ModuleType("astrbot.api.event")
    event_module.AstrMessageEvent = event_module.MessageChain = MagicMock
    event_module.filter = MagicMock()
    event_module.filter.EventMessageType.ALL = "all"
    event_module.filter.PermissionType.ADMIN = "admin"
    event_module.filter.event_message_type.side_effect = _identity_decorator
    event_module.filter.permission_type.side_effect = _identity_decorator
    event_module.filter.command_group.side_effect = _command_group_decorator
    star_module = types.ModuleType("astrbot.api.star")
    star_module.Context = MagicMock
    star_module.Star = _TestStar
    star_module.register = _identity_decorator
    core_star = types.ModuleType("astrbot.core.star")
    core_star.StarTools = MagicMock
    sys.modules.update({
        "astrbot.api.message_components": components,
        "astrbot.api.event": event_module,
        "astrbot.api.star": star_module,
        "astrbot.core.star": core_star,
    })
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sys.modules["main"] = module
    spec.loader.exec_module(module)
    return module


class _TestStar:
    def __init__(self, context):
        self.context = context



def _identity_decorator(*args, **kwargs):
    return lambda value: value


def _command_group_decorator(*args, **kwargs):
    def decorate(func):
        func.command = lambda *cmd_args, **cmd_kwargs: lambda value: value
        return func

    return decorate


def _plugin_class():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "LofterPlugin"
    )


def _command_methods():
    return {
        node.name: node
        for node in _plugin_class().body
        if isinstance(node, ast.AsyncFunctionDef)
        and any(".command(" in ast.unparse(item) for item in node.decorator_list)
    }


def test_only_fifteen_high_risk_commands_have_admin_decorator():
    methods = _command_methods()
    decorated = {
        name
        for name, method in methods.items()
        if any(ast.unparse(item).startswith("filter.permission_type") for item in method.decorator_list)
    }

    assert decorated == ADMIN_HANDLERS
    assert PUBLIC_HANDLERS.isdisjoint(decorated)


def test_admin_handlers_check_permission_before_plugin_business_logic():
    for name in ADMIN_HANDLERS:
        method = _command_methods()[name]
        statements = method.body[1:] if ast.get_docstring(method) else method.body
        first = statements[0]
        assert isinstance(first, ast.If), name
        assert ast.unparse(first.test) == "not is_admin_event(event)", name


def test_e2e_handler_uses_isolated_runner_and_discloses_live_effects():
    method = _command_methods()["run_e2e_test"]
    runner_call = next(
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "E2ETestRunner"
    )
    assert [ast.unparse(arg) for arg in runner_call.args] == [
        "self._source",
        "self._scheduler",
        "self._send_push_result",
    ]

    text = " ".join(
        node.value
        for node in ast.walk(method)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    assert "真实 LOFTER" in text
    assert "临时 SQLite" in text
    assert "发送一个" in text
    assert "最多产生文本预览与图片转发两条平台消息" in text
    assert "Lofter E2E 测试" in text


class DeniedEvent:
    message_str = object()
    unified_msg_origin = "should-not-be-read"

    def __init__(self):
        self.admin_calls = 0

    def is_admin(self):
        self.admin_calls += 1
        return False

    def plain_result(self, text):
        return text


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_name", sorted(ADMIN_HANDLERS))
async def test_admin_handler_body_rejects_after_registry_permission_downgrade(handler_name):
    main = _load_main_module()

    plugin = object.__new__(main.LofterPlugin)
    event = DeniedEvent()
    handler = getattr(main.LofterPlugin, handler_name)
    results = [item async for item in handler(plugin, event)]

    assert results == [ADMIN_ONLY_MESSAGE]
    assert event.admin_calls == 1


def test_config_validation_defaults_invalid_types_and_clamps_bounds():
    main = _load_main_module()

    config = {
        "poll_interval": True,
        "max_images": -4,
        "search_limit": 900,
    }

    assert main._validated_int_config(config, "poll_interval", 30, 1, 1440) == 30
    assert main._validated_int_config(config, "max_images", 3, 0, 20) == 0
    assert main._validated_int_config(config, "search_limit", 3, 1, 100) == 100


def test_config_warning_does_not_log_invalid_value(monkeypatch):
    main = _load_main_module()

    warning = SimpleNamespace(calls=[])
    monkeypatch.setattr(main.logger, "warning", lambda *args: warning.calls.append(args))
    secret_value = "sensitive-invalid-value"

    result = main._validated_int_config({"search_limit": secret_value}, "search_limit", 3, 1, 100)

    assert result == 3
    assert warning.calls
    assert secret_value not in repr(warning.calls)


def test_default_tests_block_real_network():
    with pytest.raises(RuntimeError, match="离线测试禁止真实网络访问"):
        socket.create_connection(("127.0.0.1", 9))


def test_pytest_config_registers_strict_markers(pytestconfig):
    assert pytestconfig.getini("strict_markers") is True
    assert any(item.startswith("real:") for item in pytestconfig.getini("markers"))


def test_all_changed_functions_stay_within_fifty_lines():
    paths = [ROOT / "main.py", ROOT / "core/llm_tools.py", ROOT / "core/permissions.py"]
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.end_lineno - node.lineno + 1 <= 50, (path.name, node.name)
