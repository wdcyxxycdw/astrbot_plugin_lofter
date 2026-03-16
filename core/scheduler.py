import asyncio
from typing import Callable, Awaitable

from astrbot.api import logger

from .client import LofterClient
from .parser import parse_blog_posts
from .storage import Subscription, SubscriptionStorage

SendFunc = Callable[[str, list], Awaitable[None]]

TAG_URL = "https://www.lofter.com/tag/{tag}"
BLOG_URL = "https://{username}.lofter.com"


def _sub_url(sub: Subscription) -> str:
    if sub.type == "tag":
        return TAG_URL.format(tag=sub.target)
    return BLOG_URL.format(username=sub.target)


async def _check_subscription(
    sub: Subscription,
    client: LofterClient,
    storage: SubscriptionStorage,
    send_func: SendFunc,
):
    url = _sub_url(sub)
    try:
        html = await client.get(url)
        posts = await parse_blog_posts(html)
    except Exception as e:
        logger.error("轮询订阅 %s/%s 失败: %s", sub.type, sub.target, e)
        return

    if not posts:
        return

    latest_id = posts[0].post_id
    if latest_id == sub.last_post_id:
        return

    # 找到上次已推送位置，仅推送新帖
    new_posts = []
    for p in posts:
        if p.post_id == sub.last_post_id:
            break
        new_posts.append(p)

    storage.update_last_post_id(sub.session_id, sub.type, sub.target, latest_id)

    for post in reversed(new_posts):
        label = "标签" if sub.type == "tag" else "博主"
        header = f"【{label}「{sub.target}」有新内容】\n{post.title or post.url}"
        await send_func(sub.session_id, header, post.images[:1])


class SubscriptionScheduler:
    def __init__(
        self,
        storage: SubscriptionStorage,
        client: LofterClient,
        send_func: SendFunc,
        interval_minutes: int = 30,
    ):
        self._storage = storage
        self._client = client
        self._send_func = send_func
        self._interval = interval_minutes * 60
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._loop())
        logger.info("Lofter 订阅轮询已启动，间隔 %d 分钟", self._interval // 60)

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Lofter 订阅轮询已停止")

    async def _loop(self):
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_all()

    async def _poll_all(self):
        subs = self._storage.all()
        tasks = [
            _check_subscription(s, self._client, self._storage, self._send_func)
            for s in subs
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
