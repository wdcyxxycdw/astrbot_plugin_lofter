"""
用 dukpy（ES5 引擎）真正执行 DWR 响应脚本，完整解析对象引用图。
"""

import asyncio
import json
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


def _execute_sync(body: str) -> list[dict]:
    if not _AVAILABLE:
        raise RuntimeError("dukpy 未安装，请执行: uv add dukpy")
    result = dukpy.evaljs(_SHIM + body + "\n" + _EXTRACT)
    if not result or result == "null":
        return []
    outer = json.loads(result)
    if not isinstance(outer, str):
        return outer if isinstance(outer, list) else []
    inner = json.loads(outer)
    return inner if isinstance(inner, list) else []


async def execute_dwr(body: str) -> list[dict]:
    """异步执行 DWR 响应，返回顶层对象列表（引用已完整展开）。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(_execute_sync, body))
