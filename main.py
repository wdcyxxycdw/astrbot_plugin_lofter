import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star, register

from .commands import LofterCommands
from .core.client import LofterClient
from .core.scheduler import SubscriptionScheduler
from .core.storage import SubscriptionStorage


@register(
    "astrbot_plugin_lofter",
    "user",
    "解析 Lofter 链接，订阅 Lofter 标签/博主，搜索 Lofter 内容",
    "v0.1.0",
)
class LofterPlugin(LofterCommands, Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self._cookie: str = config.get("lofter_cookie", "")
        self._max_images: int = int(config.get("max_images", 3))
        self._interval: int = int(config.get("poll_interval", 30))
        self._client = LofterClient(self._cookie)
        self._storage = SubscriptionStorage(context.data_dir)
        self._scheduler = SubscriptionScheduler(
            self._storage, self._client, self._send_push, self._interval
        )

    async def initialize(self):
        self._scheduler.start()

    async def terminate(self):
        await self._scheduler.stop()

    async def _send_push(self, session_id: str, text: str, images: list[str]):
        chain = [Comp.Plain(text)] + [Comp.Image.fromURL(u) for u in images]
        try:
            await self.context.send_message(session_id, chain)
        except Exception as e:
            logger.error("推送消息失败 session=%s: %s", session_id, e)
