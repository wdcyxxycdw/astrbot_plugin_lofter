from __future__ import annotations

from .author_block import AuthorBlock, is_author_blocked, required_author_fields
from .errors import SourceSchemaError
from .filter import FilterRule, apply_filter
from .parser import Post
from .post_fields import ensure_post_fields, ensure_posts_fields
from .post_identity import is_canonical_post_url, post_id_from_url
from .post_time import parse_publish_time
from .source_scan import ContentSource


async def apply_filter_with_fields(
    posts: list[Post],
    rule: FilterRule,
    source: ContentSource,
) -> list[Post]:
    required = {"tags"} if rule.exclude_tags else set()
    enriched = await ensure_posts_fields(posts, source, required)
    return apply_filter(enriched, rule)


async def filter_blocked_with_fields(
    posts: list[Post],
    blocks: list[AuthorBlock],
    source: ContentSource,
) -> tuple[list[Post], list[Post]]:
    visible: list[Post] = []
    blocked: list[Post] = []
    required = required_author_fields(blocks)
    for post in posts:
        try:
            is_blocked = is_author_blocked(post, blocks)
        except SourceSchemaError:
            post = await ensure_post_fields(post, source, required)
            is_blocked = is_author_blocked(post, blocks)
        (blocked if is_blocked else visible).append(post)
    return visible, blocked


async def ensure_subscription_posts(
    posts: list[Post],
    source: ContentSource,
    required_fields: set[str] | None = None,
) -> list[Post]:
    required = {"url", "publish_time"} | set(required_fields or ())
    enriched = await ensure_posts_fields(posts, source, required)
    for post in enriched:
        _validate_subscription_post(post)
    return enriched


def _validate_subscription_post(post: Post) -> None:
    valid_identity = (
        post.post_id
        and is_canonical_post_url(post.url)
        and post_id_from_url(post.url) == post.post_id
    )
    if not valid_identity:
        raise SourceSchemaError("post_id")
    if parse_publish_time(post.publish_time) is None:
        raise SourceSchemaError("publishTime")
