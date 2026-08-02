import importlib.util
import sys
import types
from pathlib import Path

import astrbot
import pytest

from core.permissions import ADMIN_ONLY_MESSAGE

ROOT = Path(__file__).resolve().parents[1]
REAL_ASTRBOT = isinstance(astrbot, types.ModuleType) and bool(getattr(astrbot, "__file__", None))
pytestmark = pytest.mark.skipif(not REAL_ASTRBOT, reason="需要真实 AstrBot 包")

ADMIN_COMMANDS = {
    "search",
    "cookie",
    "block-author",
    "unblock-author",
    "count",
    "count-del",
    "count-all",
    "subtag",
    "subtagpreview",
    "subblog",
    "unsubtag",
    "unexcludetag",
    "unsubblog",
    "unsub",
    "test",
}
PUBLIC_COMMANDS = {"list", "block-list", "count-list"}


def _command_name(handler, command_filter_cls):
    command_filter = next(item for item in handler.event_filters if isinstance(item, command_filter_cls))
    return command_filter.command_name


def _load_plugin(module_name):
    package_name = module_name.rsplit(".", 1)[0]
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sys.modules["main"] = module
    spec.loader.exec_module(module)
    return module


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
async def test_real_registry_has_expected_command_permission_filters():
    from astrbot.core.provider.register import llm_tools
    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import star_handlers_registry

    module_name = "lofter_registry_test.main"
    handlers_before = list(star_handlers_registry._handlers)
    map_before = dict(star_handlers_registry.star_handlers_map)
    stars_before = dict(star_map)
    tools_before = list(llm_tools.func_list)
    try:
        _load_plugin(module_name)
        handlers = [
            item
            for item in star_handlers_registry._handlers
            if item.handler_module_path == module_name
            and any(isinstance(f, CommandFilter) for f in item.event_filters)
        ]
        by_command = {_command_name(item, CommandFilter): item for item in handlers}
        assert set(by_command) == ADMIN_COMMANDS | PUBLIC_COMMANDS
        for command in ADMIN_COMMANDS:
            permissions = [f for f in by_command[command].event_filters if isinstance(f, PermissionTypeFilter)]
            assert len(permissions) == 1
            assert permissions[0].permission_type == PermissionType.ADMIN
        for command in PUBLIC_COMMANDS:
            assert not any(isinstance(f, PermissionTypeFilter) for f in by_command[command].event_filters)
        downgraded = by_command["cookie"]
        permission = next(f for f in downgraded.event_filters if isinstance(f, PermissionTypeFilter))
        permission.permission_type = PermissionType.MEMBER
        event = DeniedEvent()
        plugin = object.__new__(sys.modules[module_name].LofterPlugin)
        results = [item async for item in downgraded.handler(plugin, event)]
        assert results == [ADMIN_ONLY_MESSAGE]
        assert event.admin_calls == 1
    finally:
        star_handlers_registry._handlers[:] = handlers_before
        star_handlers_registry.star_handlers_map.clear()
        star_handlers_registry.star_handlers_map.update(map_before)
        star_map.clear()
        star_map.update(stars_before)
        llm_tools.func_list[:] = tools_before
        for name in list(sys.modules):
            if name == "main" or name.startswith("lofter_registry_test"):
                sys.modules.pop(name, None)
