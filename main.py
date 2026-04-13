import json
import os
import re

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star import StarTools

from .core.client import LofterClient
from .core.db import LofterDB
from .core.dwr_parser import parse_dwr_response
from .core.filter import apply_filter, filter_rule_from_json, filter_rule_to_json, has_filter, parse_filter_expr
from .core.parser import parse_post_page
from .core.scheduler import SubscriptionScheduler, fetch_posts
from .core.storage import Subscription, SubscriptionStorage

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
        self._search_limit: int = int(config.get("search_limit", 3))
        self._interval: int = int(config.get("poll_interval", 30))
        self._max_or_tags: int = int(config.get("max_or_tags", 3))
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
        await self._fix_corrupted_subscriptions()
        cookie = await self._db.get_config("lofter_cookie") or self._config_cookie
        if cookie:
            # 修复旧版本 bug：cookie 可能被误存为带 "lofter cookie " 前缀的形式
            if cookie.lower().startswith("lofter cookie "):
                cookie = cookie[len("lofter cookie "):]
            cookie = cookie.strip()
            await self._db.set_config("lofter_cookie", cookie)
        self._client.update_cookie(cookie)
        self._scheduler.start()

    async def terminate(self):
        await self._scheduler.stop()
        await self._db.close()

    async def _fix_corrupted_subscriptions(self):
        """修复旧版本 bug：订阅 target 可能带有命令前缀，如 'lofter subtag 原创' → '原创'。"""
        if await self._db.get_config("subscriptions_fixed"):
            return
        _PREFIXES = [
            "lofter subtagpreview ",
            "lofter subtag ",
            "lofter subblog ",
        ]
        rows = await self._db.list_subscriptions()
        for row in rows:
            sub_id, session_id, sub_type, target = row[:4]
            for prefix in _PREFIXES:
                if target.lower().startswith(prefix):
                    new_target = target[len(prefix):]
                    await self._db.fix_subscription_target(sub_id, new_target)
                    logger.info("Lofter: 修复订阅 target '%s' → '%s'", target, new_target)
                    break
        await self._db.set_config("subscriptions_fixed", "1")

    @staticmethod
    def _cmd_arg(message_str: str) -> str:
        """从 event.message_str 中提取子命令参数（去掉前两个词，即 'lofter <subcmd>'）。"""
        parts = message_str.strip().split(None, 2)
        return parts[2] if len(parts) > 2 else ""

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

    def _format_post_lines(self, post, label: str, target: str) -> list[str]:
        title = post.title or "(无标题)"
        lines = [f"【{label}「{target}」有新内容】", f"▸ {title}"]
        if post.author:
            lines.append(f"作者：{post.author}")
        tags = f"#{' #'.join(post.tags)}" if post.tags else ""
        if tags:
            lines.append(tags)
        if post.summary:
            lines.append(post.summary)
        lines.append(post.url)
        return lines

    async def _send_push(self, session_id: str, text: str, images: list[str]):
        mc = MessageChain()
        mc.message(text)
        for u in images[:self._max_images]:
            mc.url_image(u)
        try:
            await self.context.send_message(session_id, mc)
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
        except Exception as e:
            logger.error("获取 Lofter 帖子失败: %s", e)
            return

        post = await parse_post_page(html, url)
        if not post.summary and not post.images:
            return

        text_parts = [f"▸ {post.title or '(无标题)'}"]
        if post.author:
            text_parts.append(f"作者：{post.author}")
        tags = f"#{' #'.join(post.tags)}" if post.tags else ""
        if tags:
            text_parts.append(tags)
        if post.summary:
            text_parts.append(post.summary)
        text_parts.append(url)

        chain = [Comp.Plain("\n".join(text_parts))]
        chain += [Comp.Image.fromURL(u) for u in post.images[:self._max_images]]
        yield event.chain_result(chain)

    # ──────────────────────────────────────────
    # /lofter 命令组
    # ──────────────────────────────────────────

    @filter.command_group("lofter")
    def lofter(self): ...

    @lofter.command("search")
    async def search(self, event: AstrMessageEvent):
        """搜索 Lofter 标签内容。用法：/lofter search <标签名>"""
        keyword = self._cmd_arg(event.message_str)
        if not keyword:
            yield event.plain_result("请提供标签名，例如：/lofter search 原创")
            return
        try:
            limit = min(self._search_limit, 100)
            if limit <= 20:
                pages = [await self._client.search_tag(keyword, limit=limit)]
            else:
                pages = await self._client.search_tag_paged(keyword, total=limit)
            seen_ids: set[str] = set()
            posts = []
            for raw in pages:
                for p in await parse_dwr_response(raw):
                    if p.post_id not in seen_ids:
                        seen_ids.add(p.post_id)
                        posts.append(p)
        except Exception as e:
            yield event.plain_result(f"搜索失败：{e}")
            return
        if not posts:
            yield event.plain_result("没有找到相关内容")
            return
        yield event.plain_result(f"「{keyword}」标签搜索结果，共 {len(posts)} 条：")
        for p in posts[:self._search_limit]:
            title = p.title or "(无标题)"
            tags = f"#{' #'.join(p.tags)}" if p.tags else ""
            text_parts = [f"▸ {title}", f"作者：{p.author}  {p.publish_time}"]
            if tags:
                text_parts.append(tags)
            if p.summary:
                text_parts.append(p.summary)  # 已截断 300 字
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
            line = f"• [{label}] {s.target}"
            if s.filter_rule:
                rule = filter_rule_from_json(s.filter_rule)
                line += f"  过滤：{rule.raw}"
            lines.append(line)
        yield event.plain_result("\n".join(lines))

    @lofter.command("cookie")
    async def set_cookie(self, event: AstrMessageEvent):
        """更新 Lofter Cookie。用法：/lofter cookie <cookie值>"""
        value = self._cmd_arg(event.message_str)
        if not value:
            yield event.plain_result("请提供 Cookie 值，例如：/lofter cookie your_cookie_here")
            return
        await self._db.set_config("lofter_cookie", value)
        self._client.update_cookie(value)
        yield event.plain_result("Cookie 已更新")

    @lofter.command("subtag")
    async def sub_tag(self, event: AstrMessageEvent):
        """订阅标签。用法：/lofter subtag <标签名> [+包含 -排除 标签A|标签B]"""
        raw = self._cmd_arg(event.message_str)
        if not raw:
            yield event.plain_result("请提供标签名，例如：/lofter subtag 原创\n支持过滤：/lofter subtag 原神 +同人 -R18")
            return
        rule = parse_filter_expr(raw)
        if len(rule.search_tags) > self._max_or_tags:
            yield event.plain_result(f"OR 搜索标签最多 {self._max_or_tags} 个")
            return
        primary_tag = rule.search_tags[0] if rule.search_tags else raw
        filter_json = filter_rule_to_json(rule) if has_filter(rule) else None
        ok = await self._storage.add(event.unified_msg_origin, "tag", primary_tag, filter_json)
        if not ok:
            if filter_json:
                await self._storage.update_filter(event.unified_msg_origin, "tag", primary_tag, filter_json)
                yield event.plain_result(f"已更新标签「{primary_tag}」的过滤规则：{rule.raw}")
            else:
                yield event.plain_result(f"已经订阅过标签「{primary_tag}」了")
            return
        msg = f"已订阅标签「{primary_tag}」"
        if has_filter(rule):
            msg += f"\n过滤规则：{rule.raw}"
        yield event.plain_result(msg)

    @lofter.command("subtagpreview")
    async def sub_tag_preview(self, event: AstrMessageEvent):
        """订阅标签并立即预览最新内容。用法：/lofter subtagpreview <标签名> [+包含 -排除]"""
        raw = self._cmd_arg(event.message_str)
        if not raw:
            yield event.plain_result("请提供标签名，例如：/lofter subtagpreview 原创")
            return
        rule = parse_filter_expr(raw)
        if len(rule.search_tags) > self._max_or_tags:
            yield event.plain_result(f"OR 搜索标签最多 {self._max_or_tags} 个")
            return
        primary_tag = rule.search_tags[0] if rule.search_tags else raw
        filter_json = filter_rule_to_json(rule) if has_filter(rule) else None
        session_id = event.unified_msg_origin

        ok = await self._storage.add(session_id, "tag", primary_tag, filter_json)
        if not ok and filter_json:
            await self._storage.update_filter(session_id, "tag", primary_tag, filter_json)

        sub = Subscription(session_id=session_id, type="tag", target=primary_tag, filter_rule=filter_json or "")
        try:
            posts = await fetch_posts(sub, self._client)
        except Exception as e:
            yield event.plain_result(f"已订阅标签「{primary_tag}」，但获取内容失败：{e}")
            return

        if has_filter(rule):
            posts = apply_filter(posts, rule)

        if not posts:
            yield event.plain_result(f"已订阅标签「{primary_tag}」，暂无匹配内容")
            return

        sub_id = await self._db.get_subscription_id(session_id, "tag", primary_tag)
        if sub_id is not None:
            await self._db.mark_seen(sub_id, [p.post_id for p in posts])

        msg = f"已订阅标签「{primary_tag}」"
        if has_filter(rule):
            msg += f"（过滤：{rule.raw}）"
        msg += f"，以下是最新 {min(3, len(posts))} 条内容："
        yield event.plain_result(msg)
        for post in posts[:3]:
            lines = self._format_post_lines(post, "标签", primary_tag)
            chain = [Comp.Plain("\n".join(lines))]
            chain += [Comp.Image.fromURL(u) for u in post.images[:self._max_images]]
            yield event.chain_result(chain)

    @lofter.command("subblog")
    async def sub_blog(self, event: AstrMessageEvent):
        """订阅博主。用法：/lofter subblog <用户名>"""
        username = self._cmd_arg(event.message_str)
        if not username:
            yield event.plain_result("请提供博主用户名，例如：/lofter subblog username")
            return
        ok = await self._storage.add(event.unified_msg_origin, "blog", username)
        yield event.plain_result(f"已订阅博主「{username}」" if ok else f"已经订阅过博主「{username}」了")

    @lofter.command("unsubtag")
    async def unsub_tag(self, event: AstrMessageEvent):
        """取消订阅标签。用法：/lofter unsubtag <标签名>"""
        tag = self._cmd_arg(event.message_str)
        if not tag:
            yield event.plain_result("请提供标签名")
            return
        ok = await self._storage.remove(event.unified_msg_origin, "tag", tag)
        yield event.plain_result(f"已取消订阅标签「{tag}」" if ok else f"未找到标签「{tag}」的订阅")

    @lofter.command("unsubblog")
    async def unsub_blog(self, event: AstrMessageEvent):
        """取消订阅博主。用法：/lofter unsubblog <用户名>"""
        username = self._cmd_arg(event.message_str)
        if not username:
            yield event.plain_result("请提供博主用户名")
            return
        ok = await self._storage.remove(event.unified_msg_origin, "blog", username)
        yield event.plain_result(f"已取消订阅博主「{username}」" if ok else f"未找到博主「{username}」的订阅")
