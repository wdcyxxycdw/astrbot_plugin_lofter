import json
import os
import re

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star import StarTools

from .core.client import LofterClient
from .core.db import LofterDB
from .core.dwr_parser import parse_dwr_response
from .core.parser import parse_post
from .core.scheduler import SubscriptionScheduler
from .core.storage import SubscriptionStorage

POST_PATTERN = re.compile(r"[a-zA-Z0-9_-]+\.lofter\.com/post/[a-zA-Z0-9_-]+")


@register(
    "astrbot_plugin_lofter",
    "user",
    "解析 Lofter 链接，订阅 Lofter 标签/博主，搜索 Lofter 内容",
    "v0.1.0",
)
class LofterPlugin(Star):
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

    # ──────────────────────────────────────────
    # 自动解析消息中的 Lofter 链接
    # ──────────────────────────────────────────

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def auto_parse(self, event: AstrMessageEvent):
        msg = str(event.message_obj) or event.message_str
        match = POST_PATTERN.search(msg)
        if not match:
            return
        url = "https://" + match.group(0)
        try:
            html = await self._client.get(url)
            post = await parse_post(html, url, self._max_images)
        except Exception as e:
            logger.error("解析 Lofter 链接失败: %s", e)
            yield event.plain_result(f"解析失败：{e}")
            return
        if not post:
            yield event.plain_result("未能解析该帖子内容")
            return
        chain = [Comp.Plain(post.text)] + [Comp.Image.fromURL(u) for u in post.images]
        yield event.chain_result(chain)

    # ──────────────────────────────────────────
    # /lofter 命令组
    # ──────────────────────────────────────────

    @filter.command_group("lofter")
    def lofter(self): ...

    @lofter.command("search")
    async def search(self, event: AstrMessageEvent):
        """搜索 Lofter 标签内容。用法：/lofter search <标签名>"""
        keyword = event.message_str.strip()
        if not keyword:
            yield event.plain_result("请提供标签名，例如：/lofter search 原创")
            return
        try:
            raw = await self._client.search_tag(keyword, limit=10)
            posts = parse_dwr_response(raw)
        except Exception as e:
            yield event.plain_result(f"搜索失败：{e}")
            return
        if not posts:
            yield event.plain_result("没有找到相关内容")
            return
        yield event.plain_result(f"「{keyword}」标签搜索结果，共 {len(posts)} 条：")
        for p in posts[:3]:
            title = p.title or "(无标题)"
            tags = f"#{' #'.join(p.tags)}" if p.tags else ""
            text_parts = [f"▸ {title}", f"作者：{p.author}  {p.publish_time}"]
            if tags:
                text_parts.append(tags)
            if p.summary:
                text_parts.append(p.summary)
            text_parts.append(p.url)
            chain = [Comp.Plain("\n".join(text_parts))]
            chain += [Comp.Image.fromURL(u) for u in p.images[:self._max_images]]
            yield event.chain_result(chain)

    @lofter.command("list")
    async def sub_list(self, event: AstrMessageEvent):
        """查看当前会话的订阅列表"""
        subs = await self._storage.list_by_session(event.unified_msg_origin)
        if not subs:
            yield event.plain_result("当前没有订阅")
            return
        lines = ["当前订阅列表："]
        for s in subs:
            label = "标签" if s.type == "tag" else "博主"
            lines.append(f"• [{label}] {s.target}")
        yield event.plain_result("\n".join(lines))

    @lofter.command("cookie")
    async def set_cookie(self, event: AstrMessageEvent):
        """更新 Lofter Cookie。用法：/lofter cookie <cookie值>"""
        value = event.message_str.strip()
        if not value:
            yield event.plain_result("请提供 Cookie 值，例如：/lofter cookie your_cookie_here")
            return
        await self._db.set_config("lofter_cookie", value)
        self._client.update_cookie(value)
        yield event.plain_result("Cookie 已更新")

    @lofter.command("subtag")
    async def sub_tag(self, event: AstrMessageEvent):
        """订阅标签。用法：/lofter subtag <标签名>"""
        tag = event.message_str.strip()
        if not tag:
            yield event.plain_result("请提供标签名，例如：/lofter subtag 原创")
            return
        ok = await self._storage.add(event.unified_msg_origin, "tag", tag)
        yield event.plain_result(f"已订阅标签「{tag}」" if ok else f"已经订阅过标签「{tag}」了")

    @lofter.command("subblog")
    async def sub_blog(self, event: AstrMessageEvent):
        """订阅博主。用法：/lofter subblog <用户名>"""
        username = event.message_str.strip()
        if not username:
            yield event.plain_result("请提供博主用户名，例如：/lofter subblog username")
            return
        ok = await self._storage.add(event.unified_msg_origin, "blog", username)
        yield event.plain_result(f"已订阅博主「{username}」" if ok else f"已经订阅过博主「{username}」了")

    @lofter.command("unsubtag")
    async def unsub_tag(self, event: AstrMessageEvent):
        """取消订阅标签。用法：/lofter unsubtag <标签名>"""
        tag = event.message_str.strip()
        if not tag:
            yield event.plain_result("请提供标签名")
            return
        ok = await self._storage.remove(event.unified_msg_origin, "tag", tag)
        yield event.plain_result(f"已取消订阅标签「{tag}」" if ok else f"未找到标签「{tag}」的订阅")

    @lofter.command("unsubblog")
    async def unsub_blog(self, event: AstrMessageEvent):
        """取消订阅博主。用法：/lofter unsubblog <用户名>"""
        username = event.message_str.strip()
        if not username:
            yield event.plain_result("请提供博主用户名")
            return
        ok = await self._storage.remove(event.unified_msg_origin, "blog", username)
        yield event.plain_result(f"已取消订阅博主「{username}」" if ok else f"未找到博主「{username}」的订阅")
