from dataclasses import dataclass, field

from .errors import SourceSchemaError
from .parser import Post


@dataclass
class FilterRule:
    search_tags: list[str]
    exclude_tags: list[str] = field(default_factory=list)


def _split_expr_token(token: str) -> tuple[list[str], list[str]]:
    negative = token.startswith("-") and len(token) > 1
    body = token[1:] if negative or token.startswith("+") else token
    values = [value for value in body.split("|") if value]
    return ([], values) if negative else (values, [])


def parse_tag_expr(raw: str) -> tuple[list[str], list[str]]:
    """返回 (subscribes, excludes)。所有 token 统一处理，纯 -B 不会被搜索。"""
    subscribes: list[str] = []
    excludes: list[str] = []
    for token in raw.strip().split():
        positive, negative = _split_expr_token(token)
        subscribes.extend(positive)
        excludes.extend(negative)
    return subscribes, excludes


def matches(post: Post, rule: FilterRule) -> bool:
    if rule.exclude_tags and not post.has_fields({"tags"}):
        raise SourceSchemaError("tags")
    post_tags_lower = {t.lower() for t in post.tags}
    for tag in rule.exclude_tags:
        if tag.lower() in post_tags_lower:
            return False
    return True


def apply_filter(posts: list[Post], rule: FilterRule) -> list[Post]:
    return [p for p in posts if matches(p, rule)]
