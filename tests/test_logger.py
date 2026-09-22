import importlib
import sys
import types

import pytest


@pytest.mark.parametrize("module_name", ["core.count_commands", "core.llm_tools", "core.reaction"])
def test_modules_use_astrbot_logger(module_name):
    module = importlib.import_module(module_name)

    assert module.logger is sys.modules["astrbot.api"].logger


@pytest.mark.parametrize("module_name", ["core.count_commands", "core.llm_tools", "core.reaction"])
@pytest.mark.parametrize("error_type", [ModuleNotFoundError, RuntimeError])
def test_logger_import_errors_are_not_swallowed(module_name, error_type, monkeypatch):
    module = importlib.import_module(module_name)
    api = types.ModuleType("astrbot.api")

    def fail_logger_import(name):
        if name == "logger":
            raise error_type("AstrBot logger unavailable")
        raise AttributeError(name)

    api.__getattr__ = fail_logger_import
    try:
        with monkeypatch.context() as patch:
            patch.setitem(sys.modules, "astrbot.api", api)
            with pytest.raises(error_type, match="AstrBot logger unavailable"):
                importlib.reload(module)
    finally:
        importlib.reload(module)
