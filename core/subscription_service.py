from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .author_block import AuthorBlock
from .db import LofterDB
from .filter import FilterRule, apply_filter
from .parser import Post
from .post_consumers import filter_blocked_with_fields, ensure_subscription_posts
from .post_fields import ensure_posts_fields, validate_post_evidence
from .post_time import parse_publish_time
from .session_gate import SessionGateRegistry
from .source_scan import ContentSource, collect_pages
from .storage import Subscription

SubscriptionType = Literal["tag", "blog"]
SubscriptionRole = Literal["subscribe", "exclude"]


@dataclass(frozen=True)
class SubscriptionRef:
    id: int
    type: SubscriptionType
    role: SubscriptionRole
    target: str
    state: str
    revision: int


@dataclass(frozen=True)
class SessionSnapshot:
    session_id: str
    policy_generation: int
    type_revisions: dict[str, int]
    subscriptions: tuple[SubscriptionRef, ...]
    author_blocks: tuple[AuthorBlock, ...]


@dataclass(frozen=True)
class SubscriptionMutationResult:
    added_subscribes: tuple[str, ...]
    added_excludes: tuple[str, ...]
    preview_posts: tuple[Post, ...] = ()


class SubscriptionService:
    def __init__(
        self,
        db: LofterDB,
        source: ContentSource,
        gates: SessionGateRegistry,
    ) -> None:
        self._db = db
        self._source = source
        self._gates = gates

    async def capture_snapshot(self, session_id: str) -> SessionSnapshot:
        raw = await self._db.capture_session_snapshot(session_id)
        return _snapshot_from_row(session_id, raw)

    async def subscribe_tags(
        self,
        session_id: str,
        subscribes: list[str],
        excludes: list[str],
        *,
        preview: bool = False,
    ) -> SubscriptionMutationResult:
        subscribes = _unique_nonempty(subscribes)
        excludes = _unique_nonempty(excludes)
        if preview and not subscribes:
            raise ValueError("preview requires a subscribe tag")
        snapshot = await self._capture_under_gate(session_id)
        targets = subscribes if preview else _new_subscribe_targets(
            snapshot, "tag", subscribes
        )
        fetched = await self._fetch_tag_targets(targets)
        display_posts = []
        if preview:
            preview_excludes = _tag_excludes(snapshot, excludes)
            display_posts = await self._preview_posts(
                fetched, preview_excludes, list(snapshot.author_blocks)
            )
        seen = _seen_by_target(fetched)
        async with self._gates.hold(session_id):
            added_subs, added_excludes = await self._db.initialize_runtime_subscriptions(
                session_id,
                "tag",
                subscribes,
                excludes,
                seen,
                snapshot.type_revisions["tag"],
                snapshot.policy_generation,
                _snapshot_rows(snapshot),
                preview,
            )
        return SubscriptionMutationResult(
            tuple(added_subs),
            tuple(added_excludes),
            tuple(display_posts if preview else ()),
        )

    async def subscribe_blog(
        self, session_id: str, username: str
    ) -> SubscriptionMutationResult:
        target = username.strip()
        if not target:
            raise ValueError("blog username is required")
        snapshot = await self._capture_under_gate(session_id)
        targets = _new_subscribe_targets(snapshot, "blog", [target])
        posts = await fetch_blog_target(target, self._source) if targets else []
        seen = {target: _post_times(posts)} if targets else {}
        async with self._gates.hold(session_id):
            added, _ = await self._db.initialize_runtime_subscriptions(
                session_id,
                "blog",
                [target],
                [],
                seen,
                snapshot.type_revisions["blog"],
                snapshot.policy_generation,
                _snapshot_rows(snapshot),
            )
        return SubscriptionMutationResult(tuple(added), ())

    async def remove(
        self,
        session_id: str,
        sub_type: SubscriptionType,
        target: str,
        role: SubscriptionRole = "subscribe",
    ) -> bool:
        async with self._gates.hold(session_id):
            return await self._db.remove_runtime_subscription(
                session_id, sub_type, target, role
            )

    async def remove_by_index(
        self, session_id: str, index: int
    ) -> tuple[Subscription | None, int]:
        async with self._gates.hold(session_id):
            row, count = await self._db.remove_runtime_subscription_by_index(
                session_id, index
            )
        return (_subscription_from_row(row) if row else None), count

    async def _capture_under_gate(self, session_id: str) -> SessionSnapshot:
        async with self._gates.hold(session_id):
            return await self.capture_snapshot(session_id)

    async def _fetch_tag_targets(
        self, targets: list[str]
    ) -> dict[str, list[Post]]:
        result: dict[str, list[Post]] = {}
        evidence: list[Post] = []
        for target in targets:
            page = await collect_pages(
                lambda cursor, tag=target: self._source.list_tag(
                    tag, cursor, 20, "new"
                ),
                limit=20,
            )
            posts = await ensure_subscription_posts(page.items, self._source)
            result[target] = posts
            evidence.extend(page.evidence_items)
            evidence.extend(posts)
        validate_post_evidence(evidence)
        return result

    async def _preview_posts(
        self,
        posts_by_target: dict[str, list[Post]],
        excludes: list[str],
        author_blocks: list[AuthorBlock],
    ) -> list[Post]:
        posts = _merge_target_posts(posts_by_target)
        if not posts:
            return []
        if excludes:
            posts = await ensure_posts_fields(posts, self._source, {"tags"})
            posts = apply_filter(posts, FilterRule([], excludes))
        if author_blocks:
            posts, _ = await filter_blocked_with_fields(
                posts, author_blocks, self._source
            )
        return posts


async def fetch_blog_target(
    username: str, source: ContentSource, limit: int = 20
) -> list[Post]:
    page = await collect_pages(
        lambda cursor: source.list_blog(username, cursor, limit),
        limit=limit,
    )
    posts = await ensure_subscription_posts(page.items, source)
    validate_post_evidence([*page.evidence_items, *posts])
    return posts


def _snapshot_from_row(session_id: str, row: tuple) -> SessionSnapshot:
    tag_revision, blog_revision, policy, subscriptions, blocks = row
    refs = tuple(
        SubscriptionRef(item[0], item[1], item[2], item[3], item[4], item[5])
        for item in subscriptions
    )
    author_blocks = tuple(
        AuthorBlock(item[0], item[1], item[2], item[3], item[4])
        for item in blocks
    )
    return SessionSnapshot(
        session_id,
        policy,
        {"tag": tag_revision, "blog": blog_revision},
        refs,
        author_blocks,
    )


def _snapshot_rows(snapshot: SessionSnapshot) -> tuple[tuple, ...]:
    return tuple(
        (sub.id, sub.type, sub.role, sub.target, sub.state, sub.revision)
        for sub in snapshot.subscriptions
    )


def _unique_nonempty(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _new_subscribe_targets(
    snapshot: SessionSnapshot,
    sub_type: SubscriptionType,
    targets: list[str],
) -> list[str]:
    existing = {
        sub.target
        for sub in snapshot.subscriptions
        if sub.type == sub_type and sub.role == "subscribe"
    }
    return [target for target in targets if target not in existing]


def _tag_excludes(
    snapshot: SessionSnapshot, requested: list[str]
) -> list[str]:
    current = [
        sub.target
        for sub in snapshot.subscriptions
        if sub.type == "tag" and sub.role == "exclude"
    ]
    return list(dict.fromkeys([*current, *requested]))


def _post_times(posts: list[Post]) -> list[tuple[str, int]]:
    result = []
    for post in posts:
        published_at = parse_publish_time(post.publish_time)
        if published_at is None:
            raise ValueError("invalid publish time")
        result.append((post.post_id, published_at))
    return result


def _seen_by_target(
    posts_by_target: dict[str, list[Post]],
) -> dict[str, list[tuple[str, int]]]:
    return {
        target: _post_times(posts)
        for target, posts in posts_by_target.items()
    }


def _merge_target_posts(posts_by_target: dict[str, list[Post]]) -> list[Post]:
    occurrences = [
        post for posts in posts_by_target.values() for post in posts
    ]
    validate_post_evidence(occurrences)
    result: dict[str, Post] = {}
    for post in occurrences:
        result.setdefault(post.post_id, post)
    return list(result.values())


def _subscription_from_row(row: tuple) -> Subscription:
    return Subscription(
        id=row[0],
        session_id=row[1],
        type=row[2],
        role=row[3],
        target=row[4],
        created_at=row[5],
        state=row[6],
        revision=row[7],
        initialized_at=row[8],
    )
