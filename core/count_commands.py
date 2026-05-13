from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .tag_count import (
    CountExpressionError,
    CountResult,
    build_count_csv,
    build_count_csv_path,
    count_posts,
    format_scanned_pages,
    is_admin_event,
    parse_count_command_arg,
)

try:
    from astrbot.api import logger
except Exception:
    logger = logging.getLogger(__name__)


ADMIN_ONLY_MESSAGE = "只有管理员可以使用统计命令"


class LofterCountCommandsMixin:
    async def handle_count(self, event):
        if not is_admin_event(event):
            yield event.plain_result(ADMIN_ONLY_MESSAGE)
            return
        raw = self._cmd_arg(event.message_str)
        try:
            name, expression = parse_count_command_arg(raw)
            result = await count_posts(expression, self._client)
        except CountExpressionError as e:
            yield event.plain_result(f"统计条件错误：{e}")
            return
        except Exception as e:
            logger.exception("Lofter: 统计「%s」失败", raw)
            yield event.plain_result(f"统计失败：{e}")
            return
        await self._db.upsert_count_condition(name, expression)
        result = replace(result, name=name)
        yield event.plain_result(_format_count_result(result))

    async def handle_count_list(self, event):
        if not is_admin_event(event):
            yield event.plain_result(ADMIN_ONLY_MESSAGE)
            return
        rows = await self._db.list_count_conditions()
        if not rows:
            yield event.plain_result("当前没有统计条件")
            return
        yield event.plain_result(_format_count_list(rows))

    async def handle_count_del(self, event):
        if not is_admin_event(event):
            yield event.plain_result(ADMIN_ONLY_MESSAGE)
            return
        arg = self._cmd_arg(event.message_str).strip()
        if not arg:
            yield event.plain_result("请提供统计条件名称或编号，例如：/lofter count-del 米哈游相关")
            return
        ok = await self._db.delete_count_condition(arg)
        if ok:
            yield event.plain_result(f"已删除统计条件「{arg}」")
            return
        yield event.plain_result(await self._delete_count_condition_by_index(arg))

    async def handle_count_all(self, event):
        if not is_admin_event(event):
            yield event.plain_result(ADMIN_ONLY_MESSAGE)
            return
        conditions = await self._db.list_count_conditions()
        if not conditions:
            yield event.plain_result("当前没有统计条件")
            return
        counted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = [await self._count_condition(name, expr, counted_at) for name, expr in conditions]
        try:
            path = self._write_count_csv(rows, counted_at)
        except Exception as e:
            logger.exception("Lofter: CSV 写入失败")
            yield event.plain_result(f"CSV 写入失败：{e}")
            return
        try:
            yield self._send_count_csv(event, path)
        except Exception:
            logger.exception("Lofter: CSV 发送失败")
            yield event.plain_result(f"CSV 已生成，但发送失败：{path}")

    async def _count_condition(self, name: str, expression: str, counted_at: str) -> CountResult:
        try:
            result = await count_posts(expression, self._client)
            return replace(result, name=name, counted_at=counted_at)
        except Exception as e:
            logger.exception("Lofter: 统计「%s」失败", name)
            return CountResult(name, expression, 0, "失败", str(e), counted_at)

    async def _delete_count_condition_by_index(self, arg: str) -> str:
        if not arg.isdigit() or int(arg) < 1:
            return f"未找到统计条件「{arg}」，可使用名称或 /lofter count-list 编号删除"
        rows = await self._db.list_count_conditions()
        idx = int(arg)
        if idx > len(rows):
            return f"编号 {idx} 超出范围（当前共 {len(rows)} 条统计条件）"
        name = rows[idx - 1][0]
        ok = await self._db.delete_count_condition(name)
        return f"已删除第 {idx} 条统计条件「{name}」" if ok else f"删除第 {idx} 条统计条件失败，请重新 count-list 确认编号"

    def _write_count_csv(self, rows: list[CountResult], counted_at: str) -> Path:
        base_dir = Path(self._db._path).parent
        path = build_count_csv_path(base_dir, counted_at)
        path.write_text(build_count_csv(rows), encoding="utf-8-sig")
        return path

    def _send_count_csv(self, event, path: Path):
        file_component = _build_file_component(path)
        if file_component is None:
            return event.plain_result(f"CSV 已生成，但当前适配器可能不支持文件发送：{path}")
        return event.chain_result([file_component])


def _format_count_result(result: CountResult) -> str:
    lines = [
        f"「{result.name}」统计完成：{result.count} 个作品",
        f"候选作品：{result.candidates}",
        f"扫描页数：{format_scanned_pages(result.scanned_pages) or '无'}",
        f"条件：{result.expression}",
    ]
    lines.extend(f"提示：{warning}" for warning in result.warnings)
    return "\n".join(lines)


def _format_count_list(rows: list[tuple[str, str]]) -> str:
    lines = ["全局统计条件："]
    for i, (name, expression) in enumerate(rows, 1):
        lines.append(f"{i}. {name} = {expression}")
    lines.append("\n用 /lofter count-del <名称或编号> 删除统计条件")
    return "\n".join(lines)


def _build_file_component(path: Path):
    try:
        import astrbot.api.message_components as Comp
    except Exception:
        return None
    file_cls = getattr(Comp, "File", None)
    if file_cls is None:
        return None
    return _try_file_factories(file_cls, path)


def _try_file_factories(file_cls, path: Path):
    for name in ("fromPath", "fromFileSystem", "fromLocalPath"):
        component = _try_file_factory(getattr(file_cls, name, None), path)
        if component is not None:
            return component
    return _try_direct_file(file_cls, path)


def _try_file_factory(factory, path: Path):
    if not callable(factory):
        return None
    try:
        return factory(str(path))
    except Exception:
        return None


def _try_direct_file(file_cls, path: Path):
    for args, kwargs in _file_constructor_candidates(path):
        try:
            return file_cls(*args, **kwargs)
        except Exception:
            continue
    return None


def _file_constructor_candidates(path: Path):
    text = str(path)
    return [((path.name,), {"file": text}), ((text,), {}), ((), {"path": text}), ((), {"file": text})]
