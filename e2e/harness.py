import asyncio
import contextlib
import json
import time
import base64
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestServer

API_URLS = ("TAG_URL", "BLOG_URL", "DETAIL_URL")


def envelope(response: dict) -> dict:
    return {"meta": {"status": 200, "msg": ""}, "response": response}


def form_fields(text: str) -> dict:
    from urllib.parse import unquote

    return {k: unquote(v) for k, v in (pair.split("=", 1) for pair in text.split("&") if "=" in pair)}


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
        self.blog_pages = {}
        self.post_pages = {}
        self.api_requests = []
        self.image_requests = 0
        self.video_body = b"\x00fake-mp4" * 64
        self.video_requests = 0

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
        app.router.add_post("/tag", self.tag)
        app.router.add_post("/blog", self.blog)
        app.router.add_post("/detail", self.detail)
        app.router.add_get("/image.png", self.image)
        app.router.add_get("/video.mp4", self.video)
        self.http = await self.stack.enter_async_context(TestServer(app))
        import lofter.client as client_module
        self.client_module = client_module
        self.original_api_urls = {name: getattr(client_module, name) for name in API_URLS}
        client_module.TAG_URL = str(self.http.make_url("/tag"))
        client_module.BLOG_URL = str(self.http.make_url("/blog"))
        client_module.DETAIL_URL = str(self.http.make_url("/detail"))
        websocket = await self.stack.enter_async_context(self.adapter.bot.server_app.test_client().websocket(
            "/ws", headers={"X-Self-ID": "30001", "X-Client-Role": "Universal"},
        ))
        self.peer = OneBotPeer(websocket)
        self.receiver = asyncio.create_task(self.peer.receive())

    async def _read(self, request) -> dict:
        fields = form_fields(await request.text())
        self.api_requests.append(fields)
        return fields

    def _reply(self, page, key: str, offset: int = 0):
        if isinstance(page, str):
            return web.Response(text=page, content_type="text/html")
        return web.json_response(envelope({key: page, "offset": offset + len(page)}))

    async def tag(self, request):
        fields = await self._read(request)
        offset = int(fields["offset"])
        return self._reply(self.pages.get((fields["tag"], offset), []), "items", offset)

    async def blog(self, request):
        fields = await self._read(request)
        username = fields["blogdomain"].removesuffix(".lofter.com")
        return self._reply(self.blog_pages.get(username, []), "posts")

    async def detail(self, request):
        fields = await self._read(request)
        permalink = f"{int(fields['blogId']):x}_{int(fields['postid']):x}"
        return self._reply(self.post_pages.get(permalink, []), "posts")

    async def video(self, request):
        self.video_requests += 1
        return web.Response(body=self.video_body, content_type="video/mp4")

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
        for name, url in getattr(self, "original_api_urls", {}).items():
            setattr(self.client_module, name, url)
        if hasattr(self, "receiver"):
            self.receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.receiver
        await self.stack.aclose()
        from astrbot.core import db_helper
        await db_helper.engine.dispose()
