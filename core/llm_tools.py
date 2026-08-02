import importlib.util
import logging
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any


def _is_package_module(module: ModuleType) -> bool:
    spec = getattr(module, "__spec__", None)
    return bool(spec is not None and spec.submodule_search_locations is not None) or hasattr(module, "__path__")


def _astrbot_package_missing() -> bool:
    loaded = sys.modules.get("astrbot")
    if loaded is not None and not _is_package_module(loaded):
        return True
    loaded_api = sys.modules.get("astrbot.api")
    if loaded_api is not None and not _is_package_module(loaded_api):
        return True
    try:
        return importlib.util.find_spec("astrbot") is None
    except (ModuleNotFoundError, ValueError):
        return True


try:
    from astrbot.api.event import AstrMessageEvent, filter
except ModuleNotFoundError:
    if not _astrbot_package_missing():
        raise
    AstrMessageEvent = Any

    class _FallbackFilter:
        @staticmethod
        def llm_tool(*args, **kwargs):
            def decorator(func):
                return func
            return decorator

    filter = _FallbackFilter()

from .content_source import collect_pages
from .count_commands import _format_count_list
from .count_formatters import format_count_result as _format_count_result
from .filter import parse_tag_expr
from .llm_tool_formatters import format_added_tag_result as _format_added_tag_result
from .llm_tool_formatters import format_index_remove_result as _format_index_remove_result
from .llm_tool_formatters import format_post_for_tool as _format_post_for_tool
from .llm_tool_formatters import format_preview_result as _format_preview_result
from .llm_tool_formatters import format_remove_result as _format_remove_result
from .llm_tool_formatters import format_subscription_line as _format_subscription_line
from .llm_tool_formatters import missing_remove_target as _missing_remove_target
from .llm_tool_formatters import unknown_action as _unknown_action
from .permissions import ADMIN_ONLY_MESSAGE, is_admin_event
from .post_consumers import filter_blocked_with_fields
from .tag_count import CountExpressionError, CountResult, build_count_csv, build_count_csv_path, count_posts

try:
    from astrbot.api import logger
except ModuleNotFoundError:
    if not _astrbot_package_missing():
        raise
    logger = logging.getLogger(__name__)


SUBSCRIPTION_ACTIONS = (
    "list",
    "subscribe_tag",
    "preview_tag",
    "subscribe_blog",
    "unsubscribe_tag",
    "unexclude_tag",
    "unsubscribe_blog",
    "unsubscribe_index",
)
AUTHOR_BLOCK_ACTIONS = ("list", "block", "unblock")
CONTENT_ACTIONS = ("search",)
COUNT_ACTIONS = ("run", "list", "delete", "run_all")
MAX_TOOL_INTEGER = 2_147_483_647


def _validate_tool_inputs(
    strings: dict[str, Any],
    integers: dict[str, Any] | None = None,
) -> str | None:
    for name, value in strings.items():
        if not isinstance(value, str):
            return f"参数「{name}」必须是字符串"
    for name, value in (integers or {}).items():
        if type(value) is not int:
            return f"参数「{name}」必须是整数"
        if abs(value) > MAX_TOOL_INTEGER:
            return f"参数「{name}」超出允许范围"
    return None


def _plugin_main_module_path(module_path: str) -> str:
    if module_path == "core.llm_tools":
        return "main"
    suffix = ".core.llm_tools"
    if module_path.endswith(suffix):
        return f"{module_path[:-len(suffix)]}.main"
    return module_path


def _lofter_llm_tool(*args: Any, **kwargs: Any):
    def decorator(func: Any):
        func.__module__ = _plugin_main_module_path(func.__module__)
        return filter.llm_tool(*args, **kwargs)(func)

    return decorator


class LofterLLMToolsMixin:
    @_lofter_llm_tool(name="lofter_subscription")
    async def lofter_subscription(
        self,
        event: AstrMessageEvent,
        action: str,
        target: str = "",
        index: int = 0,
    ) -> str:
        """管理当前会话的 Lofter 标签订阅、标签排除和博主订阅。
        自然语言映射：查看当前订阅 -> list；订阅/关注标签 -> subscribe_tag；
        订阅标签并立即看最新内容 -> preview_tag；订阅/关注博主 -> subscribe_blog；
        取消标签订阅 -> unsubscribe_tag；不再排除标签 -> unexclude_tag；
        取消博主订阅 -> unsubscribe_blog；按 list 编号删除规则 -> unsubscribe_index。

        订阅排除规则：subscribe_tag/preview_tag 的 target 复用 parse_tag_expr，
        如 "原神 -R18 -剧透" 会新增标签订阅「原神」和排除标签「R18」「剧透」。
        排除标签只过滤帖子标签，不是作者屏蔽；取消排除必须用 unexclude_tag，
        不会误删同名标签订阅。新增订阅按会话隔离，并在抓取验证实际来源后原子写入；
        合法空结果可激活，失败或策略变化零部分副作用；preview 写实际来源 seen 但不创建投递。

        Args:
            action(string): list、subscribe_tag、preview_tag、subscribe_blog、unsubscribe_tag、
                unexclude_tag、unsubscribe_blog 或 unsubscribe_index。
            target(string): 标签表达式、标签名或博主用户名。
            index(number): unsubscribe_index 使用的编号，来自 list 返回结果。
        """
        if not is_admin_event(event):
            return ADMIN_ONLY_MESSAGE
        error = _validate_tool_inputs(
            {"action": action, "target": target},
            {"index": index},
        )
        if error:
            return error
        action = action.strip().lower()
        session_id = event.unified_msg_origin
        if action == "list":
            return await self._llm_subscription_list(session_id)
        if action in {"subscribe_tag", "preview_tag"}:
            return await self._llm_subscribe_tag(session_id, target, action == "preview_tag")
        if action == "subscribe_blog":
            return await self._llm_subscribe_blog(session_id, target)
        if action == "unsubscribe_tag":
            return await self._llm_remove_subscription(session_id, "tag", target, "subscribe")
        if action == "unexclude_tag":
            return await self._llm_remove_subscription(session_id, "tag", target, "exclude")
        if action == "unsubscribe_blog":
            return await self._llm_remove_subscription(session_id, "blog", target, "subscribe")
        if action == "unsubscribe_index":
            return await self._llm_unsubscribe_index(session_id, index)
        return _unknown_action("lofter_subscription", SUBSCRIPTION_ACTIONS)

    @_lofter_llm_tool(name="lofter_author_block")
    async def lofter_author_block(self, event: AstrMessageEvent, action: str, author: str = "") -> str:
        """管理当前会话的 Lofter 作者屏蔽名单。

        自然语言映射：屏蔽作者/以后不要看某作者作品 -> block；解除屏蔽/恢复显示 -> unblock；
        查看屏蔽名单 -> list。

        作者屏蔽规则：author 可为昵称、Lofter 用户名或 https://username.lofter.com，
        复用 AuthorBlockStorage 归一化逻辑并按 event.unified_msg_origin 会话隔离。
        它不同于订阅排除标签：会按作者昵称或用户名过滤自动解析、搜索、订阅预览和推送；
        解除屏蔽后不会补推屏蔽期间已处理的旧内容。

        Args:
            action(string): list、block 或 unblock。
            author(string): 作者昵称、用户名或 Lofter 主页 URL。
        """
        if not is_admin_event(event):
            return ADMIN_ONLY_MESSAGE
        error = _validate_tool_inputs({"action": action, "author": author})
        if error:
            return error
        action = action.strip().lower()
        session_id = event.unified_msg_origin
        if action == "list":
            return await self._llm_author_block_list(session_id)
        if action == "block":
            return await self._llm_block_author(session_id, author)
        if action == "unblock":
            return await self._llm_unblock_author(session_id, author)
        return _unknown_action("lofter_author_block", AUTHOR_BLOCK_ACTIONS)

    @_lofter_llm_tool(name="lofter_content")
    async def lofter_content(
        self,
        event: AstrMessageEvent,
        action: str,
        query: str = "",
        limit: int = 0,
    ) -> str:
        """搜索 Lofter 标签内容，不解析消息里的链接。

        自然语言映射：搜索/查找/看看某标签内容 -> search。帖子链接解析由自动消息事件触发，
        cookie、test 和 parse 不通过 LLM tool 暴露；本工具只按 query 搜索标签，
        并应用当前会话的作者屏蔽规则。

        Args:
            action(string): search。
            query(string): 要搜索的标签名。
            limit(number): 返回数量上限，最大 100；不传或小于等于 0 时使用默认搜索上限。
        """
        if not is_admin_event(event):
            return ADMIN_ONLY_MESSAGE
        error = _validate_tool_inputs(
            {"action": action, "query": query},
            {"limit": limit},
        )
        if error:
            return error
        action = action.strip().lower()
        if action != "search":
            return _unknown_action("lofter_content", CONTENT_ACTIONS)
        keyword = query.strip()
        if not keyword:
            return "请提供标签名，例如：action=search, query=原创"
        try:
            posts = await self._search_content_posts(keyword, limit)
            blocks = await self._author_blocks.list_by_session(event.unified_msg_origin)
            posts, _ = await filter_blocked_with_fields(
                posts, blocks, self._source
            )
            if not posts:
                return "没有找到未屏蔽作者的相关内容"
            lines = [f"「{keyword}」标签搜索结果，共 {len(posts)} 条："]
            lines.extend(_format_post_for_tool(post, include_time=True, max_images=self._max_images) for post in posts)
            return "\n\n".join(lines)
        except Exception as e:
            logger.exception("lofter_content search failed")
            return f"搜索失败：{e}"

    @_lofter_llm_tool(name="lofter_count")
    async def lofter_count(
        self,
        event: AstrMessageEvent,
        action: str,
        name: str = "",
        expression: str = "",
        target: str = "",
    ) -> str:
        """管理全局 Lofter 标签统计条件并执行统计。

        统计条件是全局配置，不按会话隔离；只有管理员可用。自然语言映射：
        查看统计条件 -> list；新增或运行统计 -> run；删除统计条件 -> delete；
        运行全部统计并生成 CSV -> run_all。cookie、test 和 parse 不通过 LLM tool 暴露。

        Args:
            action(string): run、list、delete 或 run_all。
            name(string): run 使用的统计名称。
            expression(string): run 使用的标签统计表达式，支持 AND/OR/NOT/括号。
            target(string): delete 使用的统计条件名称或 list 编号。
        """
        if not is_admin_event(event):
            return ADMIN_ONLY_MESSAGE
        error = _validate_tool_inputs(
            {"action": action, "name": name, "expression": expression, "target": target}
        )
        if error:
            return error
        action = action.strip().lower()
        if action == "run":
            return await self._llm_count_run(name, expression)
        if action == "list":
            return await self._llm_count_list()
        if action == "delete":
            return await self._llm_count_delete(target)
        if action == "run_all":
            return await self._llm_count_run_all()
        return _unknown_action("lofter_count", COUNT_ACTIONS)

    def _resolve_tool_limit(self, limit: int) -> int:
        return min(self._search_limit if limit <= 0 else limit, 100)

    async def _search_content_posts(self, keyword: str, limit: int) -> list[Any]:
        search_limit = self._resolve_tool_limit(limit)
        page = await collect_pages(
            lambda cursor: self._source.list_tag(
                keyword, cursor, min(search_limit, 100), "new"
            ),
            limit=search_limit,
        )
        return page.items

    async def _llm_subscription_list(self, session_id: str) -> str:
        subs = await self._storage.list_by_session(session_id)
        if not subs:
            return "当前没有订阅"
        lines = ["当前订阅列表：", *(_format_subscription_line(i, sub) for i, sub in enumerate(subs, 1))]
        lines.append("\n用 action=unsubscribe_index 和 index 编号取消订阅")
        return "\n".join(lines)

    async def _llm_subscribe_tag(self, session_id: str, raw: str, preview: bool) -> str:
        raw = raw.strip()
        if not raw:
            return "请提供标签名，例如：action=subscribe_tag, target=原创；支持排除：target=原神 -R18"
        subscribes, excludes = parse_tag_expr(raw)
        if preview and not subscribes:
            return "请至少提供一个要订阅的标签（排除规则不触发预览）"
        try:
            result = await self._subscriptions.subscribe_tags(
                session_id, subscribes, excludes, preview=preview
            )
        except Exception as e:
            return f"订阅{'预览' if preview else ''}失败：{e}"
        if preview:
            return _format_preview_result(
                subscribes[0], list(result.preview_posts), self._max_images
            )
        return _format_added_tag_result(
            list(result.added_subscribes), list(result.added_excludes)
        )

    async def _llm_subscribe_blog(self, session_id: str, username: str) -> str:
        username = username.strip()
        if not username:
            return "请提供博主用户名，例如：action=subscribe_blog, target=username"
        try:
            result = await self._subscriptions.subscribe_blog(
                session_id, username
            )
        except Exception as e:
            return f"订阅博主失败：{e}"
        ok = bool(result.added_subscribes)
        return f"已订阅博主「{username}」" if ok else f"已经订阅过博主「{username}」了"

    async def _llm_remove_subscription(self, session_id: str, sub_type: str, target: str, role: str) -> str:
        target = target.strip()
        if not target:
            return _missing_remove_target(sub_type, role)
        ok = await self._subscriptions.remove(
            session_id, sub_type, target, role
        )
        return _format_remove_result(ok, sub_type, target, role)

    async def _llm_unsubscribe_index(self, session_id: str, index: int) -> str:
        if index < 1:
            return "请提供有效编号，例如：action=unsubscribe_index, index=2（先用 action=list 查看编号）"
        target_sub, count = await self._subscriptions.remove_by_index(
            session_id, index
        )
        if target_sub is None:
            return f"编号 {index} 超出范围（当前共 {count} 条订阅）"
        return _format_index_remove_result(True, index, target_sub)

    async def _llm_author_block_list(self, session_id: str) -> str:
        blocks = await self._author_blocks.list_by_session(session_id)
        if not blocks:
            return "当前没有屏蔽作者"
        lines = ["当前屏蔽作者列表："]
        lines.extend(f"{i}. [{'用户名' if block.kind == 'username' else '昵称'}] {block.display}" for i, block in enumerate(blocks, 1))
        return "\n".join(lines)

    async def _llm_block_author(self, session_id: str, author: str) -> str:
        author = author.strip()
        if not author:
            return "请提供作者昵称或用户名，例如：action=block, author=username"
        ok = await self._author_blocks.add(session_id, author)
        return f"已屏蔽作者「{author}」" if ok else f"作者「{author}」已在屏蔽列表中"

    async def _llm_unblock_author(self, session_id: str, author: str) -> str:
        author = author.strip()
        if not author:
            return "请提供作者昵称或用户名，例如：action=unblock, author=username"
        ok = await self._author_blocks.remove(session_id, author)
        return f"已解除屏蔽作者「{author}」" if ok else f"未找到作者「{author}」的屏蔽记录"

    async def _llm_count_run(self, name: str, expression: str) -> str:
        name = name.strip()
        expression = expression.strip()
        if not name or not expression:
            return "请提供统计名称和表达式，例如：action=run, name=米哈游相关, expression=原神|崩铁 -R18"
        try:
            result = await count_posts(expression, self._source)
        except CountExpressionError as e:
            return f"统计条件错误：{e}"
        except Exception as e:
            logger.exception("Lofter: LLM 统计「%s」失败", name)
            return f"统计失败：{e}"
        await self._db.upsert_count_condition(name, expression)
        return _format_count_result(replace(result, name=name))

    async def _llm_count_list(self) -> str:
        rows = await self._db.list_count_conditions()
        return _format_count_list(rows) if rows else "当前没有统计条件"

    async def _llm_count_delete(self, target: str) -> str:
        target = target.strip()
        if not target:
            return "请提供统计条件名称或编号，例如：action=delete, target=米哈游相关"
        ok = await self._db.delete_count_condition(target)
        return f"已删除统计条件「{target}」" if ok else await self._llm_delete_count_condition_by_index(target)

    async def _llm_delete_count_condition_by_index(self, target: str) -> str:
        if not target.isdigit() or int(target) < 1:
            return f"未找到统计条件「{target}」，可使用名称或 list 编号删除"
        rows = await self._db.list_count_conditions()
        idx = int(target)
        if idx > len(rows):
            return f"编号 {idx} 超出范围（当前共 {len(rows)} 条统计条件）"
        name = rows[idx - 1][0]
        ok = await self._db.delete_count_condition(name)
        return f"已删除第 {idx} 条统计条件「{name}」" if ok else f"删除第 {idx} 条统计条件失败，请重新 list 确认编号"

    async def _llm_count_run_all(self) -> str:
        conditions = await self._db.list_count_conditions()
        if not conditions:
            return "当前没有统计条件"
        counted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = [await self._llm_count_condition(name, expr, counted_at) for name, expr in conditions]
        try:
            path = self._llm_write_count_csv(rows, counted_at)
        except Exception as e:
            logger.exception("Lofter: LLM CSV 写入失败")
            return f"CSV 写入失败：{e}"
        return f"CSV 已生成：{path}"

    async def _llm_count_condition(self, name: str, expression: str, counted_at: str) -> CountResult:
        try:
            result = await count_posts(expression, self._source)
            return replace(result, name=name, counted_at=counted_at)
        except Exception as e:
            logger.exception("Lofter: LLM 统计「%s」失败", name)
            return CountResult(
                name, expression, 0, "failed", str(e), counted_at
            )

    def _llm_write_count_csv(self, rows: list[CountResult], counted_at: str) -> Path:
        base_dir = Path(self._db._path).parent
        path = build_count_csv_path(base_dir, counted_at)
        path.write_text(build_count_csv(rows), encoding="utf-8-sig")
        return path
