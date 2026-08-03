import os
import socket
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(__file__))

try:
    import astrbot.api  # noqa: F401
except ModuleNotFoundError:
    _astrbot = MagicMock()
    _astrbot.api.logger = MagicMock()
    sys.modules.setdefault("astrbot", _astrbot)
    sys.modules.setdefault("astrbot.api", _astrbot.api)


@pytest.fixture(autouse=True)
def block_live_network_by_default(request, monkeypatch):
    is_live = request.node.get_closest_marker("real") and os.getenv("LOFTER_RUN_LIVE") == "1"
    if is_live:
        return

    def blocked(*args, **kwargs):
        raise RuntimeError("离线测试禁止真实网络访问")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
