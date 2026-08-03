import asyncio
from types import SimpleNamespace

import aiohttp
import pytest

import core.client as client_module
from core.client import (
    DWR_SEARCH_URL,
    GLOBAL_HTTP_LIMIT,
    HOST_HTTP_LIMIT,
    LofterClient,
    MAX_ATTEMPTS,
    MAX_BODY_BYTES,
    build_tag_search_body,
)
from core.errors import (
    SourceClosingError,
    SourceHTTPError,
    SourceLimitError,
    SourceRetryExhaustedError,
    SourceSchemaError,
    SourceTimeoutError,
)


class FakeContent:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self._chunks:
            yield chunk


class FakeResponse:
    def __init__(self, status=200, body=b"ok", headers=None):
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent([body])
        self.charset = "utf-8"
        self.released = False

    def release(self):
        self.released = True


class FakeSession:
    def __init__(self, responses, request_hook=None):
        self.responses = list(responses)
        self.request_hook = request_hook
        self.requests = []
        self.closed = False
        self.cookie_jar = None
        self.connector = None

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if self.request_hook:
            await self.request_hook(method, url, kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def close(self):
        self.closed = True
        if self.connector is not None:
            await self.connector.close()


class SessionFactory:
    def __init__(self, session):
        self.session = session
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.session.cookie_jar = kwargs["cookie_jar"]
        self.session.connector = kwargs["connector"]
        return self.session


async def make_client(responses, **kwargs):
    session = FakeSession(responses, kwargs.pop("request_hook", None))
    factory = SessionFactory(session)
    client = LofterClient(session_factory=factory, **kwargs)
    await client.initialize()
    return client, session, factory


def test_build_tag_search_body_uses_offset_in_param7_only():
    body = build_tag_search_body("demo", offset=20, limit=20)

    assert "c0-param1=number:0" in body
    assert "c0-param6=number:20" in body
    assert "c0-param7=number:20" in body
    assert "c0-param8=number:0" in body
    assert "c0-param1=number:20" not in body


@pytest.mark.asyncio
async def test_initialize_creates_one_anonymous_bounded_session():
    client, session, factory = await make_client([])
    try:
        await client.initialize()
        assert len(factory.calls) == 1
        call = factory.calls[0]
        assert isinstance(call["cookie_jar"], aiohttp.DummyCookieJar)
        assert "Cookie" not in call["headers"]
        assert call["connector"].limit == GLOBAL_HTTP_LIMIT
        assert call["connector"].limit_per_host == HOST_HTTP_LIMIT
    finally:
        await client.close()
    assert session.closed


@pytest.mark.asyncio
async def test_anonymous_and_credentialed_requests_isolate_cookie():
    client, session, _ = await make_client([FakeResponse(), FakeResponse()])
    client.update_cookie("demo=value")
    try:
        await client.get("https://www.lofter.com/public")
        await client.get("https://www.lofter.com/legacy", credentialed=True)
    finally:
        await client.close()

    anonymous = session.requests[0][2]["headers"]
    credentialed = session.requests[1][2]["headers"]
    assert "Cookie" not in anonymous
    assert credentialed["Cookie"] == "demo=value"


@pytest.mark.asyncio
async def test_cookie_update_only_affects_later_operations():
    entered = asyncio.Event()
    resume = asyncio.Event()

    async def hook(*_args):
        if not entered.is_set():
            entered.set()
            await resume.wait()

    client, session, _ = await make_client(
        [FakeResponse(), FakeResponse()], request_hook=hook
    )
    client.update_cookie("demo=old")
    first = asyncio.create_task(
        client.get("https://www.lofter.com/one", credentialed=True)
    )
    await entered.wait()
    client.update_cookie("demo=new")
    resume.set()
    await first
    await client.get("https://www.lofter.com/two", credentialed=True)
    await client.close()

    assert session.requests[0][2]["headers"]["Cookie"] == "demo=old"
    assert session.requests[1][2]["headers"]["Cookie"] == "demo=new"


@pytest.mark.asyncio
async def test_set_cookie_is_not_persisted():
    responses = [
        FakeResponse(headers={"Set-Cookie": "server=ignored"}),
        FakeResponse(),
    ]
    client, session, _ = await make_client(responses)
    try:
        await client.get("https://www.lofter.com/one")
        await client.get("https://www.lofter.com/two")
    finally:
        await client.close()

    assert len(session.cookie_jar) == 0
    assert "Cookie" not in session.requests[1][2]["headers"]


@pytest.mark.asyncio
async def test_credentialed_cross_origin_redirect_fails_closed():
    response = FakeResponse(
        status=302, headers={"Location": "https://example.com/next"}
    )
    client, session, _ = await make_client([response])
    client.update_cookie("demo=value")
    try:
        with pytest.raises(SourceHTTPError) as exc_info:
            await client.get(
                "https://www.lofter.com/legacy", credentialed=True
            )
    finally:
        await client.close()

    assert exc_info.value.status == 302
    assert len(session.requests) == 1
    assert response.released


@pytest.mark.asyncio
async def test_same_origin_redirect_keeps_request_cookie():
    responses = [
        FakeResponse(status=302, headers={"Location": "/next"}),
        FakeResponse(body=b"done"),
    ]
    client, session, _ = await make_client(responses)
    client.update_cookie("demo=value")
    try:
        result = await client.get(
            "https://www.lofter.com/start", credentialed=True
        )
    finally:
        await client.close()

    assert result == "done"
    assert session.requests[1][1] == "https://www.lofter.com/next"
    assert session.requests[1][2]["headers"]["Cookie"] == "demo=value"


@pytest.mark.asyncio
async def test_retry_after_and_backoff_run_between_attempts():
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    responses = [
        FakeResponse(status=429, headers={"Retry-After": "2"}),
        FakeResponse(status=500),
        FakeResponse(body=b"ok"),
    ]
    client, session, _ = await make_client(
        responses, sleep=sleep, jitter=lambda _attempt: 0.0
    )
    try:
        assert await client.get("https://www.lofter.com/retry") == "ok"
    finally:
        await client.close()

    assert len(session.requests) == MAX_ATTEMPTS
    assert sleeps == [2.0, 0.5]


@pytest.mark.asyncio
async def test_retry_backoff_does_not_hold_request_semaphore():
    sleeping = asyncio.Event()
    resume = asyncio.Event()

    async def sleep(_delay):
        sleeping.set()
        await resume.wait()

    responses = [
        FakeResponse(status=429, headers={"Retry-After": "1"}),
        FakeResponse(body=b"second"),
        FakeResponse(body=b"first"),
    ]
    client, _, _ = await make_client(responses, sleep=sleep)
    client._request_slots = asyncio.Semaphore(1)
    try:
        first = asyncio.create_task(client.get("https://www.lofter.com/first"))
        await sleeping.wait()
        second = asyncio.create_task(client.get("https://www.lofter.com/second"))
        assert await asyncio.wait_for(second, 0.2) == "second"
        resume.set()
        assert await first == "first"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_retry_exhaustion_is_typed_and_sanitized():
    responses = [FakeResponse(status=503) for _ in range(MAX_ATTEMPTS)]
    client, _, _ = await make_client(responses, sleep=AsyncNoop())
    try:
        with pytest.raises(SourceRetryExhaustedError) as exc_info:
            await client.get("https://www.lofter.com/retry")
    finally:
        await client.close()
    assert exc_info.value.attempts == MAX_ATTEMPTS
    assert "response" not in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_timeout_exhaustion_has_distinct_type():
    responses = [asyncio.TimeoutError() for _ in range(MAX_ATTEMPTS)]
    client, _, _ = await make_client(responses, sleep=AsyncNoop())
    try:
        with pytest.raises(SourceTimeoutError):
            await client.get("https://www.lofter.com/timeout")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_non_retryable_http_error_stops_after_one_attempt():
    client, session, _ = await make_client([FakeResponse(status=404)])
    try:
        with pytest.raises(SourceHTTPError) as exc_info:
            await client.get("https://www.lofter.com/missing")
    finally:
        await client.close()
    assert exc_info.value.status == 404
    assert len(session.requests) == 1


class AsyncNoop:
    async def __call__(self, _delay):
        return None


@pytest.mark.asyncio
async def test_body_limit_uses_decompressed_stream_size():
    response = FakeResponse(body=b"x" * (MAX_BODY_BYTES + 1))
    client, _, _ = await make_client([response])
    try:
        with pytest.raises(SourceLimitError) as exc_info:
            await client.get("https://www.lofter.com/large")
    finally:
        await client.close()
    assert exc_info.value.resource == "body"
    assert exc_info.value.limit == MAX_BODY_BYTES
    assert response.released


@pytest.mark.asyncio
async def test_request_json_types_external_decoder_failures(monkeypatch):
    client, _, _ = await make_client([
        FakeResponse(body=b"[]"),
        FakeResponse(body=b"[]"),
    ])
    failures = iter([ValueError("payload=private"), RecursionError("private")])
    monkeypatch.setattr(
        client_module.json, "loads", lambda _text: (_ for _ in ()).throw(next(failures))
    )
    try:
        for _ in range(2):
            with pytest.raises(SourceSchemaError) as exc_info:
                await client.request_json("GET", "https://api.lofter.com/data")
            assert exc_info.value.location == "json"
            assert "private" not in str(exc_info.value)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_request_json_does_not_hide_decoder_programming_error(monkeypatch):
    client, _, _ = await make_client([FakeResponse(body=b"[]")])
    monkeypatch.setattr(
        client_module.json, "loads", lambda _text: (_ for _ in ()).throw(RuntimeError("bug"))
    )
    try:
        with pytest.raises(RuntimeError, match="bug"):
            await client.request_json("GET", "https://api.lofter.com/data")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_request_json_deep_payload_is_typed_and_redacted():
    payload = b"[" * 10_000 + b"0" + b"]" * 10_000
    client, _, _ = await make_client([FakeResponse(body=payload)])
    try:
        with pytest.raises(SourceSchemaError) as exc_info:
            await client.request_json("GET", "https://api.lofter.com/data")
    finally:
        await client.close()
    assert exc_info.value.location == "json"
    assert "[" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_close_waits_for_inflight_rejects_new_and_is_idempotent():
    entered = asyncio.Event()
    resume = asyncio.Event()

    async def hook(*_args):
        entered.set()
        await resume.wait()

    client, session, _ = await make_client(
        [FakeResponse()], request_hook=hook
    )
    request = asyncio.create_task(client.get("https://www.lofter.com/slow"))
    await entered.wait()
    closing = asyncio.create_task(client.close())
    await asyncio.sleep(0)
    assert not closing.done()
    with pytest.raises(SourceClosingError):
        await client.get("https://www.lofter.com/late")
    resume.set()
    await request
    await closing
    await client.close()
    assert session.closed


@pytest.mark.asyncio
async def test_search_tag_is_explicit_credentialed_fallback():
    client, session, _ = await make_client([FakeResponse(body=b"dwr")])
    client.update_cookie("demo=value")
    try:
        assert await client.search_tag("demo", offset=20, limit=10) == "dwr"
    finally:
        await client.close()
    method, url, kwargs = session.requests[0]
    assert (method, url) == ("POST", DWR_SEARCH_URL)
    assert kwargs["headers"]["Cookie"] == "demo=value"
    assert "c0-param7=number:20" in kwargs["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/post/a_b",
        "http://www.lofter.com/post/a_b",
        "https://user:pass@www.lofter.com/post/a_b",
        "https://evillofter.com/post/a_b",
        "https://www.lofter.com:0/post/a_b",
        "https://www.lofter.com:444/post/a_b",
    ],
)
async def test_credentialed_initial_target_rejected_before_request(url):
    client, session, _ = await make_client([])
    try:
        with pytest.raises(SourceSchemaError):
            await client.get(url, credentialed=True)
    finally:
        await client.close()
    assert session.requests == []


@pytest.mark.asyncio
async def test_credentialed_explicit_default_port_is_same_origin():
    responses = [
        FakeResponse(status=302, headers={"Location": "https://www.lofter.com:443/next"}),
        FakeResponse(body=b"ok"),
    ]
    client, session, _ = await make_client(responses)
    client.update_cookie("demo=value")
    try:
        assert await client.get("https://www.lofter.com/start", credentialed=True) == "ok"
    finally:
        await client.close()
    assert len(session.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        "https://www.lofter.com:444/next",
        "https://api.lofter.com/next",
        "http://www.lofter.com/next",
    ],
)
async def test_credentialed_redirect_rejects_effective_origin_change(location):
    client, session, _ = await make_client([
        FakeResponse(status=302, headers={"Location": location})
    ])
    try:
        with pytest.raises(SourceHTTPError):
            await client.get("https://www.lofter.com/start", credentialed=True)
    finally:
        await client.close()
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_request_url_limit_uses_strict_utf8_bytes():
    prefix = "https://www.lofter.com/"
    exact = prefix + "a" * (8192 - len(prefix.encode()))
    client, session, _ = await make_client([FakeResponse()])
    try:
        assert await client.get(exact) == "ok"
        with pytest.raises(SourceLimitError):
            await client.get(exact + "a")
        with pytest.raises(SourceLimitError):
            await client.get(prefix + "界" * 2730)
        with pytest.raises(SourceSchemaError):
            await client.get(prefix + "\ud800")
    finally:
        await client.close()
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_cancelled_close_still_finishes_session_cleanup():
    entered = asyncio.Event()
    resume = asyncio.Event()

    async def hook(*_args):
        entered.set()
        await resume.wait()

    client, session, _ = await make_client([FakeResponse()], request_hook=hook)
    request = asyncio.create_task(client.get("https://www.lofter.com/slow"))
    await entered.wait()
    closing = asyncio.create_task(client.close())
    await asyncio.sleep(0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    resume.set()
    await request
    await asyncio.wait_for(client.close(), 1)
    assert session.closed


@pytest.mark.asyncio
async def test_concurrent_close_reuses_single_cleanup():
    client, session, _ = await make_client([])
    calls = 0
    original = session.close

    async def counted_close():
        nonlocal calls
        calls += 1
        await original()

    session.close = counted_close
    await asyncio.gather(client.close(), client.close(), client.close())
    assert calls == 1


@pytest.mark.asyncio
async def test_close_failure_retains_session_and_can_retry():
    client, session, _ = await make_client([])
    calls = 0
    original = session.close

    async def flaky_close():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("close failed")
        await original()

    session.close = flaky_close
    with pytest.raises(RuntimeError):
        await client.close()
    assert client._session is session
    await client.close()
    assert calls == 2 and session.closed
