import asyncio
from typing import Callable, Awaitable

from astrbot.api import logger

from .client import LofterClient
from .db import LofterDB
from .dwr_parser import parse_dwr_response
from .parser import parse_blog_posts
from .storage import Subscription, SubscriptionStorage

SendFunc = Callable[[str, str, list], Awaitable[None]]

BLOG_URL = "https://{username}.lofter.com"


async def fetch_posts(sub: Subscription, client: LofterClient):
    if sub.type == "tag":
        raw = await client.search_tag(sub.target, limit=20)
        return await parse_dwr_response(raw)
    else:
        html = await client.get(BLOG_URL.format(username=sub.target))
        return await parse_blog_posts(html)


async def _push_posts(posts, sub: Subscription, send_func: SendFunc):
    label = "标签" if sub.type == "tag" else "博主"
    for post in reversed(posts[:5]):
        title = post.title or "(无标题)"
        tags = f"#{' #'.join(post.tags)}" if post.tags else ""
        lines = [f"【{label}「{sub.target}」有新内容】", f"▸ {title}"]
        if post.author:
            lines.append(f"作者：{post.author}")
        if tags:
            lines.append(tags)
        if post.summary:
            lines.append(post.summary)
        lines.append(post.url)
        await send_func(sub.session_id, "\n".join(lines), post.images)


async def _check_subscription(
    sub: Subscription,
    client: LofterClient,
    db: LofterDB,
    send_func: SendFunc,
):
    try:
        posts = await fetch_posts(sub, client)
    except Exception as e:
        logger.error("轮询订阅 %s/%s 失败: %s", sub.type, sub.target, e)
        return

    if not posts:
        return

    sub_id = await db.get_subscription_id(sub.session_id, sub.type, sub.target)
    if sub_id is None:
        return

    all_ids = [p.post_id for p in posts]
    unseen_ids = await db.filter_unseen(sub_id, all_ids)

    if not unseen_ids:
        return

    is_cold_start = len(unseen_ids) == len(all_ids)
    await db.mark_seen(sub_id, unseen_ids)

    if is_cold_start:
        return

    unseen_set = set(unseen_ids)
    new_posts = [p for p in posts if p.post_id in unseen_set]

    actually_new_ids = await db.filter_unsent(sub.session_id, [p.post_id for p in new_posts])
    if not actually_new_ids:
        return

    actually_new_set = set(actually_new_ids)
    actually_new = [p for p in new_posts if p.post_id in actually_new_set]
    await _push_posts(actually_new, sub, send_func)
    await db.mark_sent(sub.session_id, actually_new_ids)


class SubscriptionScheduler:
    def __init__(
        self,
        storage: SubscriptionStorage,
        client: LofterClient,
        db: LofterDB,
        send_func: SendFunc,
        interval_minutes: int = 30,
    ):
        self._storage = storage
        self._client = client
        self._db = db
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

    async def _poll_session(self, subs: list[Subscription]):
        for sub in subs:
            await _check_subscription(sub, self._client, self._db, self._send_func)

    async def _poll_all(self):
        subs = await self._storage.all()
        by_session: dict[str, list[Subscription]] = {}
        for s in subs:
            by_session.setdefault(s.session_id, []).append(s)
        await asyncio.gather(
            *[self._poll_session(session_subs) for session_subs in by_session.values()],
            return_exceptions=True,
        )
