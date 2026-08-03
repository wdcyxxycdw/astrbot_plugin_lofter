import asyncio
import multiprocessing
import threading
import time

import pytest

import core.dwr_engine as dwr_engine
from core.dwr_engine import execute_dwr
from core.errors import DWRExecutionError, SourceLimitError, SourceSchemaError


@pytest.mark.asyncio
async def test_execute_dwr_resolves_reference_graph():
    body = """
    var s0 = {};
    var s1 = {post: s0};
    s0.blogPageUrl = "https://user.lofter.com/post/a_b";
    dwr.engine._remoteHandleCallback("0", "0", [s1]);
    """

    result = await execute_dwr(body)

    assert result == [{"post": {"blogPageUrl": "https://user.lofter.com/post/a_b"}}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "/* dwr.engine._remoteHandleCallback */",
        "__dwr_called__ = true; __dwr_result__ = [];",
        '__dwr_called__ = true; delete __dwr_called__; '
        'dwr.engine._remoteHandleCallback("0", "0", []);',
        'dwr.engine._remoteHandleCallback("0", "0", []); '
        '__dwr_called__ = true; __dwr_result__ = [];',
        'dwr.engine._remoteHandleCallback("0", "0", []); '
        'dwr.engine._remoteHandleCallback("0", "0", []);',
        'dwr.engine._remoteHandleCallback = function() {};',
    ],
)
async def test_execute_dwr_rejects_missing_multiple_or_forged_callback(body):
    with pytest.raises(DWRExecutionError):
        await execute_dwr(body)


@pytest.mark.asyncio
async def test_execute_dwr_callback_uses_first_call_snapshot():
    body = """
    var result = [{value: 'before'}];
    dwr.engine._remoteHandleCallback('0', '0', result);
    result[0].value = 'after';
    """
    assert await execute_dwr(body) == [{"value": "before"}]


@pytest.mark.asyncio
async def test_execute_dwr_rejects_non_list_callback():
    body = 'dwr.engine._remoteHandleCallback("0", "0", {value: 1});'

    with pytest.raises(SourceSchemaError) as exc_info:
        await execute_dwr(body)

    assert exc_info.value.location == "dwr.callback"


@pytest.mark.asyncio
async def test_execute_dwr_rejects_unencodable_input_before_spawn():
    with pytest.raises(SourceSchemaError) as exc_info:
        await execute_dwr("\ud800")

    assert exc_info.value.location == "dwr.input"


@pytest.mark.asyncio
async def test_execute_dwr_reports_js_failure_without_response_content():
    secret = "sensitive-response-fragment"

    with pytest.raises(DWRExecutionError) as exc_info:
        await execute_dwr(f"var value = '{secret}';\nunterminated(")

    assert secret not in str(exc_info.value)
    assert "响应片段" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_execute_dwr_enforces_output_limit(monkeypatch):
    monkeypatch.setattr(dwr_engine, "_MAX_OUTPUT_BYTES", 1024)
    body = """
    var value = new Array(4097).join('x');
    dwr.engine._remoteHandleCallback("0", "0", [value]);
    """

    with pytest.raises(SourceLimitError) as exc_info:
        await execute_dwr(body)

    assert exc_info.value.resource == "body"
    assert exc_info.value.limit == 1024


@pytest.mark.asyncio
async def test_execute_dwr_timeout_terminates_and_joins(monkeypatch):
    monkeypatch.setattr(dwr_engine, "_WALL_TIMEOUT_SECONDS", 0.2)
    before = {process.pid for process in multiprocessing.active_children()}

    with pytest.raises(DWRExecutionError):
        await execute_dwr("while (true) {}")

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        remaining = [
            process
            for process in multiprocessing.active_children()
            if process.pid not in before
        ]
        if not remaining:
            break
        await asyncio.sleep(0.02)
    assert remaining == []


@pytest.mark.asyncio
async def test_execute_dwr_cancellation_requests_worker_cleanup(monkeypatch):
    stopped = threading.Event()

    def fake_run(body, cancel_event):
        while not cancel_event.wait(0.01):
            pass
        stopped.set()
        raise dwr_engine._ExecutionCancelled()

    monkeypatch.setattr(dwr_engine, "_run_isolated", fake_run)
    task = asyncio.create_task(execute_dwr("ignored"))
    await asyncio.sleep(0.02)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.wait(1)
