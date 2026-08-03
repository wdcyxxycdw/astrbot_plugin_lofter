"""解析 Lofter DWR 回调并映射为帖子。"""

import html
import json
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from .dwr_engine import execute_dwr, validate_dwr_input
from .errors import (
    DWREvidenceError,
    DWRIdentityError,
    SourceChallengeError,
    SourceLimitError,
    SourceSchemaError,
    attach_source_evidence,
    limit_identity_complete,
    mark_limit_identity_complete,
    prepend_source_evidence,
)
from .parser import Post, extract_lofter_username, post_field_metadata
from .post_identity import (
    canonical_post_id,
    consistent_blog_owner,
    decimal_post_id,
    post_url_identity,
    validate_mobile_identity_parts,
)
from .post_time import format_publish_time
from .source_limits import (
    MAX_CONTENT_BYTES,
    MAX_ITEMS,
    MAX_TITLE_BYTES,
    MAX_URL_BYTES,
    validate_text_bytes,
)

_DWR_CALLBACK = "dwr.engine._remoteHandleCallback"
_DWR_POST_PATH = re.compile(r"^/post/[A-Za-z0-9_-]+/?$")
_DWR_POST_SLUG = re.compile(r"^[A-Za-z0-9_-]+$")
_CHALLENGE_MARKERS = (
    "not logged in",
    "captcha",
    "challenge",
    "登录",
    "验证码",
    "安全验证",
    "请求过于频繁",
    "访问过于频繁",
    "风控",
)


@dataclass(frozen=True)
class DWRParseResult:
    items: list[Post]
    mapped_count: int
    dropped_count: int
    is_empty: bool
    evidence_items: tuple[Post, ...] = ()

    @property
    def posts(self) -> list[Post]:
        return self.items

    @property
    def exhausted(self) -> bool | None:
        return True if self.is_empty else None

    @property
    def complete(self) -> bool:
        return self.dropped_count == 0


def _validate_dwr_response(body: str) -> None:
    validate_dwr_input(body)
    text = body.strip()
    if not text:
        raise SourceSchemaError("dwr.body")
    sample = text[:4096].casefold()
    if _looks_like_html(sample):
        raise SourceChallengeError()
    if any(marker in sample for marker in _CHALLENGE_MARKERS):
        raise SourceChallengeError()


def _looks_like_html(text: str) -> bool:
    return text.startswith("<!doctype") or text.startswith("<html") or "<html" in text


def _fmt_time(ts_ms: object) -> tuple[str, bool]:
    value = format_publish_time(ts_ms)
    return value, bool(value)


def _strip_html(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _bounded_text(value: object, resource: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SourceSchemaError(resource)
    return validate_text_bytes(value, resource, limit)


def _extract_summary(value: object, field: str) -> tuple[str, bool]:
    if isinstance(value, str):
        raw = _bounded_text(value, "content", MAX_CONTENT_BYTES)
        return _strip_html(raw).strip(), True
    if isinstance(value, dict):
        return _summary_alias(value, field)
    return "", False


def _summary_alias(
    value: dict[object, object], field: str
) -> tuple[str, bool]:
    aliases: list[str] = []
    fields: list[str] = []
    for key in ("content", "text"):
        if key not in value or value[key] is None:
            continue
        raw = value[key]
        if not isinstance(raw, str):
            raise SourceSchemaError("content")
        text = _bounded_text(raw, "content", MAX_CONTENT_BYTES)
        aliases.append(_strip_html(text).strip())
        fields.append(f"{field}.{key}")
    return _consistent_summary_aliases(aliases, fields)


def _consistent_summary_aliases(
    aliases: list[str], fields: list[str]
) -> tuple[str, bool]:
    if not aliases:
        return "", False
    if any(value != aliases[0] for value in aliases[1:]):
        raise DWREvidenceError("content_alias_conflict", *fields)
    return aliases[0], True


def _clean_image_urls(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    seen: dict[str, None] = {}
    for value in values:
        if isinstance(value, str) and value:
            validate_text_bytes(value, "url", MAX_URL_BYTES)
            seen[value.split("?")[0]] = None
    return list(seen)


def _extract_images(value: object) -> list[str]:
    if isinstance(value, list):
        return _clean_image_urls(value)
    if not isinstance(value, str):
        return []
    try:
        urls = json.loads(value)
    except (ValueError, RecursionError):
        urls = None
    if isinstance(urls, list):
        return _clean_image_urls(urls)
    if value.startswith("http"):
        validate_text_bytes(value, "url", MAX_URL_BYTES)
        return [value.split("?")[0]]
    return []


def _extract_tags(value: object) -> tuple[list[str], bool]:
    if not isinstance(value, str):
        return [], False
    return [tag.strip() for tag in value.split(",") if tag.strip()], True


def _map_post(item: object) -> Optional[Post]:
    if not isinstance(item, dict):
        return None
    post = item.get("post")
    if not isinstance(post, dict):
        return None
    aliases = _url_identities(post)
    if not aliases:
        return None
    url, post_id = _validate_identity(post, aliases)
    witness = _identity_witness(post, url, post_id)
    try:
        title, title_known = _string_field(post, "title", "title", MAX_TITLE_BYTES)
        summary, summary_known = _content_field(post)
        tags, tags_known = _extract_tags(post.get("tag"))
        publish_time, time_known = _fmt_time(post.get("publishTime"))
        images, images_known = _image_field(post)
        author, username, author_known, username_known = _author_fields(post, url)
    except SourceLimitError as exc:
        attach_source_evidence(exc, (witness,))
        mark_limit_identity_complete(exc)
        raise
    except SourceSchemaError as exc:
        attach_source_evidence(exc, (witness,))
        raise
    known = _known_fields(
        title_known, summary_known, tags_known, time_known,
        images_known, author_known, username_known,
    )
    return Post(
        post_id=post_id, url=url, title=title, summary=summary,
        author=author, author_username=username, tags=tags,
        publish_time=publish_time, images=images,
        **post_field_metadata("dwr", known),
    )


def _url_identities(
    post: dict[str, object],
) -> list[tuple[str, str, str, str]]:
    authoritative = [
        identity
        for key in ("postUrl", "permalink")
        if (identity := _authoritative_url_identity(post, key)) is not None
    ]
    fallback = _fallback_url_identity(post)
    return authoritative or ([fallback] if fallback is not None else [])


def _authoritative_url_identity(
    post: dict[str, object], key: str
) -> tuple[str, str, str, str] | None:
    if key not in post or post[key] is None:
        return None
    value = post[key]
    if not isinstance(value, str):
        raise DWRIdentityError("invalid_identity_type", key)
    try:
        value = validate_text_bytes(value, "url", MAX_URL_BYTES)
    except SourceLimitError:
        raise
    except SourceSchemaError:
        raise DWRIdentityError(
            "invalid_post_url",
            key,
            value_shape=_dwr_url_shape(value),
        ) from None
    if key == "permalink" and _DWR_POST_SLUG.fullmatch(value):
        post_id = canonical_post_id(value)
        return key, f"https://lofter.com/post/{post_id}", post_id, ""
    try:
        url, post_id, owner = post_url_identity(value)
    except ValueError:
        raise DWRIdentityError(
            "invalid_post_url",
            key,
            value_shape=_dwr_url_shape(value),
        ) from None
    return key, url, post_id, owner


def _dwr_url_shape(value: str) -> str:
    if value == "":
        return "empty"
    if value.isspace():
        return "blank"
    if value != value.strip():
        return "surrounding_whitespace"
    if _DWR_POST_SLUG.fullmatch(value):
        return "slug"
    if _DWR_POST_PATH.fullmatch(value) or _DWR_POST_PATH.fullmatch(f"/{value}"):
        return "relative_post_path"
    if value.startswith("//"):
        return "protocol_relative"
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError, UnicodeError):
        return "malformed_url"
    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").casefold().rstrip(".")
    if scheme == "http":
        return "http_url"
    if scheme != "https":
        return "other_url" if scheme else "text"
    first_party = host == "lofter.com" or host.endswith(".lofter.com")
    if not first_party:
        return "external_https_url"
    if parsed.username is not None or parsed.password is not None or port not in (None, 443):
        return "first_party_post_url_invalid_authority"
    if _DWR_POST_PATH.fullmatch(parsed.path):
        if parsed.query:
            return "first_party_post_url_with_query"
        if parsed.fragment:
            return "first_party_post_url_with_fragment"
    return "first_party_non_post_url"


def _fallback_url_identity(
    post: dict[str, object],
) -> tuple[str, str, str, str] | None:
    value = post.get("blogPageUrl")
    if not isinstance(value, str):
        return None
    try:
        validate_text_bytes(value, "url", MAX_URL_BYTES)
    except SourceLimitError:
        raise
    except SourceSchemaError:
        return None
    try:
        url, post_id, owner = post_url_identity(value)
    except ValueError:
        return None
    return "blogPageUrl", url, post_id, owner


def _validate_identity(
    post: dict[str, object],
    identities: list[tuple[str, str, str, str]],
) -> tuple[str, str]:
    blog = post.get("blogInfo")
    blog = blog if isinstance(blog, dict) else {}
    fields = tuple(identity[0] for identity in identities)
    post_id = identities[0][2]
    if any(identity[2] != post_id for identity in identities[1:]):
        raise DWRIdentityError("post_url_conflict", *fields)
    blog_name = blog.get("blogName")
    structured = blog_name if isinstance(blog_name, str) else ""
    owner_fields = tuple(
        identity[0] for identity in identities if identity[3]
    )
    try:
        consistent_blog_owner(
            *(identity[3] for identity in identities), structured
        )
    except ValueError:
        if structured:
            owner_fields = (*owner_fields, "blogInfo.blogName")
        raise DWRIdentityError("owner_conflict", *owner_fields) from None
    selected = next(
        (identity for identity in identities if identity[3]),
        identities[0],
    )
    _validate_numeric_identity(post, blog, post_id, selected[0])
    return selected[1], post_id


def _identity_witness(post: dict[str, object], url: str, post_id: str) -> Post:
    blog = post.get("blogInfo")
    blog = blog if isinstance(blog, dict) else {}
    url_owner = post_url_identity(url)[2]
    blog_name = blog.get("blogName")
    username = url_owner or (blog_name if isinstance(blog_name, str) else "")
    known = {"url"}
    if username:
        known.add("author_username")
    return Post(
        post_id=post_id,
        title="",
        summary="",
        url=url,
        author_username=username,
        **post_field_metadata("dwr", known),
    )


def _validate_numeric_identity(
    post: dict[str, object],
    blog: dict[str, object],
    post_id: str,
    url_field: str,
) -> None:
    post_blog_id = post.get("blogId")
    blog_info_id = blog.get("blogId")
    blog_fields = tuple(
        field
        for field, value in (
            ("blogId", post_blog_id),
            ("blogInfo.blogId", blog_info_id),
        )
        if value is not None
    )
    for value, field in (
        (post_blog_id, "blogId"),
        (blog_info_id, "blogInfo.blogId"),
    ):
        if value is None:
            continue
        try:
            decimal_post_id(value, 0)
        except ValueError:
            raise DWRIdentityError("invalid_identity_type", field) from None
    if post_blog_id is not None and blog_info_id is not None:
        if decimal_post_id(post_blog_id, 0) != decimal_post_id(blog_info_id, 0):
            raise DWRIdentityError("blog_id_conflict", *blog_fields)
    try:
        validate_mobile_identity_parts(post_id, blog_ids=(
            post_blog_id, blog_info_id,
        ))
    except ValueError:
        raise DWRIdentityError(
            "blog_id_conflict", url_field, *blog_fields
        ) from None
    dwr_post_id = post.get("postId")
    if dwr_post_id is None:
        return
    try:
        decimal_post_id(0, dwr_post_id)
    except ValueError:
        raise DWRIdentityError(
            "invalid_identity_type", "postId"
        ) from None
    try:
        validate_mobile_identity_parts(
            post_id, post_ids=(dwr_post_id,)
        )
    except ValueError:
        raise DWRIdentityError(
            "post_id_conflict", "postId", url_field
        ) from None


def _string_field(
    data: dict[str, object], key: str, resource: str, limit: int
) -> tuple[str, bool]:
    value = data.get(key)
    if not isinstance(value, str):
        return "", False
    return _bounded_text(value, resource, limit), True


def _content_field(post: dict[str, object]) -> tuple[str, bool]:
    candidates: list[str] = []
    for key in ("dirContent", "content"):
        if key not in post or not isinstance(post[key], (str, dict)):
            continue
        value, complete = _extract_summary(post[key], key)
        if complete:
            candidates.append(value)
    if not candidates:
        return "", False
    summary = next((value for value in candidates if value), "")
    return summary[:300] + ("…" if len(summary) > 300 else ""), True


def _image_field(post: dict[str, object]) -> tuple[list[str], bool]:
    value = post.get("firstImageUrl")
    if not isinstance(value, (str, list)):
        return [], False
    return _extract_images(value), False


def _author_fields(
    post: dict[str, object], url: str
) -> tuple[str, str, bool, bool]:
    blog = post.get("blogInfo")
    blog = blog if isinstance(blog, dict) else {}
    nickname = blog.get("blogNickName")
    author = nickname if isinstance(nickname, str) else ""
    author_known = isinstance(nickname, str)
    url_username = extract_lofter_username(url)
    blog_name = blog.get("blogName")
    username = url_username or (blog_name if isinstance(blog_name, str) else "")
    return author, username, author_known, bool(username)


def _known_fields(*flags: bool) -> set[str]:
    names = (
        "title", "summary", "tags", "publish_time",
        "images", "author", "author_username",
    )
    return {"url"} | {name for name, known in zip(names, flags) if known}


def _map_items(items: list[object]) -> tuple[list[Post], int, tuple[Post, ...]]:
    posts: list[Post] = []
    evidence: list[Post] = []
    records: list[Post] = []
    dropped_count = 0
    for item in items:
        try:
            post = _map_post(item)
        except SourceLimitError as exc:
            if limit_identity_complete(exc):
                prepend_source_evidence(exc, records)
            raise
        except SourceSchemaError as exc:
            if exc.location in {"dwr.post.id", "post.evidence"}:
                raise
            witnesses = tuple(getattr(exc, "evidence_items", ()))
            evidence.extend(witnesses)
            records.extend(witnesses)
            post = None
        if post is None:
            dropped_count += 1
        else:
            posts.append(post)
            records.append(post)
    if posts and all(post.has_fields({"publish_time"}) for post in posts):
        posts.sort(key=lambda post: post.publish_time, reverse=True)
    return posts, dropped_count, tuple(evidence)


async def parse_dwr_response_result(body: str) -> DWRParseResult:
    """返回帖子及 mapped/dropped 完整性统计。"""
    _validate_dwr_response(body)
    items = await execute_dwr(body)
    if len(items) > MAX_ITEMS:
        raise SourceLimitError("items", MAX_ITEMS)
    posts, dropped_count, evidence = _map_items(items)
    if items and not posts:
        error = SourceSchemaError("dwr.items")
        attach_source_evidence(error, evidence)
        raise error
    return DWRParseResult(
        items=posts,
        mapped_count=len(posts),
        dropped_count=dropped_count,
        is_empty=not items,
        evidence_items=evidence,
    )


async def parse_dwr_response(body: str) -> list[Post]:
    """兼容入口：返回按发布时间倒序的 Post 列表。"""
    result = await parse_dwr_response_result(body)
    return result.posts
