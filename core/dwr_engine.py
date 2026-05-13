"""
用 dukpy（ES5 引擎）真正执行 DWR 响应脚本，完整解析对象引用图。
"""

import asyncio
import json
import re
from functools import partial

try:
    import dukpy
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False

_SHIM = """
var __result__ = null;
function safeStringify(obj) {
    var seen = [];
    return JSON.stringify(obj, function(key, val) {
        if (typeof val === 'object' && val !== null) {
            if (seen.indexOf(val) !== -1) return '[Circular]';
            seen.push(val);
        }
        return val;
    });
}
var dwr = {engine: {_remoteHandleCallback: function(_, __, result) {
    __result__ = safeStringify(result);
}}};
"""

_EXTRACT = "safeStringify(__result__);"
_SHIM_LINE_COUNT = len(_SHIM.splitlines())


def _execute_sync(body: str) -> list[dict]:
    if not _AVAILABLE:
        raise RuntimeError("dukpy 未安装，请执行: uv add dukpy")
    try:
        result = dukpy.evaljs(_SHIM + body + "\n" + _EXTRACT)
    except Exception as exc:
        raise RuntimeError(_format_execute_error(body, exc)) from exc
    if not result or result == "null":
        return []
    outer = json.loads(result)
    if not isinstance(outer, str):
        return outer if isinstance(outer, list) else []
    inner = json.loads(outer)
    return inner if isinstance(inner, list) else []


def _format_execute_error(body: str, exc: Exception) -> str:
    line_no = _response_line_no(str(exc))
    if line_no is None:
        return "DWR JS 执行失败：响应无法解析，可能 Cookie 失效、未登录或触发风控"
    return f"DWR JS 执行失败：响应第 {line_no} 行附近无法解析。响应片段：{_line_preview(body, line_no)}"


def _response_line_no(message: str) -> int | None:
    match = re.search(r"line (\d+)", message)
    if not match:
        return None
    return max(1, int(match.group(1)) - _SHIM_LINE_COUNT)


def _line_preview(body: str, line_no: int) -> str:
    lines = body.splitlines() or [body]
    if line_no > len(lines):
        return ""
    return lines[line_no - 1].strip()[:120]


async def execute_dwr(body: str) -> list[dict]:
    """异步执行 DWR 响应，返回顶层对象列表（引用已完整展开）。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(_execute_sync, body))
