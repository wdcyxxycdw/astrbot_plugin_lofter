import sys
import os
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(__file__))

# mock astrbot，使 core 模块可在测试环境中 import
_astrbot = MagicMock()
_astrbot.api.logger = MagicMock()
sys.modules.setdefault("astrbot", _astrbot)
sys.modules.setdefault("astrbot.api", _astrbot.api)
