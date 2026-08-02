from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

_HEX_SLUG = re.compile(r"^([0-9A-Fa-f]+)_([0-9A-Fa-f]+)$")
_POST_PATH = re.compile(r"^/post/(?P<slug>[A-Za-z0-9_-]+)/?$")


def canonical_post_id(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("post_id must be a non-empty string")
    match = _HEX_SLUG.fullmatch(value)
    if match is None:
        return value
    return f"{int(match.group(1), 16):x}_{int(match.group(2), 16):x}"


def decimal_post_id(blog_id: object, post_id: object) -> str:
    blog_value = _non_negative_int(blog_id)
    post_value = _non_negative_int(post_id)
    return f"{blog_value:x}_{post_value:x}"


def post_id_from_url(url: str) -> str:
    try:
        _, slug = _post_url_parts(url)
    except ValueError:
        return ""
    return canonical_post_id(slug)


def canonical_post_url(url: str) -> str:
    canonical, _, _ = post_url_identity(url)
    return canonical


def post_url_identity(url: str) -> tuple[str, str, str]:
    host, slug = _post_url_parts(url)
    post_id = canonical_post_id(slug)
    canonical = urlunsplit(("https", host, f"/post/{post_id}", "", ""))
    return canonical, post_id, _owner_from_host(host)


def consistent_blog_owner(*values: str) -> str:
    owners = [value.strip() for value in values if value.strip()]
    normalized = {value.casefold() for value in owners}
    if len(normalized) > 1:
        raise ValueError("conflicting blog owner")
    return owners[0] if owners else ""


def is_canonical_post_url(url: str) -> bool:
    try:
        return url == canonical_post_url(url)
    except ValueError:
        return False


def mobile_decimal_ids(post_id: str) -> tuple[str, str] | None:
    canonical = canonical_post_id(post_id)
    match = _HEX_SLUG.fullmatch(canonical)
    if match is None:
        return None
    return str(int(match.group(1), 16)), str(int(match.group(2), 16))


def validate_mobile_identity_parts(
    post_id: str,
    *,
    blog_ids: tuple[object, ...] = (),
    post_ids: tuple[object, ...] = (),
) -> None:
    blogs = _identity_values(blog_ids, "blog id")
    posts = _identity_values(post_ids, "post id")
    expected = mobile_decimal_ids(post_id)
    if expected is None:
        return
    if blogs and next(iter(blogs)) != int(expected[0]):
        raise ValueError("conflicting blog id")
    if posts and next(iter(posts)) != int(expected[1]):
        raise ValueError("conflicting post id")


def _identity_values(values: tuple[object, ...], name: str) -> set[int]:
    normalized = {
        _non_negative_int(value) for value in values if value is not None
    }
    if len(normalized) > 1:
        raise ValueError(f"conflicting {name}")
    return normalized


def _post_url_parts(url: str) -> tuple[str, str]:
    if not isinstance(url, str):
        raise ValueError("url must be a string")
    try:
        parsed = urlsplit(url)
        port = parsed.port if parsed.port is not None else 443
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("invalid post url") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    first_party = host == "lofter.com" or host.endswith(".lofter.com")
    match = _POST_PATH.fullmatch(parsed.path)
    invalid = (
        parsed.scheme.lower() != "https"
        or port != 443
        or not first_party
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
        or match is None
    )
    if invalid:
        raise ValueError("invalid post url")
    return host, match.group("slug")


def _owner_from_host(host: str) -> str:
    suffix = ".lofter.com"
    if not host.endswith(suffix):
        return ""
    owner = host[: -len(suffix)]
    if not owner or "." in owner or owner == "www":
        return ""
    return owner


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("identity component must be a non-negative integer")
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    if not isinstance(value, int) or value < 0:
        raise ValueError("identity component must be a non-negative integer")
    return value
