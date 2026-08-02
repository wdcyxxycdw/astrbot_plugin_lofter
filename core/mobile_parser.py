from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from .errors import (
    SourceBusinessError,
    SourceChallengeError,
    SourceLimitError,
    SourcePartialError,
    SourceSchemaError,
    attach_source_evidence,
    limit_identity_complete,
    mark_limit_identity_complete,
    prepend_source_evidence,
)
from .parser import Post, extract_lofter_username, post_field_metadata
from .post_identity import (
    canonical_post_url as normalize_post_url,
    consistent_blog_owner,
    decimal_post_id,
    mobile_decimal_ids,
    post_id_from_url,
    post_url_identity,
)
from .post_time import format_publish_time
from .source_limits import (
    MAX_CONTENT_BYTES,
    MAX_ITEMS,
    MAX_TITLE_BYTES,
    MAX_URL_BYTES,
    validate_text_bytes,
)

MAX_BODY_BYTES = 5 * 1024 * 1024

_MOBILE_ITEM_IDENTITY_LOCATIONS = frozenset({
    "blogInfo.blogId",
    "blogInfo.blogName",
    "post.id",
    "post.url",
    "postData.postCount.blogId",
    "postData.postView.blogId",
})
_MISSING = object()
_HTML_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class MobilePage:
    items: list[Post]
    source: str
    next_cursor: str | None
    exhausted: bool
    sort: str
    mapped_count: int
    dropped_count: int
    complete: bool
    evidence_items: tuple[Post, ...] = ()
    identity_records: tuple[Post, ...] = ()


@dataclass(frozen=True)
class _MobileIdentity:
    post_id: str
    url: str = ""
    owner: str = ""


def parse_mobile_post_detail(raw: str | bytes | Mapping[str, Any]) -> Post:
    root = _load_root(raw)
    response = _meta_response(root, "detail")
    posts = _items(response, "posts")
    if len(posts) != 1:
        raise SourceSchemaError("response.posts")
    return _map_detail_post(_mapping(posts[0], "response.posts[]"))


def parse_mobile_blog_page(raw: str | bytes | Mapping[str, Any]) -> MobilePage:
    root = _load_root(raw)
    response = _meta_response(root, "blog")
    _validate_blog_envelope(response)
    return _build_page(response, "posts", "mobile_blog", "new", _map_blog_post)


def parse_mobile_tag_page(raw: str | bytes | Mapping[str, Any]) -> MobilePage:
    root = _load_root(raw)
    code = _required_int(root, "code")
    if code != 0:
        raise SourceBusinessError(code)
    _required_string(root, "msg", MAX_TITLE_BYTES, "msg")
    data = _required_mapping(root, "data")
    return _build_page(data, "list", "mobile_tag", "new", _map_tag_post)


def _build_page(
    envelope: Mapping[str, Any],
    key: str,
    source: str,
    sort: str,
    mapper: Any,
) -> MobilePage:
    raw_items = _items(envelope, key)
    posts, dropped, evidence, records = _map_items(raw_items, mapper)
    if raw_items and not posts:
        error = SourcePartialError(0, dropped)
        attach_source_evidence(error, records)
        raise error
    cursor = _cursor(envelope.get("offset", _MISSING))
    return MobilePage(
        items=posts,
        source=source,
        next_cursor=cursor,
        exhausted=cursor is None,
        sort=sort,
        mapped_count=len(posts),
        dropped_count=dropped,
        complete=dropped == 0,
        evidence_items=evidence,
        identity_records=records,
    )


def _load_root(raw: str | bytes | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    if not isinstance(raw, (str, bytes)):
        raise SourceSchemaError("response")
    try:
        encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    except UnicodeEncodeError:
        raise SourceSchemaError("response") from None
    if len(encoded) > MAX_BODY_BYTES:
        raise SourceLimitError("body", MAX_BODY_BYTES)
    try:
        text = encoded.decode("utf-8-sig").lstrip()
    except UnicodeDecodeError as exc:
        raise SourceSchemaError("response") from exc
    if text.startswith("<"):
        raise SourceChallengeError()
    try:
        root = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SourceSchemaError("response") from exc
    return _mapping(root, "response")


def _meta_response(root: Mapping[str, Any], endpoint: str) -> Mapping[str, Any]:
    meta = _required_mapping(root, "meta")
    status = _required_int(meta, "status")
    if status != 200:
        raise SourceBusinessError(status)
    _required_string(meta, "msg", MAX_TITLE_BYTES, "meta.msg")
    return _required_mapping(root, "response")


def _validate_blog_envelope(response: Mapping[str, Any]) -> None:
    _required_list(response, "archives")
    _required_int(response, "minTimeStamp")
    if not isinstance(response.get("isMember"), bool):
        raise SourceSchemaError("response.isMember")
    first_post = response.get("firstPost", _MISSING)
    if first_post is not None:
        raise SourceSchemaError("response.firstPost")
    _cursor(response.get("offset", _MISSING))


def _items(data: Mapping[str, Any], key: str) -> list[Any]:
    value = _required_list(data, key)
    if len(value) > MAX_ITEMS:
        raise SourceLimitError("items", MAX_ITEMS)
    return value


def _required_list(data: Mapping[str, Any], key: str) -> list[Any]:
    value = data.get(key, _MISSING)
    if not isinstance(value, list):
        raise SourceSchemaError(key)
    return value


def _map_items(
    items: Sequence[Any], mapper: Any
) -> tuple[list[Post], int, tuple[Post, ...], tuple[Post, ...]]:
    posts: list[Post] = []
    evidence: list[Post] = []
    records: list[Post] = []
    dropped = 0
    for item in items:
        try:
            post = mapper(_mapping(item, "items[]"))
            posts.append(post)
            records.append(post)
        except SourceLimitError as exc:
            if limit_identity_complete(exc):
                prepend_source_evidence(exc, records)
            raise
        except SourceSchemaError as exc:
            if exc.location in _MOBILE_ITEM_IDENTITY_LOCATIONS:
                raise
            dropped += 1
            witnesses = tuple(getattr(exc, "evidence_items", ()))
            evidence.extend(witnesses)
            records.extend(witnesses)
    return posts, dropped, tuple(evidence), tuple(records)


def _map_blog_post(item: Mapping[str, Any]) -> Post:
    return _map_detail_post(item, "mobile_blog")


def _map_detail_post(
    item: Mapping[str, Any], source: str = "mobile_detail"
) -> Post:
    raw_post = item.get("post")
    raw_blog = item.get("blogInfo")
    identity = _preflight_detail_identity(
        item,
        raw_post if isinstance(raw_post, Mapping) else {},
        raw_blog if isinstance(raw_blog, Mapping) else {},
    )
    try:
        return _map_detail_fields(item, source)
    except SourceLimitError as exc:
        _attach_identity_witness(exc, identity, source)
        if identity is not None:
            mark_limit_identity_complete(exc)
        raise
    except SourceSchemaError as exc:
        _attach_identity_witness(exc, identity, source)
        raise


def _map_detail_fields(item: Mapping[str, Any], source: str) -> Post:
    post = _required_mapping(item, "post")
    blog = _required_mapping(item, "blogInfo")
    post_id = _canonical_decimal_id(post, blog)
    url = _detail_url(item, post_id)
    title = _required_string(post, "title", MAX_TITLE_BYTES, "title")
    content_known = isinstance(post.get("content"), str)
    digest_known = isinstance(post.get("digest"), str)
    tags_known = "tag" in post and post["tag"] is not None
    images_known = "photoLinks" in post and post["photoLinks"] is not None
    content = _optional_string(post, "content", MAX_CONTENT_BYTES, "content")
    digest = _optional_string(post, "digest", MAX_CONTENT_BYTES, "content")
    tags = _tags(post.get("tag"))
    photo_links = post.get("photoLinks")
    images = [] if photo_links is None else _url_list(photo_links, "photoLinks")
    _string_list(post.get("photoCaptions", []), "photoCaptions")
    username = _blog_username(blog, url)
    known = {"title", "author", "publish_time", "url"}
    if username:
        known.add("author_username")
    known.update({
        field for field, complete in (
            ("summary", digest_known or content_known),
            ("content", content_known),
            ("tags", tags_known),
            ("images", images_known),
        ) if complete
    })
    return Post(
        post_id=post_id,
        title=title,
        summary=_summary(digest or content),
        images=images,
        author=_required_string(blog, "blogNickName", MAX_TITLE_BYTES, "author"),
        author_username=username,
        url=url,
        tags=tags,
        publish_time=_publish_time(post.get("publishTime", _MISSING)),
        content=_plain_text(content),
        **post_field_metadata(source, known),
    )


def _map_tag_post(item: Mapping[str, Any]) -> Post:
    post_data = _required_mapping(item, "postData")
    raw_view = post_data.get("postView")
    raw_count = post_data.get("postCount")
    identity = _preflight_tag_identity(
        raw_view if isinstance(raw_view, Mapping) else {},
        raw_count if isinstance(raw_count, Mapping) else {},
    )
    try:
        return _map_tag_fields(post_data)
    except SourceLimitError as exc:
        _attach_identity_witness(exc, identity, "mobile_tag")
        if identity is not None:
            mark_limit_identity_complete(exc)
        raise
    except SourceSchemaError as exc:
        _attach_identity_witness(exc, identity, "mobile_tag")
        raise


def _map_tag_fields(post_data: Mapping[str, Any]) -> Post:
    view = _required_mapping(post_data, "postView")
    count = _required_mapping(post_data, "postCount")
    blog_id = _required_id(view, "blogId")
    if _required_id(count, "blogId") != blog_id:
        raise SourceSchemaError("postData.postCount.blogId")
    url = _canonical_post_url(
        _required_string(view, "permalink", MAX_URL_BYTES, "url")
    )
    post_id = post_id_from_url(url)
    ids = mobile_decimal_ids(post_id)
    if ids is None or int(ids[0]) != blog_id:
        raise SourceSchemaError("postData.postView.blogId")
    photo_count = _required_int(view, "photoCount")
    if photo_count < 0:
        raise SourceSchemaError("postData.postView.photoCount")
    username = extract_lofter_username(url)
    known = {"title", "publish_time", "url"}
    if username:
        known.add("author_username")
    return Post(
        post_id=post_id,
        title=_required_string(view, "title", MAX_TITLE_BYTES, "title"),
        summary="",
        author_username=username,
        url=url,
        publish_time=_publish_time(view.get("publishTime", _MISSING)),
        **post_field_metadata("mobile_tag", known),
    )


def _preflight_tag_identity(
    view: Mapping[str, Any], count: Mapping[str, Any]
) -> _MobileIdentity | None:
    view_blog_id = _optional_id(
        view, "blogId", "postData.postView.blogId"
    )
    count_blog_id = _optional_id(
        count, "blogId", "postData.postCount.blogId"
    )
    view_post_id = _optional_alias_id(view, ("id", "postId"), "post.id")
    identity = _optional_post_url_identity(
        view.get("permalink", _MISSING)
    )
    url_ids = mobile_decimal_ids(identity.post_id) if identity else None
    url_blog_id = int(url_ids[0]) if url_ids else None
    url_post_id = int(url_ids[1]) if url_ids else None
    if view_post_id is not None and url_post_id is not None:
        if view_post_id != url_post_id:
            raise SourceSchemaError("post.id")
    if view_blog_id is not None and count_blog_id is not None:
        if view_blog_id != count_blog_id:
            raise SourceSchemaError("postData.postCount.blogId")
    if url_blog_id is not None and view_blog_id is not None:
        if url_blog_id != view_blog_id:
            raise SourceSchemaError("postData.postView.blogId")
    if url_blog_id is not None and count_blog_id is not None:
        if url_blog_id != count_blog_id:
            raise SourceSchemaError("postData.postCount.blogId")
    if identity is not None:
        return identity
    blog_id = view_blog_id if view_blog_id is not None else count_blog_id
    if blog_id is None or view_post_id is None:
        return None
    return _MobileIdentity(decimal_post_id(blog_id, view_post_id))


def _preflight_detail_identity(
    item: Mapping[str, Any], post: Mapping[str, Any], blog: Mapping[str, Any]
) -> _MobileIdentity | None:
    post_blog_id = _optional_id(post, "blogId", "blogInfo.blogId")
    blog_info_id = _optional_id(blog, "blogId", "blogInfo.blogId")
    if post_blog_id is not None and blog_info_id is not None:
        if post_blog_id != blog_info_id:
            raise SourceSchemaError("blogInfo.blogId")
    identities = [
        identity for identity in (
            _optional_post_url_identity(item.get("permalink", _MISSING)),
            _optional_post_url_identity(item.get("blogPageUrl", _MISSING)),
        ) if identity is not None
    ]
    if len({identity.post_id for identity in identities}) > 1:
        raise SourceSchemaError("post.url")
    numeric_id = _validate_detail_numeric_evidence(
        post, post_blog_id, blog_info_id, identities
    )
    owner = _detail_owner_evidence(blog, identities)
    post_id = identities[0].post_id if identities else numeric_id
    if not post_id:
        return None
    url = _preferred_identity_url(identities)
    return _MobileIdentity(post_id, url, owner)


def _validate_detail_numeric_evidence(
    post: Mapping[str, Any],
    post_blog_id: int | None,
    blog_info_id: int | None,
    identities: list[_MobileIdentity],
) -> str:
    post_id = _optional_alias_id(post, ("id", "postId"), "post.id")
    blog_id = post_blog_id if post_blog_id is not None else blog_info_id
    numeric_id = ""
    if blog_id is not None and post_id is not None:
        numeric_id = decimal_post_id(blog_id, post_id)
    if numeric_id and any(item.post_id != numeric_id for item in identities):
        raise SourceSchemaError("post.id")
    for identity in identities:
        ids = mobile_decimal_ids(identity.post_id)
        if ids and blog_info_id is not None and int(ids[0]) != blog_info_id:
            raise SourceSchemaError("blogInfo.blogId")
    return numeric_id


def _optional_alias_id(
    data: Mapping[str, Any], keys: tuple[str, ...], location: str
) -> int | None:
    values: set[int] = set()
    for key in keys:
        try:
            value = _optional_id(data, key, location)
        except SourceSchemaError:
            raise SourceSchemaError(location) from None
        if value is not None:
            values.add(value)
    if len(values) > 1:
        raise SourceSchemaError(location)
    return next(iter(values), None)


def _detail_owner_evidence(
    blog: Mapping[str, Any], identities: list[_MobileIdentity]
) -> str:
    try:
        identity_owner = consistent_blog_owner(
            *(identity.owner for identity in identities)
        )
    except ValueError:
        raise SourceSchemaError("post.url") from None
    owners = [identity_owner]
    blog_name = blog.get("blogName")
    if isinstance(blog_name, str):
        owners.append(blog_name)
    home = blog.get("homePageUrl")
    if isinstance(home, str):
        home_owner = extract_lofter_username(home)
        if home_owner:
            owners.append(home_owner)
    try:
        return consistent_blog_owner(*owners)
    except ValueError:
        raise SourceSchemaError("blogInfo.blogName") from None


def _preferred_identity_url(identities: list[_MobileIdentity]) -> str:
    if not identities:
        return ""
    owned = next((identity for identity in identities if identity.owner), None)
    return (owned or identities[0]).url


def _attach_identity_witness(
    error: SourceSchemaError | SourceLimitError,
    identity: _MobileIdentity | None,
    source: str,
) -> None:
    location = getattr(error, "location", "")
    if identity is None or location in _MOBILE_ITEM_IDENTITY_LOCATIONS:
        return
    known = {
        field for field, value in (
            ("author_username", identity.owner),
            ("url", identity.url),
        ) if value
    }
    witness = Post(
        post_id=identity.post_id,
        title="",
        summary="",
        author_username=identity.owner,
        url=identity.url,
        **post_field_metadata(f"{source}_identity", known),
    )
    attach_source_evidence(error, (witness,))


def _optional_id(
    data: Mapping[str, Any], key: str, location: str
) -> int | None:
    value = data.get(key, _MISSING)
    if value is _MISSING or value is None:
        return None
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SourceSchemaError(location)
    return value


def _optional_post_url_identity(value: Any) -> _MobileIdentity | None:
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, str):
        raise SourceSchemaError("post.url")
    try:
        value = validate_text_bytes(value, "url", MAX_URL_BYTES)
    except SourceSchemaError:
        raise SourceSchemaError("post.url") from None
    try:
        canonical, post_id, owner = post_url_identity(value)
    except ValueError:
        raise SourceSchemaError("post.url") from None
    return _MobileIdentity(post_id, canonical, owner)


def _canonical_decimal_id(post: Mapping[str, Any], blog: Mapping[str, Any]) -> str:
    post_blog_id = _required_id(post, "blogId")
    if _required_id(blog, "blogId") != post_blog_id:
        raise SourceSchemaError("blogInfo.blogId")
    try:
        return decimal_post_id(post_blog_id, _required_id(post, "id"))
    except ValueError as exc:
        raise SourceSchemaError("post_id") from exc


def _detail_url(item: Mapping[str, Any], post_id: str) -> str:
    permalink = _canonical_post_url(
        _required_string(item, "permalink", MAX_URL_BYTES, "url")
    )
    blog_url = _canonical_post_url(
        _required_string(item, "blogPageUrl", MAX_URL_BYTES, "url")
    )
    if post_id_from_url(permalink) != post_id or post_id_from_url(blog_url) != post_id:
        raise SourceSchemaError("post.url")
    try:
        consistent_blog_owner(
            post_url_identity(permalink)[2],
            post_url_identity(blog_url)[2],
        )
    except ValueError:
        raise SourceSchemaError("post.url") from None
    return permalink


def _canonical_post_url(url: str) -> str:
    try:
        return normalize_post_url(url)
    except ValueError:
        raise SourceSchemaError("url") from None


def _blog_username(blog: Mapping[str, Any], post_url: str) -> str:
    name = _required_string(blog, "blogName", MAX_TITLE_BYTES, "author")
    home = _required_string(blog, "homePageUrl", MAX_URL_BYTES, "url")
    home_name = extract_lofter_username(home)
    post_name = extract_lofter_username(post_url)
    try:
        return consistent_blog_owner(home_name, post_name, name)
    except ValueError:
        raise SourceSchemaError("blogInfo.blogName") from None


def _tags(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        values = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list):
        values = value
    else:
        raise SourceSchemaError("tag")
    return [_bounded_string(item, MAX_TITLE_BYTES, "tags") for item in values]


def _url_list(value: Any, location: str) -> list[str]:
    values = _string_list(value, location)
    result: list[str] = []
    for url in values:
        url = _bounded_string(url, MAX_URL_BYTES, "url")
        try:
            scheme = urlparse(url).scheme
        except ValueError:
            raise SourceSchemaError(location) from None
        if scheme not in {"http", "https"}:
            raise SourceSchemaError(location)
        if url not in result:
            result.append(url)
    return result


def _string_list(value: Any, location: str) -> list[str]:
    if not isinstance(value, list):
        raise SourceSchemaError(location)
    return [_bounded_string(item, MAX_CONTENT_BYTES, "content") for item in value]


def _cursor(value: Any) -> str | None:
    if value == -1:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SourceSchemaError("offset")
    return str(value)


def _publish_time(value: Any) -> str:
    timestamp = format_publish_time(value)
    if not timestamp:
        raise SourceSchemaError("publishTime")
    return timestamp


def _required_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    return _mapping(data.get(key, _MISSING), key)


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SourceSchemaError(location)
    return value


def _required_string(
    data: Mapping[str, Any], key: str, limit: int, location: str
) -> str:
    return _bounded_string(data.get(key, _MISSING), limit, location)


def _optional_string(data: Mapping[str, Any], key: str, limit: int, location: str) -> str:
    value = data.get(key, "")
    return "" if value is None else _bounded_string(value, limit, location)


def _bounded_string(value: Any, limit: int, location: str) -> str:
    if not isinstance(value, str):
        raise SourceSchemaError(location)
    resource = location if location in {"title", "url", "content", "tags"} else "resource"
    try:
        return validate_text_bytes(value, resource, limit)
    except SourceSchemaError:
        raise SourceSchemaError(location) from None


def _required_int(data: Mapping[str, Any], key: str) -> int:
    return _integer(data.get(key, _MISSING), key)


def _required_id(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key, _MISSING)
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    result = _integer(value, key)
    if result < 0:
        raise SourceSchemaError(key)
    return result


def _integer(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourceSchemaError(location)
    return value


def _plain_text(value: str) -> str:
    return html.unescape(_HTML_RE.sub("", value)).strip()


def _summary(value: str) -> str:
    text = _plain_text(value)
    return text[:300] + ("…" if len(text) > 300 else "")
