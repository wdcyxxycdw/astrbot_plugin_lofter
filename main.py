import json
import os

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star, register
from astrbot.core.star import StarTools

from .commands import LofterCommands
from .core.client import LofterClient
from .core.db import LofterDB
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
        self._config_cookie: str = config.get("lofter_cookie", "")
        self._max_images: int = int(config.get("max_images", 3))
        self._interval: int = int(config.get("poll_interval", 30))
        db_path = os.path.join(StarTools.get_data_dir(), "lofter.db")
        self._db = LofterDB(db_path)
        self._client = LofterClient("")
        self._storage = SubscriptionStorage(self._db)
        self._scheduler = SubscriptionScheduler(
            self._storage, self._client, self._db, self._send_push, self._interval
        )

    async def initialize(self):
        await self._db.initialize()
        await self._migrate_json_once()
        cookie = await self._db.get_config("lofter_cookie") or self._config_cookie
        if cookie:
            await self._db.set_config("lofter_cookie", cookie)
        self._client.update_cookie(cookie)
        self._scheduler.start()

    async def terminate(self):
        await self._scheduler.stop()
        await self._db.close()

    async def _migrate_json_once(self):
        if await self._db.get_config("json_migrated"):
            return
        json_path = os.path.join(os.path.dirname(self._db._path), "subscriptions.json")
        if os.path.exists(json_path):
            try:
                with open(json_path, encoding="utf-8") as f:
                    data = json.load(f)
                for s in data.get("subscriptions", []):
                    await self._db.add_subscription(s["session_id"], s["type"], s["target"])
                logger.info("Lofter: 已从 subscriptions.json 迁移 %d 条订阅", len(data.get("subscriptions", [])))
            except Exception as e:
                logger.error("Lofter: JSON 迁移失败: %s", e)
        await self._db.set_config("json_migrated", "1")

    async def _send_push(self, session_id: str, text: str, images: list[str]):
        chain = [Comp.Plain(text)] + [Comp.Image.fromURL(u) for u in images]
        try:
            await self.context.send_message(session_id, chain)
        except Exception as e:
            logger.error("推送消息失败 session=%s: %s", session_id, e)
