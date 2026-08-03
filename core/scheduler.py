import asyncio
from typing import Callable, Awaitable

from astrbot.api import logger

from .author_block import (
    AuthorBlockStorage,
    filter_blocked_posts,
    required_author_fields,
)
from .content_source import ContentSource
from .db import LofterDB
from .delivery import (
    SEND_TIMEOUT_SECONDS,
    ClaimStatus,
    DeliveryQueue,
    SourceBatch,
    source_for_subscription,
)
from .errors import (
    IDENTITY_SCHEMA_LOCATIONS,
    SourceSchemaError,
    attach_source_evidence,
)
from .filter import FilterRule, apply_filter
from .parser import POST_FIELDS, Post, post_owner_identity
from .post_consumers import (
    ensure_subscription_posts,
    filter_blocked_with_fields,
)
from .post_fields import (
    ensure_post_fields,
    ensure_posts_fields,
    merge_post_fields,
    validate_post_evidence,
)
from .post_identity import consistent_blog_owner
from .session_gate import SessionGateRegistry
from .source_scan import collect_pages
from .storage import Subscription, SubscriptionStorage
from .subscription_service import (
    SessionSnapshot,
    SubscriptionRef,
    SubscriptionService,
)

SendFunc = Callable[
    [str, Post, str, frozenset[str]], Awaitable[bool]
]
TagSources = dict[str, set[str]]

BLOG_URL = "https://{username}.lofter.com"
MAX_PUSH_POSTS = 5


class _EvidencePosts(list[Post]):
    def __init__(
        self, posts: list[Post], evidence_items: tuple[Post, ...] = ()
    ) -> None:
        super().__init__(posts)
        self.evidence_items = evidence_items


class _EvidenceTargets(dict[str, list[Post]]):
    def __init__(
        self,
        targets: dict[str, list[Post]],
        evidence_items: tuple[Post, ...] = (),
    ) -> None:
        super().__init__(targets)
        self.evidence_items = evidence_items


async def fetch_tag_posts(
    search_tags: list[str], source: ContentSource, limit: int = 20
) -> list[Post]:
    occurrences: list[Post] = []
    evidence: list[Post] = []
    for tag in search_tags:
        page = await collect_pages(
            lambda cursor: source.list_tag(tag, cursor, limit, "new"),
            limit=limit,
        )
        occurrences.extend(page.items)
        evidence.extend(page.evidence_items)
    validate_post_evidence([*evidence, *occurrences])
    enriched: list[Post] = []
    for post in occurrences:
        complete = (
            await ensure_subscription_posts([post], source, {"images"})
        )[0]
        enriched.append(await _ensure_post_owner(complete, source))
    validate_post_evidence([*evidence, *enriched])
    return _EvidencePosts(_merge_posts_by_id(enriched), tuple(evidence))


def _merge_posts_by_id(posts: list[Post]) -> list[Post]:
    result: dict[str, Post] = {}
    for post in posts:
        existing = result.get(post.post_id)
        result[post.post_id] = (
            merge_post_fields(existing, post) if existing else post
        )
    return list(result.values())


def _remember_post_owner(owners: dict[str, str], post: Post) -> None:
    owner = post_owner_identity(post)
    try:
        resolved = consistent_blog_owner(owners.get(post.post_id, ""), owner)
    except ValueError:
        raise SourceSchemaError("post.owner") from None
    if resolved:
        owners[post.post_id] = resolved


async def _ensure_post_owner(post: Post, source: ContentSource) -> Post:
    if post_owner_identity(post):
        return post
    enriched = await ensure_post_fields(post, source, {"author_username"})
    if not post_owner_identity(enriched):
        raise SourceSchemaError("post.owner")
    return enriched


async def fetch_blog_posts(
    sub: Subscription, source: ContentSource, limit: int = 20
) -> list[Post]:
    page = await collect_pages(
        lambda cursor: source.list_blog(sub.target, cursor, limit),
        limit=limit,
    )
    validate_post_evidence([*page.evidence_items, *page.items])
    try:
        posts = await ensure_subscription_posts(page.items, source)
    except Exception as exc:
        attach_source_evidence(
            exc, (*page.evidence_items, *page.items)
        )
        raise
    validate_post_evidence([*page.evidence_items, *posts])
    return _EvidencePosts(posts, page.evidence_items)


def _build_tag_rule(subs: list[Subscription]) -> FilterRule:
    search_tags = [s.target for s in subs if s.role == "subscribe"]
    exclude_tags = [s.target for s in subs if s.role == "exclude"]
    return FilterRule(search_tags=search_tags, exclude_tags=exclude_tags)


def _pick_display_tag(
    post: Post, search_tags: list[str], source_tags: set[str] | None = None
) -> str:
    actual = sorted(source_tags or (), key=str.casefold)
    if actual:
        return actual[0]
    lower_set = {tag.lower() for tag in search_tags}
    hit = next((tag for tag in post.tags if tag.lower() in lower_set), None)
    return hit or (search_tags[0] if search_tags else "标签")


async def _push_tag_posts(
    session_id: str, posts: list[Post], rule: FilterRule,
    send_func: SendFunc, sources: TagSources | None = None,
) -> list[str]:
    accepted = []
    for post in reversed(posts[:MAX_PUSH_POSTS]):
        display_tag = _pick_display_tag(
            post, rule.search_tags, (sources or {}).get(post.post_id)
        )
        header = f"【标签「{display_tag}」有新内容】"
        if not await _send_post(
            session_id, post, header, frozenset({"tag"}), send_func
        ):
            break
        accepted.append(post.post_id)
    return accepted


async def _push_blog_post(
    session_id: str, post: Post, username: str, send_func: SendFunc
) -> bool:
    header = f"【博主「{username}」有新内容】"
    return await _send_post(
        session_id, post, header, frozenset({"blog"}), send_func
    )


async def _send_post(
    session_id: str,
    post: Post,
    header: str,
    source_types: frozenset[str],
    send_func: SendFunc,
) -> bool:
    try:
        return await send_func(session_id, post, header, source_types) is True
    except Exception as exc:
        logger.error("发送订阅推送失败 session=%s post=%s: %s", session_id, post.post_id, exc)
        return False


async def _enrich_blog_posts(posts: list[Post], source: ContentSource) -> list[Post]:
    enriched = []
    for post in posts:
        if post.has_fields(POST_FIELDS):
            enriched.append(post)
            continue
        try:
            rich = await source.get_post(post.url)
        except SourceSchemaError as exc:
            if exc.location in IDENTITY_SCHEMA_LOCATIONS:
                raise
            logger.warning("获取博主帖子详情失败 %s: %s", post.url, exc)
            enriched.append(post)
            continue
        except Exception as exc:
            logger.warning("获取博主帖子详情失败 %s: %s", post.url, exc)
            enriched.append(post)
            continue
        merged = merge_post_fields(post, rich)
        enriched.extend(await ensure_subscription_posts([merged], source))
    return enriched


async def _apply_legacy_rules(
    session_id: str, sub_type: str, posts: list[Post], db: LofterDB,
    checkpoint_target: str | None = None,
) -> tuple[list[Post], bool]:
    post_ids = [post.post_id for post in posts]
    checkpoint_ids = await db.consume_legacy_checkpoints(
        session_id, sub_type, post_ids, checkpoint_target
    )
    if checkpoint_ids is not None:
        allowed = set(checkpoint_ids)
        return [post for post in posts if post.post_id in allowed], True
    eligible_ids, suppressed_ids = await db.filter_legacy_floor(
        session_id, sub_type, post_ids, checkpoint_target
    )
    if suppressed_ids:
        if checkpoint_target is None:
            await db.mark_seen_session(session_id, sub_type, suppressed_ids)
        else:
            await db.mark_seen_targets(
                session_id, sub_type, {checkpoint_target: suppressed_ids}
            )
    allowed = set(eligible_ids)
    return [post for post in posts if post.post_id in allowed], False


async def _prepare_new_posts(
    session_id: str, sub_type: str, posts: list[Post], db: LofterDB,
    checkpoint_target: str | None = None,
) -> list[Post]:
    posts, checkpoint_applied = await _apply_legacy_rules(
        session_id, sub_type, posts, db, checkpoint_target
    )
    if not posts:
        return []
    if checkpoint_applied:
        return posts
    post_ids = [post.post_id for post in posts]
    unseen_ids = await db.filter_unseen_session(session_id, sub_type, post_ids)
    if not unseen_ids:
        return []
    if await db.seen_count(session_id, sub_type) == 0:
        await db.mark_seen_session(session_id, sub_type, unseen_ids)
        return []
    unseen = set(unseen_ids)
    return [post for post in posts if post.post_id in unseen]


async def _filter_unsent_visible_known(
    session_id: str, sub_type: str, posts: list[Post], db: LofterDB,
    blocks: list,
) -> tuple[list[Post], list[str]]:
    visible, blocked = filter_blocked_posts(posts, blocks)
    passive_ids = [post.post_id for post in blocked]
    unsent_ids = await db.filter_unsent(
        session_id, [post.post_id for post in visible]
    )
    unsent = set(unsent_ids)
    passive_ids.extend(
        post.post_id for post in visible if post.post_id not in unsent
    )
    if passive_ids:
        await db.mark_seen_session(session_id, sub_type, passive_ids)
    return [post for post in visible if post.post_id in unsent], passive_ids


async def _filter_unsent_visible(
    session_id: str, sub_type: str, posts: list[Post], db: LofterDB,
    block_storage: AuthorBlockStorage, source: ContentSource,
) -> tuple[list[Post], list[str], list]:
    blocks = await block_storage.list_by_session(session_id)
    visible, blocked = await filter_blocked_with_fields(posts, blocks, source)
    passive_ids = [post.post_id for post in blocked]
    unsent_ids = await db.filter_unsent(session_id, [post.post_id for post in visible])
    unsent = set(unsent_ids)
    passive_ids.extend(post.post_id for post in visible if post.post_id not in unsent)
    if passive_ids:
        await db.mark_seen_session(session_id, sub_type, passive_ids)
    return [post for post in visible if post.post_id in unsent], passive_ids, blocks


def _posts_by_target(post_ids: list[str], sources: TagSources) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for post_id in post_ids:
        for target in sources.get(post_id, set()):
            result.setdefault(target, []).append(post_id)
    return result


async def _fetch_tag_candidates(
    session_id: str, rule: FilterRule, source: ContentSource, db: LofterDB,
    required_fields: set[str] | None = None,
    prepared_targets: dict[str, list[Post]] | None = None,
) -> tuple[list[Post], TagSources, bool]:
    posts_by_target = prepared_targets
    if posts_by_target is None:
        posts_by_target = await _fetch_all_tag_targets(
            rule, source, required_fields
        )
    eligible, checkpoint_applied = await db.apply_tag_legacy_rules(
        session_id, {
            target: [post.post_id for post in posts]
            for target, posts in posts_by_target.items()
        },
    )
    return _merge_eligible_tag_posts(posts_by_target, eligible, checkpoint_applied)


async def _fetch_all_tag_targets(
    rule: FilterRule, source: ContentSource,
    required_fields: set[str] | None = None,
) -> dict[str, list[Post]]:
    raw: dict[str, list[Post]] = {}
    witnesses: list[Post] = []
    for target in rule.search_tags:
        posts = await fetch_tag_posts([target], source)
        raw[target] = list(posts)
        witnesses.extend(getattr(posts, "evidence_items", ()))
    validate_post_evidence([
        *witnesses,
        *(post for posts in raw.values() for post in posts),
    ])
    required = set(required_fields or ())
    if rule.exclude_tags:
        required.add("tags")
    enriched: dict[str, list[Post]] = {}
    for target, posts in raw.items():
        values = await ensure_posts_fields(posts, source, required)
        enriched[target] = [await _ensure_post_owner(post, source) for post in values]
    validation_items = [
        *witnesses,
        *(post for posts in raw.values() for post in posts),
        *(post for posts in enriched.values() for post in posts),
    ]
    validate_post_evidence(validation_items)
    filtered = {
        target: apply_filter(posts, rule)
        for target, posts in enriched.items()
    }
    return _EvidenceTargets(filtered, tuple(validation_items))


def _merge_eligible_tag_posts(
    posts_by_target: dict[str, list[Post]], eligible: dict[str, list[str]],
    checkpoint_applied: bool,
) -> tuple[list[Post], TagSources, bool]:
    validate_post_evidence(
        post for posts in posts_by_target.values() for post in posts
    )
    posts_by_id: dict[str, Post] = {}
    owners: dict[str, str] = {}
    sources: TagSources = {}
    for target, posts in posts_by_target.items():
        allowed = set(eligible.get(target, ()))
        for post in posts:
            _remember_post_owner(owners, post)
            existing = posts_by_id.get(post.post_id)
            posts_by_id[post.post_id] = (
                merge_post_fields(existing, post) if existing else post
            )
            if post.post_id in allowed:
                sources.setdefault(post.post_id, set()).add(target)
    selected = [
        post for post_id, post in posts_by_id.items() if post_id in sources
    ]
    return selected, sources, checkpoint_applied


async def _prepare_tag_posts(
    session_id: str, rule: FilterRule, source: ContentSource, db: LofterDB,
    required_fields: set[str] | None = None,
    prepared_targets: dict[str, list[Post]] | None = None,
) -> tuple[list[Post], TagSources]:
    posts, sources, checkpoint_applied = await _fetch_tag_candidates(
        session_id, rule, source, db, required_fields, prepared_targets
    )
    if not posts:
        return [], sources
    unseen_ids = await db.filter_unseen_targets(
        session_id, "tag", _posts_by_target(
            [post.post_id for post in posts], sources
        )
    )
    if not unseen_ids:
        return [], sources
    if not checkpoint_applied and await db.seen_count(session_id, "tag") == 0:
        await db.mark_seen_targets(
            session_id, "tag", _posts_by_target(unseen_ids, sources)
        )
        return [], sources
    unseen = set(unseen_ids)
    return [post for post in posts if post.post_id in unseen], sources


async def _filter_tag_unsent_visible(
    session_id: str, posts: list[Post], db: LofterDB,
    blocks: list, sources: TagSources,
) -> list[Post]:
    visible, blocked = filter_blocked_posts(posts, blocks)
    passive_ids = [post.post_id for post in blocked]
    unsent_ids = await db.filter_unsent(session_id, [post.post_id for post in visible])
    unsent = set(unsent_ids)
    passive_ids.extend(post.post_id for post in visible if post.post_id not in unsent)
    if passive_ids:
        await db.mark_seen_targets(
            session_id, "tag", _posts_by_target(passive_ids, sources)
        )
    return [post for post in visible if post.post_id in unsent]


async def _check_tag_session(
    session_id: str,
    subs: list[Subscription],
    source: ContentSource,
    db: LofterDB,
    send_func: SendFunc,
    block_storage: AuthorBlockStorage,
    *,
    prepared_targets: dict[str, list[Post]] | None = None,
    prepared_blocks: list | None = None,
) -> bool:
    rule = _build_tag_rule(subs)
    if not rule.search_tags:
        return True
    try:
        blocks = prepared_blocks
        if blocks is None:
            blocks = await block_storage.list_by_session(session_id)
        new_posts, sources = await _prepare_tag_posts(
            session_id, rule, source, db, required_author_fields(blocks),
            prepared_targets,
        )
    except Exception as exc:
        logger.error("轮询标签 session=%s 失败: %s", session_id, exc)
        return False
    if not new_posts:
        return True
    try:
        candidates = await _filter_tag_unsent_visible(
            session_id, new_posts, db, blocks, sources
        )
    except Exception as exc:
        logger.error("轮询标签作者字段补全失败 session=%s: %s", session_id, exc)
        return False
    to_push = candidates[:MAX_PUSH_POSTS]
    if not to_push:
        return True
    accepted_ids = await _push_tag_posts(
        session_id, to_push, rule, send_func, sources
    )
    if accepted_ids:
        await db.mark_accepted_targets(
            session_id, "tag", _posts_by_target(accepted_ids, sources)
        )
    return len(accepted_ids) == len(to_push)


async def _select_blog_pushes(
    candidates: list[Post], source: ContentSource, blocks: list,
) -> tuple[list[Post], list[str]]:
    selected = []
    blocked_ids = []
    cursor = 0
    while len(selected) < MAX_PUSH_POSTS and cursor < len(candidates):
        missing = MAX_PUSH_POSTS - len(selected)
        batch = candidates[cursor : cursor + missing]
        cursor += len(batch)
        enriched = await _enrich_blog_posts(batch, source)
        visible, blocked = filter_blocked_posts(enriched, blocks)
        selected.extend(visible)
        blocked_ids.extend(post.post_id for post in blocked)
    return selected, blocked_ids


async def _push_blog_posts(
    sub: Subscription, posts: list[Post], send_func: SendFunc
) -> list[str]:
    accepted = []
    for post in reversed(posts[:MAX_PUSH_POSTS]):
        if not await _push_blog_post(sub.session_id, post, sub.target, send_func):
            break
        accepted.append(post.post_id)
    return accepted


def _record_blog_failure(
    evidence: list[Post], sub: Subscription, exc: Exception, stage: str
) -> None:
    if isinstance(exc, SourceSchemaError):
        if exc.location in IDENTITY_SCHEMA_LOCATIONS:
            raise exc
    evidence.extend(getattr(exc, "evidence_items", ()))
    logger.error("轮询博主 %s%s失败: %s", sub.target, stage, exc)


async def _preflight_blog_subs(
    subs: list[Subscription], source: ContentSource,
    block_storage: AuthorBlockStorage, prepared_blocks: list | None = None,
) -> tuple[list[tuple[Subscription, list[Post]]], list, tuple[Post, ...]]:
    fetched: list[tuple[Subscription, list[Post]]] = []
    evidence: list[Post] = []
    for sub in subs:
        try:
            posts = await fetch_blog_posts(sub, source)
        except Exception as exc:
            _record_blog_failure(evidence, sub, exc, " ")
            continue
        fetched.append((sub, list(posts)))
        evidence.extend(getattr(posts, "evidence_items", ()))
    validate_post_evidence([
        *evidence,
        *(post for _, posts in fetched for post in posts),
    ])
    blocks = prepared_blocks
    if blocks is None:
        blocks = await block_storage.list_by_session(subs[0].session_id)
    required = required_author_fields(blocks)
    enriched: list[tuple[Subscription, list[Post]]] = []
    for sub, posts in fetched:
        try:
            values = await _enrich_blog_posts(posts, source)
            complete = await ensure_posts_fields(values, source, required)
        except Exception as exc:
            _record_blog_failure(evidence, sub, exc, " 补全")
            continue
        enriched.append((sub, complete))
    validation_items = [
        *evidence,
        *(post for _, posts in fetched for post in posts),
        *(post for _, posts in enriched for post in posts),
    ]
    validate_post_evidence(validation_items)
    return enriched, blocks, tuple(validation_items)


async def _check_blog_sub(
    sub: Subscription,
    source: ContentSource,
    db: LofterDB,
    send_func: SendFunc,
    block_storage: AuthorBlockStorage,
    *,
    prepared_posts: list[Post] | None = None,
    prepared_blocks: list | None = None,
) -> bool:
    if prepared_posts is None:
        try:
            posts = await fetch_blog_posts(sub, source)
        except Exception as e:
            logger.error("轮询博主 %s 失败: %s", sub.target, e)
            return True
        blocks = await block_storage.list_by_session(sub.session_id)
    else:
        posts = prepared_posts
        blocks = prepared_blocks or []
    if not posts:
        return True
    new_posts = await _prepare_new_posts(
        sub.session_id, "blog", posts, db, sub.target
    )
    if not new_posts:
        return True
    if prepared_posts is None:
        candidates, _, blocks = await _filter_unsent_visible(
            sub.session_id, "blog", new_posts, db, block_storage, source
        )
        to_push, blocked_after_ids = await _select_blog_pushes(
            candidates, source, blocks
        )
        if blocked_after_ids:
            await db.mark_seen_session(
                sub.session_id, "blog", blocked_after_ids
            )
    else:
        candidates, _ = await _filter_unsent_visible_known(
            sub.session_id, "blog", new_posts, db, blocks
        )
        to_push = candidates[:MAX_PUSH_POSTS]
    if not to_push:
        return True
    accepted_ids = await _push_blog_posts(sub, to_push, send_func)
    if accepted_ids:
        await db.mark_accepted_session(sub.session_id, "blog", accepted_ids)
    return len(accepted_ids) == len(to_push)


async def _preflight_session(
    session_id: str,
    typed: dict[str, list[Subscription]],
    source: ContentSource,
    block_storage: AuthorBlockStorage,
) -> tuple[dict[str, list[Post]] | None, list[tuple[Subscription, list[Post]]], list]:
    blocks = await block_storage.list_by_session(session_id)
    tag_targets = None
    if typed["tag"]:
        rule = _build_tag_rule(typed["tag"])
        if rule.search_tags:
            tag_targets = await _fetch_all_tag_targets(
                rule, source, required_author_fields(blocks)
            )
    blogs: list[tuple[Subscription, list[Post]]] = []
    blog_evidence: tuple[Post, ...] = ()
    if typed["blog"]:
        blogs, _, blog_evidence = await _preflight_blog_subs(
            typed["blog"], source, block_storage, blocks
        )
    validate_post_evidence([
        *getattr(tag_targets, "evidence_items", ()),
        *(post for posts in (tag_targets or {}).values() for post in posts),
        *blog_evidence,
        *(post for _, posts in blogs for post in posts),
    ])
    return tag_targets, blogs, blocks


async def _poll_session(
    session_id: str, typed: dict[str, list[Subscription]],
    source: ContentSource, db: LofterDB, send_func: SendFunc,
    block_storage: AuthorBlockStorage,
) -> None:
    try:
        tag_targets, prepared_blogs, blocks = await _preflight_session(
            session_id, typed, source, block_storage
        )
    except Exception as exc:
        logger.error("轮询 session=%s 预检失败: %s", session_id, exc)
        return
    if typed["tag"]:
        proceed = await _check_tag_session(
            session_id, typed["tag"], source, db, send_func, block_storage,
            prepared_targets=tag_targets, prepared_blocks=blocks,
        )
        if not proceed:
            return
    for sub, posts in prepared_blogs:
        if not await _check_blog_sub(
            sub, source, db, send_func, block_storage,
            prepared_posts=posts, prepared_blocks=blocks,
        ):
            return


async def _fetch_snapshot_batches(
    snapshot: SessionSnapshot,
    source: ContentSource,
) -> list[SourceBatch]:
    required = required_author_fields(list(snapshot.author_blocks))
    excludes = any(
        sub.type == "tag" and sub.role == "exclude"
        for sub in snapshot.subscriptions
    )
    batches = []
    for sub in snapshot.subscriptions:
        if sub.role != "subscribe" or sub.state != "active":
            continue
        if sub.type == "tag":
            posts = await _fetch_tag_source(sub, source, required, excludes)
        else:
            posts = await _fetch_blog_source(snapshot, sub, source, required)
        batches.append(SourceBatch(source_for_subscription(sub), tuple(posts)))
    return batches


async def _fetch_tag_source(
    sub: SubscriptionRef,
    source: ContentSource,
    required_author: set[str],
    has_excludes: bool,
) -> list[Post]:
    posts = await fetch_tag_posts([sub.target], source)
    required = set(required_author)
    if has_excludes:
        required.add("tags")
    enriched = await ensure_posts_fields(list(posts), source, required)
    validate_post_evidence([
        *getattr(posts, "evidence_items", ()), *posts, *enriched,
    ])
    return enriched


async def _fetch_blog_source(
    snapshot: SessionSnapshot,
    sub: SubscriptionRef,
    source: ContentSource,
    required_author: set[str],
) -> list[Post]:
    legacy = Subscription(
        id=sub.id,
        session_id=snapshot.session_id,
        type="blog",
        role="subscribe",
        target=sub.target,
        state="active",
        revision=sub.revision,
    )
    posts = await fetch_blog_posts(legacy, source)
    enriched = await _enrich_blog_posts(list(posts), source)
    complete = await ensure_posts_fields(enriched, source, required_author)
    validate_post_evidence([
        *getattr(posts, "evidence_items", ()), *posts, *complete,
    ])
    return complete


_SEND_TIMED_OUT = object()


def _delivery_header(sources) -> str:
    source = sources[0]
    if source.type == "tag":
        return f"【标签「{source.target}」有新内容】"
    return f"【博主「{source.target}」有新内容】"


def _consume_send_result(task: asyncio.Task) -> None:
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


async def _send_claim(claim, send_func: SendFunc):
    header = _delivery_header(claim.sources)
    source_types = frozenset(source.type for source in claim.sources)
    task = asyncio.create_task(send_func(
        claim.session_id,
        claim.post,
        header,
        source_types,
    ))
    task.add_done_callback(_consume_send_result)
    try:
        result = await asyncio.wait_for(
            asyncio.shield(task), SEND_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        logger.error(
            "发送订阅推送超时 session=%s post=%s",
            claim.session_id,
            claim.post.post_id,
        )
        return _SEND_TIMED_OUT
    return result is True


async def _drain_session_queue(
    session_id: str,
    queue: DeliveryQueue,
    send_func: SendFunc,
) -> None:
    sent = 0
    while sent < MAX_PUSH_POSTS:
        result = await queue.claim_next(session_id)
        if result.status is not ClaimStatus.CLAIMED:
            return
        claim = result.delivery
        if claim is None:
            return
        sent += 1
        try:
            accepted = await _send_claim(claim, send_func)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "发送订阅推送失败 session=%s post=%s: %s",
                session_id,
                claim.post.post_id,
                exc,
            )
            await queue.release_failure(claim, exc)
            return
        if accepted is _SEND_TIMED_OUT:
            return
        if not accepted:
            await queue.release_failure(claim, "adapter rejected delivery")
            return
        if not await queue.ack_success(claim):
            return


async def _capture_scheduler_snapshot(
    session_id: str,
    service: SubscriptionService,
    gates: SessionGateRegistry,
) -> SessionSnapshot:
    async with gates.hold(session_id):
        return await service.capture_snapshot(session_id)


async def _poll_delivery_session(
    session_id: str,
    source: ContentSource,
    service: SubscriptionService,
    gates: SessionGateRegistry,
    queue: DeliveryQueue,
    send_func: SendFunc,
) -> None:
    snapshot = await _capture_scheduler_snapshot(session_id, service, gates)
    try:
        batches = await _fetch_snapshot_batches(snapshot, source)
        discovery = await queue.persist_discovery(snapshot, batches)
        if discovery.backpressured:
            logger.warning(
                "订阅投递队列已满 session=%s skipped=%d",
                session_id,
                discovery.backpressured,
            )
    except Exception as exc:
        logger.error("轮询 session=%s 抓取或持久化失败: %s", session_id, exc)
    await _drain_session_queue(session_id, queue, send_func)


class SubscriptionScheduler:
    def __init__(
        self,
        storage: SubscriptionStorage,
        source: ContentSource,
        db: LofterDB,
        send_func: SendFunc,
        *,
        block_storage: AuthorBlockStorage,
        interval_minutes: int = 30,
        gates: SessionGateRegistry | None = None,
        subscription_service: SubscriptionService | None = None,
        delivery_queue: DeliveryQueue | None = None,
    ):
        self._storage = storage
        self._source = source
        self._db = db
        self._send_func = send_func
        self._block_storage = block_storage
        self._gates = gates or block_storage._gates
        self._subscription_service = subscription_service or SubscriptionService(
            db, source, self._gates
        )
        self._delivery_queue = delivery_queue or DeliveryQueue(db, self._gates)
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
        session_ids = await self._delivery_queue.session_ids()
        tasks = [
            _poll_delivery_session(
                session_id,
                self._source,
                self._subscription_service,
                self._gates,
                self._delivery_queue,
                self._send_func,
            )
            for session_id in session_ids
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                logger.error("轮询会话失败: %s", result)
