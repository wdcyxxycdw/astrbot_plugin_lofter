import asyncio
from typing import Callable, Awaitable

from astrbot.api import logger

from .client import LofterClient
from .db import LofterDB
from .dwr_parser import parse_dwr_response
from .filter import FilterRule, apply_filter
from .parser import Post, parse_blog_posts, parse_post_page
from .storage import Subscription, SubscriptionStorage

SendFunc = Callable[[str, str, list], Awaitable[None]]

BLOG_URL = "https://{username}.lofter.com"


async def fetch_tag_posts(search_tags: list[str], client: LofterClient) -> list[Post]:
    seen_ids: set[str] = set()
    result: list[Post] = []
    for tag in search_tags:
        raw = await client.search_tag(tag, limit=20)
        posts = await parse_dwr_response(raw)
        for p in posts:
            if p.post_id not in seen_ids:
                seen_ids.add(p.post_id)
                result.append(p)
    return result


async def fetch_blog_posts(sub: Subscription, client: LofterClient) -> list[Post]:
    html = await client.get(BLOG_URL.format(username=sub.target))
    return await parse_blog_posts(html)


def _build_tag_rule(subs: list[Subscription]) -> FilterRule:
    search_tags = [s.target for s in subs if s.role == "subscribe"]
    exclude_tags = [s.target for s in subs if s.role == "exclude"]
    return FilterRule(search_tags=search_tags, exclude_tags=exclude_tags)


def _pick_display_tag(post: Post, search_tags: list[str]) -> str:
    lower_set = {s.lower() for s in search_tags}
    hit = next((t for t in post.tags if t.lower() in lower_set), None)
    return hit or (search_tags[0] if search_tags else "标签")


async def _push_tag_posts(session_id: str, posts: list[Post], rule: FilterRule, send_func: SendFunc):
    for post in reversed(posts[:5]):
        display_tag = _pick_display_tag(post, rule.search_tags)
        title = post.title or "(无标题)"
        tags = f"#{' #'.join(post.tags)}" if post.tags else ""
        lines = [f"【标签「{display_tag}」有新内容】", f"▸ {title}"]
        if post.author:
            lines.append(f"作者：{post.author}")
        if tags:
            lines.append(tags)
        if post.summary:
            lines.append(post.summary)
        lines.append(post.url)
        await send_func(session_id, "\n".join(lines), post.images)


async def _push_blog_post(session_id: str, post: Post, username: str, send_func: SendFunc):
    title = post.title or "(无标题)"
    tags = f"#{' #'.join(post.tags)}" if post.tags else ""
    lines = [f"【博主「{username}」有新内容】", f"▸ {title}"]
    if post.author:
        lines.append(f"作者：{post.author}")
    if tags:
        lines.append(tags)
    if post.summary:
        lines.append(post.summary)
    lines.append(post.url)
    await send_func(session_id, "\n".join(lines), post.images)


async def _enrich_blog_posts(posts: list[Post], client: LofterClient) -> list[Post]:
    enriched = []
    for post in posts:
        try:
            html = await client.get(post.url)
            rich = await parse_post_page(html, post.url)
            rich.post_id = post.post_id
            enriched.append(rich)
        except Exception as e:
            logger.warning("获取博主帖子详情失败 %s: %s", post.url, e)
            enriched.append(post)
    return enriched


async def _push_posts(posts: list[Post], sub: Subscription, send_func: SendFunc):
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


async def _check_tag_session(
    session_id: str,
    subs: list[Subscription],
    client: LofterClient,
    db: LofterDB,
    send_func: SendFunc,
):
    rule = _build_tag_rule(subs)
    if not rule.search_tags:
        return

    try:
        posts = await fetch_tag_posts(rule.search_tags, client)
    except Exception as e:
        logger.error("轮询标签 session=%s 失败: %s", session_id, e)
        return

    posts = apply_filter(posts, rule)
    if not posts:
        return

    all_ids = [p.post_id for p in posts]
    unseen_ids = await db.filter_unseen_session(session_id, "tag", all_ids)
    if not unseen_ids:
        return

    is_cold = await db.seen_count(session_id, "tag") == 0
    await db.mark_seen_session(session_id, "tag", unseen_ids)
    if is_cold:
        return

    unseen_set = set(unseen_ids)
    new_posts = [p for p in posts if p.post_id in unseen_set]
    actually_new_ids = await db.filter_unsent(session_id, [p.post_id for p in new_posts])
    if not actually_new_ids:
        return

    actually_new_set = set(actually_new_ids)
    to_push = [p for p in new_posts if p.post_id in actually_new_set]
    await _push_tag_posts(session_id, to_push, rule, send_func)
    await db.mark_sent(session_id, actually_new_ids)


async def _check_blog_sub(
    sub: Subscription,
    client: LofterClient,
    db: LofterDB,
    send_func: SendFunc,
):
    try:
        posts = await fetch_blog_posts(sub, client)
    except Exception as e:
        logger.error("轮询博主 %s 失败: %s", sub.target, e)
        return

    if not posts:
        return

    all_ids = [p.post_id for p in posts]
    unseen_ids = await db.filter_unseen_session(sub.session_id, "blog", all_ids)
    if not unseen_ids:
        return

    is_cold = await db.seen_count(sub.session_id, "blog") == 0
    await db.mark_seen_session(sub.session_id, "blog", unseen_ids)
    if is_cold:
        return

    unseen_set = set(unseen_ids)
    new_posts = [p for p in posts if p.post_id in unseen_set]
    actually_new_ids = await db.filter_unsent(sub.session_id, [p.post_id for p in new_posts])
    if not actually_new_ids:
        return

    actually_new_set = set(actually_new_ids)
    to_push = [p for p in new_posts if p.post_id in actually_new_set]
    to_push = await _enrich_blog_posts(to_push, client)
    for post in reversed(to_push[:5]):
        await _push_blog_post(sub.session_id, post, sub.target, send_func)
    await db.mark_sent(sub.session_id, actually_new_ids)


async def _poll_tag_session(session_id: str, tag_subs: list[Subscription], client: LofterClient, db: LofterDB, send_func: SendFunc):
    await _check_tag_session(session_id, tag_subs, client, db, send_func)


async def _poll_blog_session(session_id: str, blog_subs: list[Subscription], client: LofterClient, db: LofterDB, send_func: SendFunc):
    for sub in blog_subs:
        await _check_blog_sub(sub, client, db, send_func)


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

    async def _poll_all(self):
        subs = await self._storage.all()
        by_session: dict[str, dict[str, list[Subscription]]] = {}
        for s in subs:
            by_session.setdefault(s.session_id, {"tag": [], "blog": []})
            by_session[s.session_id][s.type].append(s)

        tasks = []
        for session_id, typed in by_session.items():
            if typed["tag"]:
                tasks.append(_poll_tag_session(session_id, typed["tag"], self._client, self._db, self._send_func))
            for sub in typed["blog"]:
                tasks.append(_check_blog_sub(sub, self._client, self._db, self._send_func))

        await asyncio.gather(*tasks, return_exceptions=True)
