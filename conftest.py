import sys
import os
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(__file__))

_astrbot = MagicMock()
_astrbot.api.logger = MagicMock()
sys.modules.setdefault("astrbot", _astrbot)
sys.modules.setdefault("astrbot.api", _astrbot.api)
