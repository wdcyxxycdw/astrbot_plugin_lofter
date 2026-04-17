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
from .core.filter import parse_tag_expr
from .core.parser import parse_post_page
from .core.utils import _split_text
from .core.scheduler import SubscriptionScheduler, fetch_tag_posts
from .core.storage import SubscriptionStorage

POST_PATTERN = re.compile(r"[a-zA-Z0-9_-]+\.lofter\.com/post/[a-zA-Z0-9_-]+")


@register(
    "astrbot_plugin_lofter",
    "user",
    "解析 Lofter 链接，订阅 Lofter 标签/博主，搜索 Lofter 内容",
    "v0.2.0",
)
class LofterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self._config_cookie: str = config.get("lofter_cookie", "")
        self._max_images: int = int(config.get("max_images", 3))
        self._search_limit: int = int(config.get("search_limit", 3))
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
            if cookie.lower().startswith("lofter cookie "):
                cookie = cookie[len("lofter cookie "):]
            cookie = cookie.strip()
            await self._db.set_config("lofter_cookie", cookie)
        self._client.update_cookie(cookie)
        self._scheduler.start()

    async def terminate(self):
        await self._scheduler.stop()
        await self._db.close()

    @staticmethod
    def _cmd_arg(message_str: str) -> str:
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

    async def _warmup_tag(self, session_id: str, target: str):
        try:
            posts = await fetch_tag_posts([target], self._client)
            if posts:
                await self._db.mark_seen_session(session_id, "tag", [p.post_id for p in posts])
        except Exception as e:
            logger.warning("Lofter: warmup 标签「%s」失败: %s", target, e)

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
        if not post.summary and not post.images and not post.content:
            return

        text_parts = [f"▸ {post.title or '(无标题)'}"]
        if post.author:
            text_parts.append(f"作者：{post.author}")
        tags = f"#{' #'.join(post.tags)}" if post.tags else ""
        if tags:
            text_parts.append(tags)
        header = "\n".join(text_parts)

        if post.images:
            if post.summary:
                text_parts.append(post.summary)
            text_parts.append(url)
            chain = [Comp.Plain("\n".join(text_parts))]
            chain += [Comp.Image.fromURL(u) for u in post.images[:self._max_images]]
            yield event.chain_result(chain)
        elif post.content:
            is_private = "FriendMessage" in event.unified_msg_origin
            if is_private:
                preview = post.content[:500] + ("…\n（全文请点击链接）" if len(post.content) > 500 else "")
                text_parts.append(preview)
                text_parts.append(url)
                yield event.chain_result([Comp.Plain("\n".join(text_parts))])
            else:
                author_name = post.author or "Lofter"
                chunks = _split_text(post.content)
                nodes = [Comp.Node(content=[Comp.Plain(header)], name=author_name, uin="0")]
                for chunk in chunks:
                    nodes.append(Comp.Node(content=[Comp.Plain(chunk)], name=author_name, uin="0"))
                nodes.append(Comp.Node(content=[Comp.Plain(url)], name=author_name, uin="0"))
                yield event.chain_result([Comp.Nodes(nodes=nodes)])
        else:
            text_parts.append(post.summary)
            text_parts.append(url)
            yield event.chain_result([Comp.Plain("\n".join(text_parts))])

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
        for i, s in enumerate(subs, 1):
            if s.type == "tag":
                role_label = "订阅" if s.role == "subscribe" else "排除"
                lines.append(f"{i}. [标签｜{role_label}] {s.target}")
            else:
                lines.append(f"{i}. [博主]       {s.target}")
        lines.append("\n用 /lofter unsub <编号> 取消订阅")
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
        """订阅标签。用法：/lofter subtag <标签名> [-排除标签]"""
        raw = self._cmd_arg(event.message_str)
        if not raw:
            yield event.plain_result("请提供标签名，例如：/lofter subtag 原创\n支持排除：/lofter subtag 原神 -R18")
            return
        subscribes, excludes = parse_tag_expr(raw)
        session_id = event.unified_msg_origin

        added_subs, added_excls = await self._add_tag_entries(session_id, subscribes, excludes)
        await self._warmup_new_subscribes(session_id, added_subs)

        if not added_subs and not added_excls:
            yield event.plain_result("订阅已存在，无需重复添加")
            return

        parts = []
        if added_subs:
            parts.append(f"新增订阅：{', '.join(added_subs)}")
        if added_excls:
            parts.append(f"新增排除：{', '.join(added_excls)}")
        yield event.plain_result("\n".join(parts))

    @lofter.command("subtagpreview")
    async def sub_tag_preview(self, event: AstrMessageEvent):
        """订阅标签并立即预览最新内容。用法：/lofter subtagpreview <标签名> [-排除]"""
        raw = self._cmd_arg(event.message_str)
        if not raw:
            yield event.plain_result("请提供标签名，例如：/lofter subtagpreview 原创")
            return
        subscribes, excludes = parse_tag_expr(raw)
        session_id = event.unified_msg_origin

        added_subs, _ = await self._add_tag_entries(session_id, subscribes, excludes)

        if not subscribes:
            yield event.plain_result("请至少提供一个要订阅的标签（排除规则不触发预览）")
            return

        from .core.filter import FilterRule, apply_filter
        rule = FilterRule(search_tags=subscribes, exclude_tags=excludes)
        try:
            from .core.scheduler import fetch_tag_posts
            posts = await fetch_tag_posts(subscribes, self._client)
        except Exception as e:
            yield event.plain_result(f"已处理订阅，但获取内容失败：{e}")
            return

        posts = apply_filter(posts, rule)
        await self._db.mark_seen_session(session_id, "tag", [p.post_id for p in posts])

        if not posts:
            yield event.plain_result(f"已订阅标签「{subscribes[0]}」，暂无匹配内容")
            return

        msg = f"已订阅标签「{subscribes[0]}」，以下是最新 {min(3, len(posts))} 条内容："
        yield event.plain_result(msg)
        for post in posts[:3]:
            lines = self._format_post_lines(post, "标签", subscribes[0])
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
        ok = await self._storage.remove(event.unified_msg_origin, "tag", tag, "subscribe")
        yield event.plain_result(f"已取消订阅标签「{tag}」" if ok else f"未找到标签「{tag}」的订阅")

    @lofter.command("unexcludetag")
    async def unexclude_tag(self, event: AstrMessageEvent):
        """取消排除标签。用法：/lofter unexcludetag <标签名>"""
        tag = self._cmd_arg(event.message_str)
        if not tag:
            yield event.plain_result("请提供标签名")
            return
        ok = await self._storage.remove(event.unified_msg_origin, "tag", tag, "exclude")
        yield event.plain_result(f"已取消排除标签「{tag}」" if ok else f"未找到标签「{tag}」的排除规则")

    @lofter.command("unsubblog")
    async def unsub_blog(self, event: AstrMessageEvent):
        """取消订阅博主。用法：/lofter unsubblog <用户名>"""
        username = self._cmd_arg(event.message_str)
        if not username:
            yield event.plain_result("请提供博主用户名")
            return
        ok = await self._storage.remove(event.unified_msg_origin, "blog", username)
        yield event.plain_result(f"已取消订阅博主「{username}」" if ok else f"未找到博主「{username}」的订阅")

    @lofter.command("unsub")
    async def unsub_by_index(self, event: AstrMessageEvent):
        """按编号取消订阅。用法：/lofter unsub <编号>（编号来自 /lofter list）"""
        arg = self._cmd_arg(event.message_str)
        if not arg or not arg.isdigit():
            yield event.plain_result("请提供有效编号，例如：/lofter unsub 2\n（先用 /lofter list 查看编号）")
            return
        idx = int(arg)
        subs = await self._storage.list_by_session(event.unified_msg_origin)
        if idx < 1 or idx > len(subs):
            yield event.plain_result(f"编号 {idx} 超出范围（当前共 {len(subs)} 条订阅）")
            return
        target_sub = subs[idx - 1]
        ok = await self._storage.remove_by_id(target_sub.id)
        if ok:
            role_label = "排除" if target_sub.role == "exclude" else "订阅"
            type_label = "标签" if target_sub.type == "tag" else "博主"
            yield event.plain_result(f"已删除第 {idx} 条：[{type_label}｜{role_label}] {target_sub.target}")
        else:
            yield event.plain_result(f"删除失败，请重新 list 确认编号")

    async def _add_tag_entries(self, session_id: str, subscribes: list[str], excludes: list[str]) -> tuple[list[str], list[str]]:
        added_subs: list[str] = []
        added_excls: list[str] = []
        for tag in subscribes:
            ok = await self._storage.add(session_id, "tag", tag, "subscribe")
            if ok:
                added_subs.append(tag)
        for tag in excludes:
            ok = await self._storage.add(session_id, "tag", tag, "exclude")
            if ok:
                added_excls.append(tag)
        return added_subs, added_excls

    async def _warmup_new_subscribes(self, session_id: str, new_tags: list[str]):
        for tag in new_tags:
            await self._warmup_tag(session_id, tag)
