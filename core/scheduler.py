import asyncio
from typing import Callable, Awaitable

from astrbot.api import logger

from .author_block import AuthorBlockStorage, filter_blocked_posts
from .client import LofterClient
from .db import LofterDB
from .dwr_parser import parse_dwr_response
from .filter import FilterRule, apply_filter
from .formatter import format_post
from .parser import Post, parse_blog_posts, parse_post_page
from .storage import Subscription, SubscriptionStorage

SendFunc = Callable[[str, str, list], Awaitable[None]]

BLOG_URL = "https://{username}.lofter.com"
MAX_PUSH_POSTS = 5


async def fetch_tag_posts(search_tags: list[str], client: LofterClient, *, db: LofterDB | None = None, session_id: str = "") -> list[Post]:
    seen_ids: set[str] = set()
    result: list[Post] = []
    for tag in search_tags:
        try:
            posts = await _fetch_tag_pages(tag, client, db, session_id)
        except Exception:
            if db is None:
                raise
            logger.exception("Lofter 标签「%s」抓取中断，保留队列和翻页位置", tag)
            continue
        for p in posts:
            if p.post_id not in seen_ids:
                seen_ids.add(p.post_id)
                result.append(p)
    return result


async def _fetch_tag_pages(tag: str, client: LofterClient, db: LofterDB | None, session_id: str) -> list[Post]:
    offset, before = await db.tag_scan_cursor(session_id, tag) if db else (0, 0)
    result = []
    page_ids: set[str] = set()
    warm = db is not None and await db.seen_count(session_id, "tag") > 0
    while True:
        raw = await client.search_tag(tag, limit=20, offset=offset, before=before)
        posts = await parse_dwr_response(raw)
        ids = {post.post_id for post in posts}
        if not ids:
            break
        if not ids - page_ids:
            raise RuntimeError(f"标签「{tag}」返回重复页，已保留翻页位置")
        page_ids.update(ids)
        result.extend(posts)
        if not warm:
            break
        unseen = set(await db.filter_unseen_session(session_id, "tag", list(ids)))
        pending = {post.post_id for post in await db.pending_posts(session_id, "tag", "")}
        if not unseen - pending:
            break
        timestamps = [post.publish_time_ms for post in posts if post.publish_time_ms > 0]
        if timestamps:
            before = min(timestamps)
        offset += 20
        await db.save_tag_page(session_id, tag, [post for post in posts if post.post_id in unseen], offset, before)
    if db:
        await db.clear_tag_scan_cursor(session_id, tag)
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
    for post in reversed(posts[:MAX_PUSH_POSTS]):
        display_tag = _pick_display_tag(post, rule.search_tags)
        header = f"【标签「{display_tag}」有新内容】"
        text = format_post(post, header=header)
        await send_func(session_id, text, post.images)


async def _push_blog_post(session_id: str, post: Post, username: str, send_func: SendFunc):
    header = f"【博主「{username}」有新内容】"
    text = format_post(post, header=header)
    await send_func(session_id, text, post.images)


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


async def _check_tag_session(
    session_id: str,
    subs: list[Subscription],
    client: LofterClient,
    db: LofterDB,
    send_func: SendFunc,
    block_storage: AuthorBlockStorage,
):
    rule = _build_tag_rule(subs)
    if not rule.search_tags:
        return

    try:
        posts = await fetch_tag_posts(rule.search_tags, client, db=db, session_id=session_id)
    except Exception as e:
        logger.error("轮询标签 session=%s 失败: %s", session_id, e)
        posts = []

    pending = await db.pending_posts(session_id, "tag", "")
    posts = list({post.post_id: post for post in pending + posts}.values())
    original_ids = {post.post_id for post in posts}
    posts = apply_filter(posts, rule)
    excluded_ids = list(original_ids - {post.post_id for post in posts})
    await db.discard_pending(session_id, "tag", excluded_ids)
    if not posts:
        return

    all_ids = [p.post_id for p in posts]
    unseen_ids = await db.filter_unseen_session(session_id, "tag", all_ids)
    if not unseen_ids:
        return

    is_cold = await db.seen_count(session_id, "tag") == 0
    if is_cold:
        await db.mark_seen_session(session_id, "tag", unseen_ids)
        return

    unseen_set = set(unseen_ids)
    new_posts = [p for p in posts if p.post_id in unseen_set]
    await db.enqueue_posts(session_id, "tag", "", new_posts)
    blocks = await block_storage.list_by_session(session_id)
    visible_posts, blocked_posts = filter_blocked_posts(new_posts, blocks)
    blocked_ids = [p.post_id for p in blocked_posts]
    await db.discard_pending(session_id, "tag", blocked_ids)
    if not visible_posts:
        await db.mark_seen_session(session_id, "tag", blocked_ids)
        return

    visible_ids = [p.post_id for p in visible_posts]
    actually_new_ids = await db.filter_unsent(session_id, visible_ids)
    actually_new_set = set(actually_new_ids)
    already_sent_ids = [pid for pid in visible_ids if pid not in actually_new_set]
    await db.discard_pending(session_id, "tag", already_sent_ids)
    if not actually_new_ids:
        await db.mark_seen_session(session_id, "tag", blocked_ids + already_sent_ids)
        return

    to_push = [p for p in visible_posts if p.post_id in actually_new_set][:MAX_PUSH_POSTS]
    await db.mark_seen_session(session_id, "tag", blocked_ids + already_sent_ids)
    for post in reversed(to_push):
        await _push_tag_posts(session_id, [post], rule, send_func)
        await db.mark_delivered(session_id, "tag", post.post_id)


async def _check_blog_sub(
    sub: Subscription,
    client: LofterClient,
    db: LofterDB,
    send_func: SendFunc,
    block_storage: AuthorBlockStorage,
):
    try:
        posts = await fetch_blog_posts(sub, client)
    except Exception as e:
        logger.error("轮询博主 %s 失败: %s", sub.target, e)
        posts = []

    pending = await db.pending_posts(sub.session_id, "blog", sub.target)
    posts = list({post.post_id: post for post in pending + posts}.values())

    if not posts:
        return

    all_ids = [p.post_id for p in posts]
    unseen_ids = await db.filter_unseen_session(sub.session_id, "blog", all_ids)
    if not unseen_ids:
        return

    is_cold = await db.seen_count(sub.session_id, "blog") == 0
    if is_cold:
        await db.mark_seen_session(sub.session_id, "blog", unseen_ids)
        return

    unseen_set = set(unseen_ids)
    new_posts = [p for p in posts if p.post_id in unseen_set]
    await db.enqueue_posts(sub.session_id, "blog", sub.target, new_posts)
    blocks = await block_storage.list_by_session(sub.session_id)
    visible_posts, blocked_before_enrich = filter_blocked_posts(new_posts, blocks)
    blocked_before_ids = [p.post_id for p in blocked_before_enrich]
    await db.discard_pending(sub.session_id, "blog", blocked_before_ids)
    if not visible_posts:
        await db.mark_seen_session(sub.session_id, "blog", blocked_before_ids)
        return

    visible_ids = [p.post_id for p in visible_posts]
    actually_new_ids = await db.filter_unsent(sub.session_id, visible_ids)
    actually_new_set = set(actually_new_ids)
    already_sent_ids = [pid for pid in visible_ids if pid not in actually_new_set]
    await db.discard_pending(sub.session_id, "blog", already_sent_ids)
    if not actually_new_ids:
        await db.mark_seen_session(sub.session_id, "blog", blocked_before_ids + already_sent_ids)
        return

    push_candidates = [p for p in visible_posts if p.post_id in actually_new_set]
    to_push: list[Post] = []
    blocked_after_ids: list[str] = []
    cursor = 0
    while len(to_push) < MAX_PUSH_POSTS and cursor < len(push_candidates):
        missing = MAX_PUSH_POSTS - len(to_push)
        batch = push_candidates[cursor : cursor + missing]
        cursor += len(batch)
        enriched = await _enrich_blog_posts(batch, client)
        visible_enriched, blocked_after_enrich = filter_blocked_posts(enriched, blocks)
        to_push.extend(visible_enriched)
        blocked_after_ids.extend(p.post_id for p in blocked_after_enrich)

    await db.discard_pending(sub.session_id, "blog", blocked_after_ids)

    if not to_push:
        await db.mark_seen_session(sub.session_id, "blog", blocked_before_ids + already_sent_ids + blocked_after_ids)
        return

    await db.mark_seen_session(
        sub.session_id,
        "blog",
        blocked_before_ids + already_sent_ids + blocked_after_ids,
    )
    for post in reversed(to_push):
        await _push_blog_post(sub.session_id, post, sub.target, send_func)
        await db.mark_delivered(sub.session_id, "blog", post.post_id)


class SubscriptionScheduler:
    def __init__(
        self,
        storage: SubscriptionStorage,
        client: LofterClient,
        db: LofterDB,
        send_func: SendFunc,
        *,
        block_storage: AuthorBlockStorage,
        interval_minutes: int = 30,
    ):
        self._storage = storage
        self._client = client
        self._db = db
        self._send_func = send_func
        self._block_storage = block_storage
        self._interval = interval_minutes * 60
        self._task: asyncio.Task | None = None
        self._poll_lock = asyncio.Lock()

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
            try:
                await self._poll_all()
            except Exception:
                logger.exception("Lofter 轮询失败，将在下一轮重试")

    async def _poll_all(self, session_id: str | None = None):
        async with self._poll_lock:
            await self._poll_subscriptions(session_id)

    async def _poll_subscriptions(self, session_id: str | None):
        subs = await self._storage.list_by_session(session_id) if session_id else await self._storage.all()
        by_session: dict[str, dict[str, list[Subscription]]] = {}
        for s in subs:
            by_session.setdefault(s.session_id, {"tag": [], "blog": []})
            by_session[s.session_id][s.type].append(s)

        async def _poll_session(session_id: str, typed: dict):
            errors: list[Exception] = []
            if typed["tag"]:
                try:
                    await _check_tag_session(
                        session_id, typed["tag"], self._client, self._db, self._send_func, self._block_storage
                    )
                except Exception as exc:
                    errors.append(exc)
            for sub in typed["blog"]:
                try:
                    await _check_blog_sub(sub, self._client, self._db, self._send_func, self._block_storage)
                except Exception as exc:
                    logger.error("Lofter 博主「%s」轮询失败：%s", sub.target, exc)
                    errors.append(exc)
            if errors:
                raise errors[0]

        tasks = [_poll_session(sid, typed) for sid, typed in by_session.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for sid, result in zip(by_session, results):
            if isinstance(result, BaseException):
                logger.error("Lofter 轮询会话 %s 失败：%s", sid, result)
        if session_id and results and isinstance(results[0], BaseException):
            raise results[0]
