import json
from dataclasses import dataclass, field

from .parser import Post


@dataclass
class FilterRule:
    search_tags: list[str]
    include_tags: list[str] = field(default_factory=list)
    exclude_tags: list[str] = field(default_factory=list)
    or_tag_groups: list[list[str]] = field(default_factory=list)
    raw: str = ""


def parse_filter_expr(raw: str) -> FilterRule:
    tokens = raw.strip().split()
    if not tokens:
        return FilterRule(search_tags=[], raw=raw)

    first, rest = tokens[0], tokens[1:]
    search_tags = first.split("|")

    include_tags: list[str] = []
    exclude_tags: list[str] = []
    or_tag_groups: list[list[str]] = []

    for token in rest:
        if token.startswith("+"):
            include_tags.append(token[1:])
        elif token.startswith("-"):
            exclude_tags.append(token[1:])
        elif "|" in token:
            or_tag_groups.append(token.split("|"))
        else:
            include_tags.append(token)

    return FilterRule(
        search_tags=search_tags,
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        or_tag_groups=or_tag_groups,
        raw=raw,
    )


def matches(post: Post, rule: FilterRule) -> bool:
    post_tags_lower = {t.lower() for t in post.tags}

    for tag in rule.exclude_tags:
        if tag.lower() in post_tags_lower:
            return False

    for tag in rule.include_tags:
        if tag.lower() not in post_tags_lower:
            return False

    for group in rule.or_tag_groups:
        if not any(t.lower() in post_tags_lower for t in group):
            return False

    return True


def apply_filter(posts: list[Post], rule: FilterRule) -> list[Post]:
    return [p for p in posts if matches(p, rule)]


def has_filter(rule: FilterRule) -> bool:
    return bool(rule.include_tags or rule.exclude_tags or rule.or_tag_groups)


def filter_rule_to_json(rule: FilterRule) -> str:
    return json.dumps({
        "search_tags": rule.search_tags,
        "include_tags": rule.include_tags,
        "exclude_tags": rule.exclude_tags,
        "or_tag_groups": rule.or_tag_groups,
        "raw": rule.raw,
    }, ensure_ascii=False)


def filter_rule_from_json(s: str) -> FilterRule:
    d = json.loads(s)
    return FilterRule(
        search_tags=d["search_tags"],
        include_tags=d.get("include_tags", []),
        exclude_tags=d.get("exclude_tags", []),
        or_tag_groups=d.get("or_tag_groups", []),
        raw=d.get("raw", ""),
    )
