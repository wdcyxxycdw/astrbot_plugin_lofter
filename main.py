import asyncio
import os
import re

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star import StarTools

from .core.author_block import AuthorBlockStorage
from .core.content_source import DefaultContentSource, ContentSource, collect_pages
from .core.count_commands import LofterCountCommandsMixin
from .core.db import LofterDB
from .core.db_json_migration import migrate_json_v2
from .core.llm_tools import LofterLLMToolsMixin
from .core.filter import parse_tag_expr
from .core.formatter import format_post, visible_images
from .core.instance_lock import InstanceLock
from .core.permissions import ADMIN_ONLY_MESSAGE, is_admin_event
from .core.parser import Post
from .core.post_consumers import filter_blocked_with_fields
from .core.scheduler import SubscriptionScheduler
from .core.session_gate import SessionGateRegistry
from .core.storage import SubscriptionStorage
from .core.subscription_service import SubscriptionService
from .core.utils import _split_text, extract_message_body_text

POST_PATTERN = re.compile(r"[a-zA-Z0-9_-]+\.lofter\.com/post/[a-zA-Z0-9_-]+")


def _validated_int_config(
    config: AstrBotConfig,
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = config.get(key, default)
    if type(value) is not int:
        logger.warning("Lofter: 配置 %s 类型无效，已使用默认值", key)
        return default
    clamped = max(minimum, min(value, maximum))
    if clamped != value:
        logger.warning("Lofter: 配置 %s 超出范围，已限制到有效边界", key)
    return clamped


def _qq_share(post: Post, header: str = ""):
    title = "Lofter"
    if post.has_fields({"title"}):
        title = post.title or "(无标题)"
    content = [header] if header else []
    if post.has_fields({"author"}) and post.author:
        content.append(f"作者：{post.author}")
    if post.has_fields({"tags"}) and post.tags:
        content.append(f"#{' #'.join(post.tags)}")
    if post.has_fields({"summary"}) and post.summary:
        content.append(post.summary)
    images = visible_images(post)
    content_text = "\n".join(content)
    if post.has_fields({"url"}) and post.url:
        content_text = content_text.replace(post.url, "").strip()
    return Comp.Share(
        url=post.url if post.has_fields({"url"}) else "",
        title=title,
        content=content_text,
        image=images[0] if images else "",
    )


def _qq_image_nodes(post: Post):
    name = post.author if post.has_fields({"author"}) and post.author else "Lofter"
    return Comp.Nodes(nodes=[
        Comp.Node(content=[Comp.Image.fromURL(url)], name=name, uin="0")
        for url in visible_images(post)
    ])


def _post_components(
    post: Post, header: str, source_types: frozenset[str],
    is_qq: bool, max_images: int,
):
    if not is_qq:
        chain = [Comp.Plain(format_post(post, header=header))]
        chain.extend(
            Comp.Image.fromURL(url)
            for url in visible_images(post)[:max_images]
        )
        return chain
    chain = [_qq_share(post, header)]
    if "tag" in source_types and visible_images(post):
        chain.append(_qq_image_nodes(post))
    return chain


def _auto_post_result(event: AstrMessageEvent, post: Post, max_images: int):
    if event.get_platform_name() != "aiocqhttp":
        content = post.content if post.has_fields({"content"}) else ""
        images = visible_images(post)[:max_images]
        if content and not images:
            suffix = "…\n（全文请点击链接）" if len(content) > 500 else ""
            text = format_post(post, body=content[:500] + suffix)
        else:
            text = format_post(post)
        chain = [Comp.Plain(text)]
        chain.extend(Comp.Image.fromURL(url) for url in images)
        return event.chain_result(chain)
    chain = [_qq_share(post)]
    content = post.content if post.has_fields({"content"}) else ""
    if content and len(content) > 500 and event.get_group_id() and not event.is_private_chat():
        name = post.author if post.has_fields({"author"}) and post.author else "Lofter"
        nodes = [
            Comp.Node(
                content=[Comp.Plain(chunk)], name=name, uin=event.get_self_id()
            )
            for chunk in _split_text(content)
        ]
        chain.append(Comp.Nodes(nodes=nodes))
    return event.chain_result(chain)


async def _search_unique_posts(source: ContentSource, keyword: str, limit: int):
    page = await collect_pages(
        lambda cursor: source.list_tag(keyword, cursor, min(limit, 100), "new"),
        limit=limit,
    )
    return page.items


@register(
    "astrbot_plugin_lofter",
    "user",
    "解析 Lofter 链接，订阅 Lofter 标签/博主，搜索 Lofter 内容，支持标签表达式统计",
    "v2.0.8",
)
class LofterPlugin(LofterLLMToolsMixin, LofterCountCommandsMixin, Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self._config_cookie: str = config.get("lofter_cookie", "")
        self._max_images = _validated_int_config(config, "max_images", 3, 0, 20)
        self._search_limit = _validated_int_config(config, "search_limit", 3, 1, 100)
        self._interval = _validated_int_config(config, "poll_interval", 30, 1, 1440)
        db_path = os.path.join(StarTools.get_data_dir(), "lofter.db")
        self._instance_lock = InstanceLock(db_path)
        self._db = LofterDB(db_path)
        self._source = DefaultContentSource()
        self._storage = SubscriptionStorage(self._db)
        self._session_gates = SessionGateRegistry()
        self._subscriptions = SubscriptionService(
            self._db, self._source, self._session_gates
        )
        self._author_blocks = AuthorBlockStorage(
            self._db, self._session_gates
        )
        self._scheduler = SubscriptionScheduler(
            self._storage,
            self._source,
            self._db,
            self._send_push,
            block_storage=self._author_blocks,
            interval_minutes=self._interval,
            gates=self._session_gates,
            subscription_service=self._subscriptions,
        )

    async def initialize(self):
        self._instance_lock.acquire()
        try:
            await self._initialize_locked()
        except BaseException:
            await asyncio.shield(self._close_failed_initialize())
            raise

    async def _initialize_locked(self):
        await self._db.initialize()
        await self._migrate_json_once()
        cookie = await self._db.get_config("lofter_cookie") or self._config_cookie
        if cookie:
            if cookie.lower().startswith("lofter cookie "):
                cookie = cookie[len("lofter cookie "):]
            cookie = cookie.strip()
            await self._db.set_config("lofter_cookie", cookie)
        self._source.update_cookie(cookie)
        await self._source.initialize()
        self._scheduler.start()

    async def _close_resources(self):
        try:
            await self._scheduler.stop()
        finally:
            try:
                await self._source.close()
            finally:
                try:
                    await self._db.close()
                finally:
                    self._instance_lock.release()

    async def _close_failed_initialize(self):
        await self._close_resources()

    async def terminate(self):
        await self._close_resources()

    @staticmethod
    def _cmd_arg(message_str: str) -> str:
        parts = message_str.strip().split(None, 2)
        return parts[2] if len(parts) > 2 else ""

    async def _migrate_json_once(self):
        json_path = os.path.join(os.path.dirname(self._db._path), "subscriptions.json")
        result = await migrate_json_v2(self._db, json_path)
        if result.source_found and not result.already_migrated:
            logger.info(
                "Lofter: 已从 subscriptions.json 迁移 %d/%d 条订阅",
                result.inserted,
                result.total,
            )


    async def _send_push(
        self,
        session_id: str,
        post: Post,
        header: str,
        source_types: frozenset[str],
    ) -> bool:
        platform_id = session_id.split(":", 1)[0]
        platform = self.context.get_platform_inst(platform_id)
        is_qq = platform is not None and platform.meta().name == "aiocqhttp"
        components = _post_components(
            post, header, source_types, is_qq, self._max_images
        )
        try:
            result = await self.context.send_message(
                session_id, MessageChain(components)
            )
            return result is not False
        except Exception as e:
            logger.error("推送消息失败 session=%s: %s", session_id, e)
            return False

    # ──────────────────────────────────────────
    # 自动解析消息中的 Lofter 链接
    # ──────────────────────────────────────────

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def auto_parse(self, event: AstrMessageEvent):
        msg = extract_message_body_text(event.message_obj, event.message_str)
        match = POST_PATTERN.search(msg)
        if not match:
            return
        url = "https://" + match.group(0)
        try:
            post = await self._source.get_post(url)
        except Exception as e:
            logger.error("获取 Lofter 帖子失败: %s", e)
            return
        blocks = await self._author_blocks.list_by_session(event.unified_msg_origin)
        try:
            visible, _ = await filter_blocked_with_fields(
                [post], blocks, self._source
            )
        except Exception as e:
            logger.error("补全 Lofter 作者字段失败: %s", e)
            return
        if not visible:
            return
        post = visible[0]
        yield _auto_post_result(event, post, self._max_images)

    # ──────────────────────────────────────────
    # /lofter 命令组
    # ──────────────────────────────────────────

    @filter.command_group("lofter")
    def lofter(self): ...

    @filter.permission_type(filter.PermissionType.ADMIN)
    @lofter.command("search")
    async def search(self, event: AstrMessageEvent):
        """搜索 Lofter 标签内容。用法：/lofter search <标签名>"""
        if not is_admin_event(event):
            yield event.plain_result(ADMIN_ONLY_MESSAGE)
            return
        keyword = self._cmd_arg(event.message_str)
        if not keyword:
            yield event.plain_result("请提供标签名，例如：/lofter search 原创")
            return
        try:
            posts = await _search_unique_posts(
                self._source, keyword, min(self._search_limit, 100)
            )
        except Exception as e:
            yield event.plain_result(f"搜索失败：{e}")
            return
        blocks = await self._author_blocks.list_by_session(event.unified_msg_origin)
        try:
            posts, _ = await filter_blocked_with_fields(
                posts, blocks, self._source
            )
        except Exception as e:
            yield event.plain_result(f"搜索结果字段不完整：{e}")
            return
        if not posts:
            yield event.plain_result("没有找到未屏蔽作者的相关内容")
            return
        yield event.plain_result(f"「{keyword}」标签搜索结果，共 {len(posts)} 条：")
        for p in posts[:self._search_limit]:
            chain = [Comp.Plain(format_post(p, include_time=True))]
            chain += [
                Comp.Image.fromURL(u)
                for u in visible_images(p)[:self._max_images]
            ]
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

    @filter.permission_type(filter.PermissionType.ADMIN)
    @lofter.command("cookie")
    async def set_cookie(self, event: AstrMessageEvent):
        """更新 Lofter Cookie。用法：/lofter cookie <cookie值>"""
        if not is_admin_event(event):
            yield event.plain_result(ADMIN_ONLY_MESSAGE)
            return
        value = self._cmd_arg(event.message_str)
        if not value:
            yield event.plain_result("请提供 Cookie 值，例如：/lofter cookie your_cookie_here")
            return
        await self._db.set_config("lofter_cookie", value)
        self._source.update_cookie(value)
        yield event.plain_result("Cookie 已更新")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @lofter.command("block-author")
    async def block_author(self, event: AstrMessageEvent):
        """屏蔽作者。用法：/lofter block-author <昵称或用户名>"""
        if not is_admin_event(event):
            yield event.plain_result(ADMIN_ONLY_MESSAGE)
            return
        raw = self._cmd_arg(event.message_str).strip()
        if not raw:
            yield event.plain_result("请提供作者昵称或用户名，例如：/lofter block-author username")
            return
        ok = await self._author_blocks.add(event.unified_msg_origin, raw)
        yield event.plain_result(f"已屏蔽作者「{raw}」" if ok else f"作者「{raw}」已在屏蔽列表中")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @lofter.command("unblock-author")
    async def unblock_author(self, event: AstrMessageEvent):
        """解除作者屏蔽。用法：/lofter unblock-author <昵称或用户名>"""
        if not is_admin_event(event):
            yield event.plain_result(ADMIN_ONLY_MESSAGE)
            return
        raw = self._cmd_arg(event.message_str).strip()
        if not raw:
            yield event.plain_result("请提供作者昵称或用户名")
            return
        ok = await self._author_blocks.remove(event.unified_msg_origin, raw)
        yield event.plain_result(f"已解除屏蔽作者「{raw}」" if ok else f"未找到作者「{raw}」的屏蔽记录")

    @lofter.command("block-list")
    async def block_list(self, event: AstrMessageEvent):
        """查看当前会话屏蔽作者列表"""
        blocks = await self._author_blocks.list_by_session(event.unified_msg_origin)
        if not blocks:
            yield event.plain_result("当前没有屏蔽作者")
            return
        lines = ["当前屏蔽作者列表："]
        for i, block in enumerate(blocks, 1):
            label = "用户名" if block.kind == "username" else "昵称"
            lines.append(f"{i}. [{label}] {block.display}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @lofter.command("count")
    async def count(self, event: AstrMessageEvent):
        """保存并执行标签表达式统计。用法：/lofter count <名称> = <表达式>"""
        if not is_admin_event(event):
            yield event.plain_result(ADMIN_ONLY_MESSAGE)
            return
        async for result in self.handle_count(event): yield result

    @lofter.command("count-list")
    async def count_list(self, event: AstrMessageEvent):
        """查看已保存的全局统计条件。用法：/lofter count-list"""
        async for result in self.handle_count_list(event): yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @lofter.command("count-del")
    async def count_del(self, event: AstrMessageEvent):
        """按名称或编号删除统计条件。用法：/lofter count-del <名称或编号>"""
        if not is_admin_event(event):
            yield event.plain_result(ADMIN_ONLY_MESSAGE)
            return
        async for result in self.handle_count_del(event): yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @lofter.command("count-all")
    async def count_all(self, event: AstrMessageEvent):
        """执行所有已保存统计条件并生成 CSV。用法：/lofter count-all"""
        if not is_admin_event(event):
            yield event.plain_result(ADMIN_ONLY_MESSAGE)
            return
        async for result in self.handle_count_all(event): yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @lofter.command("subtag")
    async def sub_tag(self, event: AstrMessageEvent):
        """订阅标签。用法：/lofter subtag <标签名> [-排除标签]"""
        if not is_admin_event(event):
            yield event.plain_result(ADMIN_ONLY_MESSAGE)
            return
        raw = self._cmd_arg(event.message_str)
        if not raw:
            yield event.plain_result("请提供标签名，例如：/lofter subtag 原创\n支持排除：/lofter subtag 原神 -R18")
            return
        subscribes, excludes = parse_tag_expr(raw)
        session_id = event.unified_msg_origin
        try:
            result = await self._subscriptions.subscribe_tags(
                session_id, subscribes, excludes
            )
        except Exception as e:
            yield event.plain_result(f"订阅失败：{e}")
            return
        added_subs = list(result.added_subscribes)
        added_excls = list(result.added_excludes)

        if not added_subs and not added_excls:
            yield event.plain_result("订阅已存在，无需重复添加")
            return

        parts = []
        if added_subs:
            parts.append(f"新增订阅：{', '.join(added_subs)}")
        if added_excls:
            parts.append(f"新增排除：{', '.join(added_excls)}")
        yield event.plain_result("\n".join(parts))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @lofter.command("subtagpreview")
    async def sub_tag_preview(self, event: AstrMessageEvent):
        """订阅标签并立即预览最新内容。用法：/lofter subtagpreview <标签名> [-排除]"""
        if not is_admin_event(event):
            yield event.plain_result(ADMIN_ONLY_MESSAGE)
            return
        raw = self._cmd_arg(event.message_str)
        if not raw:
            yield event.plain_result("请提供标签名，例如：/lofter subtagpreview 原创")
            return
        subscribes, excludes = parse_tag_expr(raw)
        if not subscribes:
            yield event.plain_result("请至少提供一个要订阅的标签（排除规则不触发预览）")
            return
        session_id = event.unified_msg_origin
        try:
            result = await self._subscriptions.subscribe_tags(
                session_id, subscribes, excludes, preview=True
            )
        except Exception as e:
            yield event.plain_result(f"订阅预览失败：{e}")
            return
        posts = list(result.preview_posts)

        if not posts:
            yield event.plain_result(f"已订阅标签「{subscribes[0]}」，暂无未屏蔽作者的匹配内容")
            return

        msg = f"已订阅标签「{subscribes[0]}」，以下是最新 {min(3, len(posts))} 条内容："
        yield event.plain_result(msg)
        is_qq = event.get_platform_name() == "aiocqhttp"
        for post in posts[:3]:
            header = f"【标签「{subscribes[0]}」有新内容】"
            chain = _post_components(
                post, header, frozenset({"tag"}), is_qq, self._max_images
            )
            yield event.chain_result(chain)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @lofter.command("subblog")
    async def sub_blog(self, event: AstrMessageEvent):
        """订阅博主。用法：/lofter subblog <用户名>"""
        if not is_admin_event(event):
            yield event.plain_result(ADMIN_ONLY_MESSAGE)
            return
        username = self._cmd_arg(event.message_str)
        if not username:
            yield event.plain_result("请提供博主用户名，例如：/lofter subblog username")
            return
        try:
            result = await self._subscriptions.subscribe_blog(
                event.unified_msg_origin, username
            )
        except Exception as e:
            yield event.plain_result(f"订阅博主失败：{e}")
            return
        ok = bool(result.added_subscribes)
        yield event.plain_result(f"已订阅博主「{username}」" if ok else f"已经订阅过博主「{username}」了")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @lofter.command("unsubtag")
    async def unsub_tag(self, event: AstrMessageEvent):
        """取消订阅标签。用法：/lofter unsubtag <标签名>"""
        if not is_admin_event(event):
            yield event.plain_result(ADMIN_ONLY_MESSAGE)
            return
        tag = self._cmd_arg(event.message_str)
        if not tag:
            yield event.plain_result("请提供标签名")
            return
        ok = await self._subscriptions.remove(
            event.unified_msg_origin, "tag", tag, "subscribe"
        )
        yield event.plain_result(f"已取消订阅标签「{tag}」" if ok else f"未找到标签「{tag}」的订阅")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @lofter.command("unexcludetag")
    async def unexclude_tag(self, event: AstrMessageEvent):
        """取消排除标签。用法：/lofter unexcludetag <标签名>"""
        if not is_admin_event(event):
            yield event.plain_result(ADMIN_ONLY_MESSAGE)
            return
        tag = self._cmd_arg(event.message_str)
        if not tag:
            yield event.plain_result("请提供标签名")
            return
        ok = await self._subscriptions.remove(
            event.unified_msg_origin, "tag", tag, "exclude"
        )
        yield event.plain_result(f"已取消排除标签「{tag}」" if ok else f"未找到标签「{tag}」的排除规则")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @lofter.command("unsubblog")
    async def unsub_blog(self, event: AstrMessageEvent):
        """取消订阅博主。用法：/lofter unsubblog <用户名>"""
        if not is_admin_event(event):
            yield event.plain_result(ADMIN_ONLY_MESSAGE)
            return
        username = self._cmd_arg(event.message_str)
        if not username:
            yield event.plain_result("请提供博主用户名")
            return
        ok = await self._subscriptions.remove(
            event.unified_msg_origin, "blog", username
        )
        yield event.plain_result(f"已取消订阅博主「{username}」" if ok else f"未找到博主「{username}」的订阅")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @lofter.command("unsub")
    async def unsub_by_index(self, event: AstrMessageEvent):
        """按编号取消订阅。用法：/lofter unsub <编号>（编号来自 /lofter list）"""
        if not is_admin_event(event):
            yield event.plain_result(ADMIN_ONLY_MESSAGE)
            return
        arg = self._cmd_arg(event.message_str)
        if not arg or not arg.isdigit():
            yield event.plain_result("请提供有效编号，例如：/lofter unsub 2\n（先用 /lofter list 查看编号）")
            return
        idx = int(arg)
        target_sub, count = await self._subscriptions.remove_by_index(
            event.unified_msg_origin, idx
        )
        if target_sub is None:
            yield event.plain_result(
                f"编号 {idx} 超出范围（当前共 {count} 条订阅）"
            )
            return
        role_label = "排除" if target_sub.role == "exclude" else "订阅"
        type_label = "标签" if target_sub.type == "tag" else "博主"
        yield event.plain_result(
            f"已删除第 {idx} 条：[{type_label}｜{role_label}] {target_sub.target}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @lofter.command("test")
    async def run_e2e_test(self, event: AstrMessageEvent):
        """运行端到端集成测试（真实网络 + 真实推送）。用法：/lofter test"""
        if not is_admin_event(event):
            yield event.plain_result(ADMIN_ONLY_MESSAGE)
            return
        from .core.e2e_test import E2ETestRunner, format_report
        runner = E2ETestRunner(
            self._source,
            self._scheduler,
            self._send_push,
        )
        yield event.plain_result(
            "开始 Lofter 实时健康检查：将访问真实 LOFTER，使用临时 SQLite；"
            "若实时 fixture 足够，最多向当前会话发送一条带“Lofter E2E 测试”标识的真实消息。"
        )
        results = await runner.run_all(event.unified_msg_origin)
        yield event.plain_result(format_report(results))
