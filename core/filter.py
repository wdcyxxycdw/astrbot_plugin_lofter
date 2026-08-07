from dataclasses import dataclass, field

from .parser import Post


@dataclass
class FilterRule:
    search_tags: list[str]
    exclude_tags: list[str] = field(default_factory=list)


def parse_tag_expr(raw: str) -> tuple[list[str], list[str]]:
    """返回 (subscribes, excludes)。所有 token 统一处理，纯 -B 不会被搜索。"""
    subscribes: list[str] = []
    excludes: list[str] = []
    for token in raw.strip().split():
        if token.startswith("-") and len(token) > 1:
            for t in token[1:].split("|"):
                if t:
                    excludes.append(t)
        else:
            body = token[1:] if token.startswith("+") else token
            for t in body.split("|"):
                if t:
                    subscribes.append(t)
    return subscribes, excludes


def matches(post: Post, rule: FilterRule) -> bool:
    post_tags_lower = {t.lower() for t in post.tags}
    for tag in rule.exclude_tags:
        if tag.lower() in post_tags_lower:
            return False
    return True


def apply_filter(posts: list[Post], rule: FilterRule) -> list[Post]:
    return [p for p in posts if matches(p, rule)]
