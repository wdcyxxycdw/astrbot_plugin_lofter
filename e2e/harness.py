import asyncio
import contextlib
import json
import time
import base64
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestServer


class OneBotPeer:
    def __init__(self, websocket):
        self.websocket = websocket
        self.requests = []
        self.fail_next_send = False
        self.send_count = 0
        self.fail_send_number = None

    async def receive(self):
        while True:
            request = json.loads(await self.websocket.receive())
            self.requests.append(request)
            is_send = request["action"].startswith("send_")
            self.send_count += int(is_send)
            failed = is_send and (self.fail_next_send or self.send_count == self.fail_send_number)
            if failed:
                self.fail_next_send = False
            await self.websocket.send(json.dumps({
                "status": "failed" if failed else "ok",
                "retcode": 100 if failed else 0,
                "data": {"message_id": len(self.requests)},
                "echo": request["echo"],
            }))

    async def message(self, text, user_id=10001, group_id=20001):
        event = {
            "time": int(time.time()), "self_id": 30001, "post_type": "message",
            "message_type": "group" if group_id else "private", "sub_type": "normal",
            "message_id": time.time_ns(), "user_id": user_id, "raw_message": text,
            "message": [{"type": "text", "data": {"text": text}}],
            "sender": {"user_id": user_id, "nickname": "E2E", "role": "member"},
            "font": 0,
        }
        if group_id:
            event["group_id"] = group_id
        await self.websocket.send(json.dumps(event))


class Runtime:
    def __init__(self, repo: Path, root: Path):
        self.repo = repo
        self.root = root
        self.stack = contextlib.AsyncExitStack()
        self.plugin = None
        self.pages = {}
        self.dwr_requests = []
        self.html_pages = {}
        self.image_requests = 0

    async def start(self):
        from astrbot.core import astrbot_config, db_helper, sp
        from astrbot.core.astrbot_config_mgr import AstrBotConfigManager
        from astrbot.core.pipeline.context import PipelineContext
        from astrbot.core.pipeline.scheduler import PipelineScheduler
        from astrbot.core.platform.manager import PlatformManager
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
        from astrbot.core.star.context import Context
        from astrbot.core.star.star_manager import PluginManager
        from astrbot.core.umop_config_router import UmopConfigRouter
        from astrbot.core.persona_mgr import PersonaManager
        from astrbot.core.provider.manager import ProviderManager
        from astrbot.core.conversation_mgr import ConversationManager

        await db_helper.initialize()
        config = astrbot_config
        config["provider_settings"]["enable"] = False
        config["admins_id"] = ["10001"]
        config["wake_prefix"] = ["/"]
        config["platform_settings"]["enable_id_white_list"] = False
        config["platform_settings"]["reply_with_mention"] = False
        config["platform_settings"]["reply_with_quote"] = False
        config["platform_settings"]["segmented_reply"]["enable"] = False
        self.queue = asyncio.Queue()
        platforms = PlatformManager(config, self.queue)
        self.adapter = AiocqhttpAdapter({
            "id": "lofter-e2e", "type": "aiocqhttp", "enable": True,
            "ws_reverse_host": "127.0.0.1", "ws_reverse_port": 0,
        }, config["platform_settings"], self.queue)
        platforms.platform_insts.append(self.adapter)
        router = UmopConfigRouter(sp)
        await router.initialize()
        configs = AstrBotConfigManager(config, router, sp)
        await configs.initialize()
        personas = PersonaManager(db_helper, configs)
        providers = ProviderManager(configs, db_helper, personas)
        conversations = ConversationManager(db_helper)
        context = Context(self.queue, config, db_helper, providers, platforms, conversations, None, personas, configs, None, None)
        plugins = PluginManager(context, config)
        plugin_dir = self.root / "data" / "plugins" / "astrbot_plugin_lofter"
        plugin_dir.parent.mkdir(parents=True, exist_ok=True)
        plugin_dir.symlink_to(self.repo, target_is_directory=True)
        (self.root / "data" / "config").mkdir(exist_ok=True)
        ok, error = await plugins.load(specified_dir_name="astrbot_plugin_lofter")
        assert ok, error
        self.plugin = context.get_registered_star("astrbot_plugin_lofter").star_cls
        await self.plugin._scheduler.stop()
        self.pipeline = PipelineScheduler(PipelineContext(config, plugins, "default"))
        await self.pipeline.initialize()
        app = web.Application()
        app.router.add_post("/dwr", self.dwr)
        app.router.add_get("/image.png", self.image)
        self.http = await self.stack.enter_async_context(TestServer(app))
        self.client_module = __import__(self.plugin.__module__.rsplit(".", 1)[0] + ".core.client", fromlist=["client"])
        self.original_dwr_url = self.client_module.DWR_SEARCH_URL
        self.client_module.DWR_SEARCH_URL = str(self.http.make_url("/dwr"))
        from http_fixture import post_server
        self.plugin._client._session = await post_server(self.stack, self.root, self.post_page)
        websocket = await self.stack.enter_async_context(self.adapter.bot.server_app.test_client().websocket(
            "/ws", headers={"X-Self-ID": "30001", "X-Client-Role": "Universal"},
        ))
        self.peer = OneBotPeer(websocket)
        self.receiver = asyncio.create_task(self.peer.receive())

    async def dwr(self, request):
        from urllib.parse import unquote
        fields = dict(line.split("=", 1) for line in (await request.text()).splitlines() if "=" in line)
        self.dwr_requests.append(fields)
        tag = unquote(fields["c0-param0"].removeprefix("string:"))
        offset = int(fields["c0-param7"].removeprefix("number:"))
        body = self.pages.get((tag, offset), "dwr.engine._remoteHandleCallback('0','0',[]);")
        return web.Response(text=body, content_type="text/javascript")

    async def post_page(self, request):
        return web.Response(text=self.html_pages.get(request.match_info["post_id"], "<html>请登录</html>"), content_type="text/html")

    async def image(self, request):
        self.image_requests += 1
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7x8AAAAASUVORK5CYII="
        return web.Response(body=base64.b64decode(png), content_type="image/png")

    async def message(self, text, timeout=20, **kwargs):
        start = len(self.peer.requests)
        await self.peer.message(text, **kwargs)
        event = await asyncio.wait_for(self.queue.get(), 10)
        await asyncio.wait_for(self.pipeline.execute(event), timeout)
        return event, self.peer.requests[start:]

    async def close(self):
        if self.plugin:
            await self.plugin.terminate()
        if hasattr(self, "receiver"):
            self.receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.receiver
        await self.stack.aclose()
        from astrbot.core import db_helper
        await db_helper.engine.dispose()
