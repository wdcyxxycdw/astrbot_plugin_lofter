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
var __called__ = false;
var __error__ = null;
function safeStringify(obj) {
    var ancestors = [];
    return JSON.stringify(obj, function(key, val) {
        if (typeof val === 'object' && val !== null) {
            while (ancestors.length && ancestors[ancestors.length - 1] !== this) ancestors.pop();
            if (ancestors.indexOf(val) !== -1) return '[Circular]';
            ancestors.push(val);
        }
        return val;
    });
}
var dwr = {engine: {
    _remoteHandleCallback: function(_, __, result) {
        __called__ = true;
        __result__ = result;
    },
    _remoteHandleException: function(_, __, error) { __error__ = error; },
    _remoteHandleBatchException: function(error) { __error__ = error; }
}};
"""

_EXTRACT = "safeStringify({called: __called__, result: __result__, error: __error__});"
_SHIM_LINE_COUNT = len(_SHIM.splitlines())


def _execute_sync(body: str) -> list[dict]:
    if not _AVAILABLE:
        raise RuntimeError("dukpy 未安装，请执行: uv add dukpy")
    try:
        result = dukpy.evaljs(_SHIM + body + "\n" + _EXTRACT)
    except Exception as exc:
        raise RuntimeError(_format_execute_error(body, exc)) from exc
    envelope = json.loads(result)
    if envelope["error"] is not None:
        raise RuntimeError("DWR 返回服务端异常，可能 Cookie 失效或触发风控")
    if not envelope["called"]:
        raise RuntimeError("DWR 未执行结果回调")
    if not isinstance(envelope["result"], list):
        raise RuntimeError("DWR 结果结构异常：预期帖子列表，不能作为空页处理")
    return envelope["result"]


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
