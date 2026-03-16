import re

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter

from .core.parser import parse_post, parse_search_results

POST_PATTERN = re.compile(r"[a-zA-Z0-9_-]+\.lofter\.com/post/[a-zA-Z0-9_-]+")
SEARCH_URL = "https://www.lofter.com/s?q={keyword}"


class LofterCommands:
    """命令处理 mixin，通过多继承挂载到 LofterPlugin"""

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

    @filter.command_group("lofter")
    def lofter(self): ...

    @lofter.command("search")
    async def search(self, event: AstrMessageEvent):
        """搜索 Lofter 内容。用法：/lofter search <关键词>"""
        keyword = event.message_str.strip()
        if not keyword:
            yield event.plain_result("请提供搜索关键词，例如：/lofter search 原创")
            return
        url = SEARCH_URL.format(keyword=keyword)
        try:
            html = await self._client.get(url)
            posts = await parse_search_results(html)
        except Exception as e:
            yield event.plain_result(f"搜索失败：{e}")
            return
        if not posts:
            yield event.plain_result("没有找到相关内容")
            return
        lines = [f"「{keyword}」搜索结果："]
        for p in posts[:5]:
            lines.append(f"• {p.title or '(无标题)'}\n  {p.url}")
        yield event.plain_result("\n".join(lines))

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
