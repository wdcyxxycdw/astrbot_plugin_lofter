"""在受限独立进程中执行 DWR 响应脚本。"""

import asyncio
import json
import multiprocessing
import os
import threading
import time
from multiprocessing.connection import Connection
from typing import Any

from .errors import DWRExecutionError, SourceLimitError, SourceSchemaError

try:
    import dukpy

    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False

_MAX_INPUT_BYTES = 5 * 1024 * 1024
_MAX_OUTPUT_BYTES = 10 * 1024 * 1024
_WALL_TIMEOUT_SECONDS = 5.0
_CPU_LIMIT_SECONDS = 2
_POLL_INTERVAL_SECONDS = 0.05

_SHIM = r"""
(function(source) {
    var stringify = JSON.stringify;
    var keys = Object.keys;
    var objectString = Object.prototype.toString;
    var root = Function('return this')();
    var forgedNames = ['__dwr_called__', '__dwr_result__'];
    var count = 0;
    var tampered = false;
    var snapshot = null;
    forgedNames.forEach(function(name) {
        Object.defineProperty(root, name, {
            enumerable: false, configurable: false,
            get: function() { tampered = true; return undefined; },
            set: function() { tampered = true; }
        });
    });
    function decycle(value, stack) {
        if (typeof value !== 'object' || value === null) return value;
        if (stack.indexOf(value) !== -1) return '[Circular]';
        stack.push(value);
        var copy = objectString.call(value) === '[object Array]' ? [] : {};
        keys(value).forEach(function(key) { copy[key] = decycle(value[key], stack); });
        stack.pop();
        return copy;
    }
    function callback(_, __, result) {
        count += 1;
        if (count === 1) snapshot = stringify(decycle(result, []));
    }
    var engine = {};
    Object.defineProperty(engine, '_remoteHandleCallback', {
        enumerable: true, configurable: false,
        get: function() { return callback; },
        set: function() { tampered = true; }
    });
    var api = {};
    Object.defineProperty(api, 'engine', {
        enumerable: true, configurable: false,
        get: function() { return engine; },
        set: function() { tampered = true; }
    });
    Function('dwr', source)(api);
    return stringify({count: count, tampered: tampered, snapshot: snapshot});
})
"""


class _ExecutionCancelled(Exception):
    pass


def validate_dwr_input(body: str) -> None:
    if not isinstance(body, str):
        raise SourceSchemaError("dwr.input")
    try:
        input_size = len(body.encode("utf-8"))
    except UnicodeEncodeError:
        raise SourceSchemaError("dwr.input") from None
    if input_size > _MAX_INPUT_BYTES:
        raise SourceLimitError("body", _MAX_INPUT_BYTES)


def _apply_cpu_limit() -> None:
    if os.name == "nt":
        return
    import resource

    resource.setrlimit(
        resource.RLIMIT_CPU,
        (_CPU_LIMIT_SECONDS, _CPU_LIMIT_SECONDS),
    )


def _evaluate(body: str) -> dict[str, Any]:
    if not _AVAILABLE:
        raise RuntimeError("dukpy unavailable")
    script = _SHIM + "(" + json.dumps(body, ensure_ascii=True) + ");"
    raw = dukpy.evaljs(script)
    if not isinstance(raw, str):
        raise ValueError("invalid engine result")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid engine envelope")
    return value


def _serialize_wire(payload: dict[str, Any]) -> bytes:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) <= _MAX_OUTPUT_BYTES:
        return encoded
    return b'{"status":"output_limit"}'


def _worker(send_conn: Connection, body: str) -> None:
    try:
        _apply_cpu_limit()
        payload = {"status": "ok", "value": _evaluate(body)}
    except BaseException:
        payload = {"status": "error"}
    try:
        send_conn.send_bytes(_serialize_wire(payload))
    finally:
        send_conn.close()


def _terminate_and_join(process: multiprocessing.Process) -> None:
    if process.pid is None:
        return
    if process.is_alive():
        process.terminate()
    process.join(1.0)
    if process.is_alive() and hasattr(process, "kill"):
        process.kill()
        process.join(1.0)


def _wait_for_wire(
    process: multiprocessing.Process,
    recv_conn: Connection,
    cancel_event: threading.Event,
) -> bytes:
    deadline = time.monotonic() + _WALL_TIMEOUT_SECONDS
    while True:
        if cancel_event.is_set():
            raise _ExecutionCancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DWRExecutionError()
        if recv_conn.poll(min(_POLL_INTERVAL_SECONDS, remaining)):
            break
    try:
        return recv_conn.recv_bytes(_MAX_OUTPUT_BYTES)
    except OSError as exc:
        raise SourceLimitError("body", _MAX_OUTPUT_BYTES) from exc
    except EOFError as exc:
        process.join(0.1)
        raise DWRExecutionError() from exc


def _decode_wire(wire: bytes) -> dict[str, Any]:
    try:
        message = json.loads(wire.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DWRExecutionError() from exc
    if not isinstance(message, dict):
        raise DWRExecutionError()
    return message


def _interpret_message(message: dict[str, Any]) -> list[object]:
    status = message.get("status")
    if status == "output_limit":
        raise SourceLimitError("body", _MAX_OUTPUT_BYTES)
    if status != "ok":
        raise DWRExecutionError()
    envelope = message.get("value")
    if not isinstance(envelope, dict):
        raise DWRExecutionError()
    if envelope.get("count") != 1 or envelope.get("tampered") is not False:
        raise DWRExecutionError()
    snapshot = envelope.get("snapshot")
    if not isinstance(snapshot, str):
        raise DWRExecutionError()
    try:
        result = json.loads(snapshot)
    except json.JSONDecodeError as exc:
        raise DWRExecutionError() from exc
    if not isinstance(result, list):
        raise SourceSchemaError("dwr.callback")
    return result


def _run_isolated(body: str, cancel_event: threading.Event) -> list[object]:
    context = multiprocessing.get_context("spawn")
    recv_conn, send_conn = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(send_conn, body), daemon=True)
    try:
        process.start()
        send_conn.close()
        message = _decode_wire(_wait_for_wire(process, recv_conn, cancel_event))
        if message.get("status") == "output_limit":
            _terminate_and_join(process)
        else:
            process.join(1.0)
        if process.is_alive():
            _terminate_and_join(process)
            raise DWRExecutionError()
        return _interpret_message(message)
    except (DWRExecutionError, SourceLimitError, SourceSchemaError, _ExecutionCancelled):
        _terminate_and_join(process)
        raise
    except BaseException as exc:
        _terminate_and_join(process)
        raise DWRExecutionError() from exc
    finally:
        recv_conn.close()
        send_conn.close()


async def execute_dwr(body: str) -> list[object]:
    """执行 DWR 并返回回调顶层列表。"""
    validate_dwr_input(body)
    cancel_event = threading.Event()
    waiter = asyncio.create_task(asyncio.to_thread(_run_isolated, body, cancel_event))
    try:
        return await asyncio.shield(waiter)
    except asyncio.CancelledError:
        cancel_event.set()
        try:
            await asyncio.shield(waiter)
        except (_ExecutionCancelled, DWRExecutionError):
            pass
        raise
