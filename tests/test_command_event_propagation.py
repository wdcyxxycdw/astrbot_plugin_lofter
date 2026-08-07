import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.parser import Post
from core.permissions import ADMIN_ONLY_MESSAGE
from tests.test_command_permissions import (
    ADMIN_HANDLERS,
    PUBLIC_HANDLERS,
    DeniedEvent,
    _load_main_module,
)


class _Event:
    unified_msg_origin = "sess"

    def __init__(self, message_str="", platform_name="test"):
        self.message_str = message_str
        self.platform_name = platform_name
        self.events = []
        self.stopped = False

    def is_admin(self):
        return True

    def plain_result(self, text):
        self.events.append(("yield", text))
        return text

    def chain_result(self, chain):
        self.events.append(("yield", chain))
        return chain

    def get_platform_name(self):
        return self.platform_name

    def stop_event(self):
        self.events.append(("stop", None))
        self.stopped = True


async def _collect(handler, plugin, event):
    return [item async for item in handler(plugin, event)]


def test_all_eighteen_command_handlers_are_wrapped_only_once():
    main = _load_main_module()
    handlers = ADMIN_HANDLERS | PUBLIC_HANDLERS

    assert len(handlers) == 18
    for name in handlers:
        handler = getattr(main.LofterPlugin, name)
        assert handler.stops_lofter_command is True
        assert not getattr(handler.__wrapped__, "stops_lofter_command", False)
    assert not hasattr(main.LofterPlugin.auto_parse, "stops_lofter_command")
    for name in (
        "lofter_subscription",
        "lofter_author_block",
        "lofter_content",
        "lofter_count",
    ):
        assert getattr(
            getattr(main.LofterPlugin, name), "stops_lofter_command", False
        ) is not True


def test_wrapper_preserves_handler_metadata():
    main = _load_main_module()
    handler = main.LofterPlugin.search
    original = handler.__wrapped__

    assert handler.__name__ == original.__name__
    assert handler.__module__ == original.__module__
    assert handler.__doc__ == original.__doc__
    assert inspect.signature(handler) == inspect.signature(original)


@pytest.mark.asyncio
async def test_internal_admin_rejection_stops_after_reply():
    main = _load_main_module()
    event = DeniedEvent()

    results = await _collect(main.LofterPlugin.search, object(), event)

    assert results == [ADMIN_ONLY_MESSAGE]
    assert event.stop_calls == 1


@pytest.mark.asyncio
async def test_wrapper_stops_only_after_all_results_are_consumed():
    main = _load_main_module()
    event = _Event()

    async def handler(self, current_event):
        for value in ("one", "two", "three"):
            current_event.events.append(("body", value))
            yield value

    wrapped = main._stop_after_lofter_command(handler)
    results = await _collect(wrapped, object(), event)

    assert results == ["one", "two", "three"]
    assert event.events == [
        ("body", "one"),
        ("body", "two"),
        ("body", "three"),
        ("stop", None),
    ]


@pytest.mark.asyncio
async def test_wrapper_does_not_stop_on_error_or_early_close():
    main = _load_main_module()

    async def failing(self, event):
        yield "before-error"
        raise RuntimeError("canary")

    failed_event = _Event()
    generator = main._stop_after_lofter_command(failing)(object(), failed_event)
    assert await anext(generator) == "before-error"
    with pytest.raises(RuntimeError, match="canary"):
        await anext(generator)
    assert failed_event.stopped is False

    early_event = _Event()
    generator = main._stop_after_lofter_command(failing)(object(), early_event)
    assert await anext(generator) == "before-error"
    await generator.aclose()
    assert early_event.stopped is False


@pytest.mark.asyncio
async def test_wrapper_does_not_stop_when_consumer_is_cancelled():
    main = _load_main_module()
    event = _Event()
    waiting = asyncio.Event()

    async def blocked(self, current_event):
        yield "before-wait"
        waiting.set()
        await asyncio.Event().wait()

    generator = main._stop_after_lofter_command(blocked)(object(), event)
    assert await anext(generator) == "before-wait"
    next_result = asyncio.create_task(anext(generator))
    await asyncio.sleep(0)
    assert waiting.is_set()
    next_result.cancel()
    with pytest.raises(asyncio.CancelledError):
        await next_result
    assert event.stopped is False


@pytest.mark.asyncio
async def test_search_preview_and_e2e_stop_after_multi_yield(monkeypatch):
    main = _load_main_module()
    post = Post(
        post_id="p",
        title="title",
        summary="",
        url="https://u.lofter.com/post/p",
        publish_time="2026-08-01 00:00:00",
    )
    plugin = object.__new__(main.LofterPlugin)
    plugin._source = object()
    plugin._search_limit = 3
    plugin._max_images = 1
    plugin._author_blocks = SimpleNamespace(list_by_session=AsyncMock(return_value=[]))
    plugin._subscriptions = SimpleNamespace(
        subscribe_tags=AsyncMock(return_value=SimpleNamespace(preview_posts=(post,)))
    )
    monkeypatch.setattr(main, "_search_unique_posts", AsyncMock(return_value=[post]))
    monkeypatch.setattr(main, "filter_blocked_with_fields", AsyncMock(return_value=([post], [])))

    search_event = _Event("/lofter search tag")
    search_results = await _collect(main.LofterPlugin.search, plugin, search_event)
    assert len(search_results) == 2
    assert search_event.events[-1] == ("stop", None)

    preview_event = _Event("/lofter subtagpreview tag")
    preview_results = await _collect(
        main.LofterPlugin.sub_tag_preview, plugin, preview_event
    )
    assert len(preview_results) == 2
    assert preview_event.events[-1] == ("stop", None)

    e2e_module = __import__(
        "lofter_permission_test.core.e2e_test", fromlist=["E2ETestRunner"]
    )
    runner = SimpleNamespace(run_all=AsyncMock(return_value=[]))
    monkeypatch.setattr(e2e_module, "E2ETestRunner", lambda *args: runner)
    monkeypatch.setattr(e2e_module, "format_report", lambda results: "report")
    plugin._scheduler = object()
    plugin._send_push_result = AsyncMock()
    test_event = _Event("/lofter test")
    test_results = await _collect(main.LofterPlugin.run_e2e_test, plugin, test_event)
    assert len(test_results) == 2
    assert test_event.events[-1] == ("stop", None)


@pytest.mark.asyncio
async def test_minimal_dispatch_skips_following_all_handler():
    main = _load_main_module()
    event = _Event()
    sentinel_calls = []

    async def command(self, current_event):
        yield current_event.plain_result("reply")

    async def sentinel():
        sentinel_calls.append("called")

    results = await _collect(
        main._stop_after_lofter_command(command), object(), event
    )
    if not event.stopped:
        await sentinel()

    assert results == ["reply"]
    assert sentinel_calls == []
