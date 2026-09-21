import core.client as client
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer


def test_build_tag_search_body_uses_offset_in_param7_only():
    assert hasattr(client, "build_tag_search_body")

    body = client.build_tag_search_body("原神", offset=20, limit=20)

    assert "c0-param1=number:0" in body
    assert "c0-param6=number:20" in body
    assert "c0-param7=number:20" in body
    assert "c0-param8=number:0" in body
    assert "c0-param1=number:20" not in body
    assert "c0-param7=number:0" not in body


def test_search_body_encodes_tag_and_preserves_raw_timestamp():
    body = client.build_tag_search_body("A\n标签", offset=40, before=1720000000123)
    assert "c0-param0=string:A%0A%E6%A0%87%E7%AD%BE\n" in body
    assert "c0-param8=number:1720000000123" in body


@pytest.mark.asyncio
async def test_client_reuses_connection_session_updates_cookie_and_closes():
    cookies = []

    async def handler(request):
        cookies.append(request.headers.get("Cookie"))
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", handler)
    async with TestServer(app) as server:
        async with client.LofterClient("session=first") as instance:
            assert await instance.get(str(server.make_url("/"))) == "ok"
            session = instance._session
            instance.update_cookie("session=second")
            assert await instance.get(str(server.make_url("/"))) == "ok"
            assert instance._session is session
        assert session.closed
    assert cookies == ["session=first", "session=second"]


@pytest.mark.asyncio
async def test_temporary_http_failure_is_retried():
    requests = []

    async def handler(request):
        requests.append(request)
        return web.Response(status=503 if len(requests) == 1 else 200, text="ok")

    app = web.Application()
    app.router.add_get("/", handler)
    async with TestServer(app) as server:
        async with client.LofterClient() as instance:
            assert await instance.get(str(server.make_url("/"))) == "ok"
    assert len(requests) == 2
