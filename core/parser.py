import asyncio
import html as html_module
import json
import re
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup, Tag
from .errors import (
    SourceChallengeError,
    SourceLimitError,
    SourceSchemaError,
    attach_source_evidence,
    limit_identity_complete,
    mark_limit_identity_complete,
    prepend_source_evidence,
)
from .post_identity import (
    canonical_post_id,
    canonical_post_url,
    consistent_blog_owner,
    decimal_post_id,
    post_id_from_url,
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
POST_FIELDS = frozenset({
    "title", "summary", "content", "images", "tags", "author",
    "author_username", "publish_time", "url",
})


@dataclass
class Post:
    post_id: str
    title: str
    summary: str
    images: list[str] = field(default_factory=list)
    author: str = ""
    author_username: str = ""
    url: str = ""
    tags: list[str] = field(default_factory=list)
    publish_time: str = ""
    content: str = ""
    source: str = "unknown"
    completeness: frozenset[str] | None = None
    provenance: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.post_id = canonical_post_id(self.post_id)
        if self.url and post_id_from_url(self.url):
            self.url = canonical_post_url(self.url)
        if self.completeness is None:
            self.completeness = _inferred_post_fields(self)
        else:
            self.completeness = frozenset(self.completeness)

    def has_fields(self, fields: set[str] | frozenset[str]) -> bool:
        return fields <= self.completeness

    def missing_fields(self, fields: set[str] | frozenset[str]) -> frozenset[str]:
        return frozenset(fields - self.completeness)


def _inferred_post_fields(post: Post) -> frozenset[str]:
    return frozenset(
        field_name
        for field_name in POST_FIELDS
        if bool(getattr(post, field_name))
    )


def post_owner_identity(post: Post) -> str:
    owners: list[str] = []
    if post.has_fields({"url"}) and post.url:
        try:
            _, post_id, owner = post_url_identity(post.url)
        except ValueError:
            raise SourceSchemaError("post.url") from None
        if post_id != post.post_id:
            raise SourceSchemaError("post.id")
        owners.append(owner)
    if post.has_fields({"author_username"}):
        owners.append(post.author_username)
    try:
        return consistent_blog_owner(*owners)
    except ValueError:
        raise SourceSchemaError("post.owner") from None


def post_field_metadata(source: str, fields: set[str]) -> dict[str, object]:
    return {
        "source": source,
        "completeness": frozenset(fields),
        "provenance": {field: source for field in fields},
    }


def _identity_witness(
    post_id: str, url: str, owner: str, source: str
) -> Post:
    known = {"url"} if url else set()
    if owner:
        known.add("author_username")
    return Post(
        post_id=post_id,
        title="",
        summary="",
        author_username=owner,
        url=url,
        **post_field_metadata(source, known),
    )
_TITLE_LIMIT, _URL_LIMIT = MAX_TITLE_BYTES, MAX_URL_BYTES
_CONTENT_LIMIT = MAX_CONTENT_BYTES
_MAX_EMBEDDED_NODES = 100_000
_IMG_CDN_RE = re.compile(
    r'(https://imglf\d+\.lf127\.net/img/[A-Za-z0-9/_=.+%-]+\.(?:jpg|png|gif|webp)'
    r'\?[^"\'<>\s]*quality=[^"\'<>\s]*)'
)
_INITIALIZE_RE = re.compile(
    r"\A\s*window\.__initialize_data__\s*=\s*(.+?)\s*;?\s*\Z", re.DOTALL
)
_BODY_SELECTORS = ["div.txtcont", "div.ct"]
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_EMPTY_BLOG_MARKERS = ("没有帖子", "暂无帖子", "暂无文章", "还没有发布", "暂未发布")
_CHALLENGE_TITLES = ("登录", "登陆", "安全验证", "访问验证", "验证码", "captcha", "challenge", "verify", "access denied")
def _check_limit(value: str, resource: str, limit: int) -> str:
    return validate_text_bytes(value, resource, limit)
def _make_soup(html: str) -> BeautifulSoup:
    if not isinstance(html, str):
        raise SourceSchemaError("html")
    return BeautifulSoup(html, "lxml")
def _extract_post_id_from_url(url: str) -> str:
    return post_id_from_url(url)
def _is_lofter_post_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        raise SourceSchemaError("post.url") from None
    is_lofter = host == "lofter.com" or host.endswith(".lofter.com")
    return parsed.scheme.lower() in {"http", "https"} and is_lofter and bool(
        _extract_post_id_from_url(url)
    )
def extract_lofter_username(url: str) -> str:
    if not isinstance(url, str):
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        raise SourceSchemaError("url") from None
    if parsed.scheme.lower() not in {"http", "https"}:
        return ""
    host = parsed.netloc
    if not host or "@" in host or host.startswith("["):
        return ""
    if ":" in host:
        host = host.rsplit(":", 1)[0]
    suffix = ".lofter.com"
    if not host.lower().endswith(suffix):
        return ""
    username = host[: -len(suffix)]
    if not username or "." in username or username.lower() == "www":
        return ""
    return username
def _looks_like_challenge(soup: BeautifulSoup) -> bool:
    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    if any(marker in title for marker in _CHALLENGE_TITLES):
        return True
    if soup.select_one("input[type='password'], [id*='captcha' i], [class*='captcha' i]"):
        return True
    for form in soup.find_all("form"):
        action = form.get("action")
        if isinstance(action, str) and any(
            marker in action.lower() for marker in ("login", "signin", "verify", "captcha")
        ):
            return True
    return False
def _raise_if_challenge(soup: BeautifulSoup) -> None:
    if _looks_like_challenge(soup):
        raise SourceChallengeError()
def _extract_body_field(soup: BeautifulSoup) -> tuple[str, bool]:
    paragraphs = soup.find_all(
        "p", id=lambda value: value and value.startswith("p_")
    )
    if paragraphs:
        raw = "\n".join(paragraph.get_text() for paragraph in paragraphs)
        return _MULTI_NEWLINE_RE.sub("\n\n", raw).strip(), True
    for selector in _BODY_SELECTORS:
        element = soup.select_one(selector)
        if element and len(element.get_text(strip=True)) > 50:
            raw = element.get_text(separator="\n").strip()
            return _MULTI_NEWLINE_RE.sub("\n\n", raw), True
    return "", False


def _extract_body_text(soup: BeautifulSoup) -> str:
    return _extract_body_field(soup)[0]
def _blog_base_url(soup: BeautifulSoup) -> str:
    for link in soup.select("link[rel~='canonical']"):
        href = link.get("href")
        if not isinstance(href, str):
            continue
        _check_limit(href, "url", _URL_LIMIT)
        username = extract_lofter_username(href)
        if username and not _extract_post_id_from_url(href):
            return f"https://{username}.lofter.com/"
    return ""
def _blog_declared_owners(soup: BeautifulSoup) -> list[str]:
    owners: list[str] = []
    for element in soup.select("[data-blog-name]"):
        value = element.get("data-blog-name")
        if isinstance(value, str) and value.strip():
            owners.append(value.strip())
    return owners


def _blog_has_identity(soup: BeautifulSoup) -> bool:
    if soup.select_one("[data-blog-name], [data-blog-id], #blogInfo, .bloginfo, .blog-info"):
        return True
    return bool(_blog_base_url(soup))
def _is_explicit_empty_blog(soup: BeautifulSoup) -> bool:
    text = " ".join(soup.get_text(" ", strip=True).split())
    return any(marker in text for marker in _EMPTY_BLOG_MARKERS)
def _parse_blog_posts_sync(
    html: str, expected_owner: str | None = None
) -> list[Post]:
    soup = _make_soup(html)
    _raise_if_challenge(soup)
    posts: list[Post] = []
    seen: set[str] = set()
    base_url = _blog_base_url(soup)
    owners = [
        expected_owner or "",
        extract_lofter_username(base_url),
        *_blog_declared_owners(soup),
    ]
    trusted_owner = _validate_blog_owners(owners)
    if not base_url and trusted_owner:
        base_url = _trusted_blog_base(trusted_owner)
    for anchor in soup.select("a[href*='/post/']"):
        post = _blog_anchor_post(
            anchor, base_url, owners, trusted_owner, seen, posts
        )
        if post is None:
            continue
        seen.add(post.post_id)
        posts.append(post)
    if not (posts or _blog_has_identity(soup) or _is_explicit_empty_blog(soup)):
        raise SourceSchemaError("blog")
    return posts


def _blog_anchor_post(
    anchor: object,
    base_url: str,
    owners: list[str],
    trusted_owner: str,
    seen: set[str],
    posts: list[Post],
) -> Post | None:
    if not isinstance(anchor, Tag):
        return None
    href = anchor.get("href")
    if not isinstance(href, str):
        return None
    _check_limit(href, "url", _URL_LIMIT)
    post_url = urljoin(base_url, href) if base_url else href
    post_url = _validated_blog_post_url(post_url, owners)
    post_id = _extract_post_id_from_url(post_url)
    if post_id in seen:
        return None
    username = extract_lofter_username(post_url) or trusted_owner
    witness = _identity_witness(
        post_id, post_url, username, "html_blog_identity"
    )
    try:
        if len(posts) >= MAX_ITEMS:
            raise SourceLimitError("items", MAX_ITEMS)
        title = _check_limit(
            anchor.get_text(strip=True), "title", _TITLE_LIMIT
        )
    except SourceLimitError as exc:
        attach_source_evidence(exc, (witness,))
        prepend_source_evidence(exc, posts)
        mark_limit_identity_complete(exc)
        raise
    known = {"url"}
    if username:
        known.add("author_username")
    return Post(
        post_id=post_id,
        title=title,
        summary="",
        url=post_url,
        author_username=username,
        **post_field_metadata("html_blog", known),
    )


def _validated_blog_post_url(post_url: str, owners: list[str]) -> str:
    _check_limit(post_url, "url", _URL_LIMIT)
    try:
        canonical = canonical_post_url(post_url)
    except ValueError:
        raise SourceSchemaError("blog") from None
    username = extract_lofter_username(canonical)
    if username:
        owners.append(username)
        _validate_blog_owners(owners)
    return canonical


def _trusted_blog_base(owner: str) -> str:
    if not re.fullmatch(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)", owner):
        raise SourceSchemaError("post.owner")
    return f"https://{owner.lower()}.lofter.com/"


def _validate_blog_owners(owners: list[str]) -> str:
    try:
        return consistent_blog_owner(*owners)
    except ValueError:
        raise SourceSchemaError("post.owner") from None


async def parse_blog_posts(
    html: str, *, expected_owner: str | None = None
) -> list[Post]:
    """解析博主主页；只有可识别博客或明确空态可返回空列表。"""
    return await asyncio.get_running_loop().run_in_executor(
        None, _parse_blog_posts_sync, html, expected_owner
    )
def _canonical_post_id(value: str) -> str:
    try:
        return canonical_post_id(value)
    except ValueError as exc:
        raise SourceSchemaError("post.id") from exc

def _expected_post_id(url: str, expected_post_id: str | None) -> str:
    value = expected_post_id or _extract_post_id_from_url(url)
    return _canonical_post_id(value)

def _post_evidence(soup: BeautifulSoup) -> tuple[str, str]:
    urls: list[str] = []
    ids: set[str] = set()
    owners: list[str] = []
    selectors = "link[rel~='canonical'], meta[property='og:url'], meta[name='og:url']"
    for element in soup.select(selectors):
        value = element.get("href") or element.get("content")
        if not isinstance(value, str) or not _is_lofter_post_url(value):
            continue
        value = _check_limit(value, "url", _URL_LIMIT)
        try:
            canonical, post_id, owner = post_url_identity(value)
        except ValueError:
            raise SourceSchemaError("post.evidence") from None
        urls.append(canonical)
        ids.add(post_id)
        owners.append(owner)
    for element in soup.select("[data-post-id]"):
        value = element.get("data-post-id")
        if isinstance(value, str) and value:
            ids.add(_canonical_post_id(value))
    try:
        evidence_owner = consistent_blog_owner(*owners)
    except ValueError:
        raise SourceSchemaError("post.evidence") from None
    if not ids:
        raise SourceSchemaError("html")
    if len(ids) > 1:
        raise SourceSchemaError("post.evidence")
    resolved_url = next(
        (value for value in urls if post_url_identity(value)[2] == evidence_owner),
        urls[0] if urls else "",
    )
    return ids.pop(), resolved_url
def _extract_title(soup: BeautifulSoup) -> tuple[str, str, bool]:
    if not soup.title:
        return "", "", False
    raw_title = soup.title.get_text(" ", strip=True)
    _check_limit(raw_title, "title", _TITLE_LIMIT)
    if "-" not in raw_title:
        return raw_title, "", False
    title, author = raw_title.rsplit("-", 1)
    author = author.strip()
    return title.strip(), author, bool(author)
def _extract_meta(soup: BeautifulSoup) -> tuple[str, list[str], bool, bool]:
    summary = ""
    tags: list[str] = []
    summary_known = False
    tags_known = False
    for meta in soup.find_all("meta"):
        name = meta.get("name")
        if not isinstance(name, str):
            continue
        value = meta.get("content")
        if value is None:
            continue
        if not isinstance(value, str):
            raise SourceSchemaError(f"post.meta.{name.lower()}")
        if name.lower() == "description":
            text = html_module.unescape(_check_limit(value, "content", _CONTENT_LIMIT)).strip()
            summary = text[:300] + ("…" if len(text) > 300 else "")
            summary_known = True
        elif name.lower() == "keywords":
            tags = [tag.strip() for tag in value.split(",") if tag.strip()]
            tags_known = True
    return summary, tags, summary_known, tags_known
def _extract_images_from_html(html: str) -> list[str]:
    images: list[str] = []
    seen: set[str] = set()
    for raw_url in _IMG_CDN_RE.findall(html):
        _check_limit(raw_url, "url", _URL_LIMIT)
        image_url = raw_url.split("?", 1)[0]
        if image_url not in seen:
            seen.add(image_url)
            images.append(image_url)
    return images
def _html_known_fields(
    resolved_url: str, *, content: bool, images: bool, title: bool,
    summary: bool, tags: bool, author: bool,
) -> set[str]:
    known = {"url"}
    flags = {
        "content": content, "images": images, "title": title,
        "summary": summary, "tags": tags, "author": author,
        "author_username": bool(extract_lofter_username(resolved_url)),
    }
    known.update(field for field, complete in flags.items() if complete)
    return known
def _html_post_identity(
    soup: BeautifulSoup, url: str, expected: str
) -> tuple[str, str, str]:
    evidence_id, evidence_url = _post_evidence(soup)
    if evidence_id != expected:
        raise SourceSchemaError("post.id")
    try:
        resolved_url, _, resolved_owner = post_url_identity(evidence_url or url)
        request_owner = post_url_identity(url)[2]
        owner = consistent_blog_owner(resolved_owner, request_owner)
    except ValueError:
        raise SourceSchemaError("post.evidence") from None
    return evidence_id, resolved_url, owner


def _parse_post_page_sync(
    html: str, url: str, expected_post_id: str | None = None
) -> Post:
    if not isinstance(url, str):
        raise SourceSchemaError("post.url")
    _check_limit(url, "url", _URL_LIMIT)
    expected = _expected_post_id(url, expected_post_id)
    soup = _make_soup(html)
    _raise_if_challenge(soup)
    evidence_id, resolved_url, owner = _html_post_identity(soup, url, expected)
    witness = _identity_witness(
        evidence_id, resolved_url, owner, "html_post_identity"
    )
    try:
        title, author, author_known = _extract_title(soup)
        title_known = soup.title is not None
        summary, tags, summary_known, tags_known = _extract_meta(soup)
        images = _extract_images_from_html(html)
        content, content_known = _extract_body_field(soup)
        content = _check_limit(content, "content", _CONTENT_LIMIT)
    except SourceLimitError as exc:
        attach_source_evidence(exc, (witness,))
        mark_limit_identity_complete(exc)
        raise
    if not any((title, summary, tags, images, content)):
        raise SourceSchemaError("post.content")
    known = _html_known_fields(
        resolved_url,
        content=content_known,
        images=bool(images),
        title=title_known,
        summary=summary_known,
        tags=tags_known,
        author=author_known,
    )
    return Post(
        post_id=evidence_id,
        title=title,
        author=author,
        summary=summary,
        tags=tags,
        images=images,
        url=resolved_url,
        author_username=extract_lofter_username(resolved_url),
        content=content,
        **post_field_metadata("html_post", known),
    )
async def parse_post_page(
    html: str, url: str, *, expected_post_id: str | None = None
) -> Post:
    """从严格校验后的单篇帖子 HTML 提取结构化内容。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _parse_post_page_sync, html, url, expected_post_id
    )
def extract_initialize_data(html: str) -> dict[str, object]:
    """提取纯 window.__initialize_data__ JSON 赋值，不执行 JavaScript。"""
    soup = _make_soup(html)
    _raise_if_challenge(soup)
    scripts = [
        script.get_text()
        for script in soup.find_all("script")
        if "window.__initialize_data__" in script.get_text()
    ]
    if len(scripts) != 1:
        raise SourceSchemaError("embedded.assignment")
    match = _INITIALIZE_RE.fullmatch(scripts[0])
    if not match:
        raise SourceSchemaError("embedded.assignment")
    try:
        data = json.loads(match.group(1))
    except (TypeError, ValueError, RecursionError):
        raise SourceSchemaError("embedded.json") from None
    if not isinstance(data, dict):
        raise SourceSchemaError("embedded.root")
    return data
def _walk_mappings(root: dict[str, object]):
    queue: deque[object] = deque([root])
    seen: set[int] = set()
    visited = 0
    while queue:
        value = queue.popleft()
        if isinstance(value, (dict, list)):
            if id(value) in seen:
                continue
            seen.add(id(value))
            visited += 1
            if visited > _MAX_EMBEDDED_NODES:
                raise SourceLimitError("items", _MAX_EMBEDDED_NODES)
        if isinstance(value, dict):
            yield value
            queue.extend(value.values())
        elif isinstance(value, list):
            queue.extend(value)
def _embedded_urls(
    item: dict[str, object], *, strict: bool
) -> list[tuple[str, str, str]]:
    keys = ("blogPageUrl", "postUrl", "permalink")
    aliases: list[tuple[str, str]] = []
    for key in keys:
        if key not in item or item[key] is None:
            continue
        value = item[key]
        if not isinstance(value, str):
            raise SourceSchemaError(f"embedded.{key}")
        aliases.append((key, value))
    if any(not value for _, value in aliases) and any(value for _, value in aliases):
        raise SourceSchemaError("post.evidence")
    result: list[tuple[str, str, str]] = []
    for key, value in aliases:
        if not value:
            continue
        _check_limit(value, "url", _URL_LIMIT)
        try:
            result.append(post_url_identity(value))
        except ValueError:
            if strict:
                raise SourceSchemaError(f"embedded.{key}") from None
    return result


def _embedded_identity(item: dict[str, object]) -> str:
    urls = _embedded_urls(item, strict=False)
    url_ids = {identity for _, identity, _ in urls}
    if len(url_ids) > 1:
        raise SourceSchemaError("embedded.post.id")
    url_id = next(iter(url_ids), "")
    blog_ids, post_ids = _embedded_numeric_parts(item)
    if url_id:
        try:
            validate_mobile_identity_parts(
                url_id, blog_ids=blog_ids, post_ids=post_ids
            )
        except ValueError:
            raise SourceSchemaError("embedded.post.id") from None
        return url_id
    return _embedded_numeric_identity(blog_ids, post_ids)


def _embedded_numeric_parts(
    item: dict[str, object]
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    blog = item.get("blogInfo")
    structured_id = blog.get("blogId") if isinstance(blog, dict) else None
    blog_ids = (structured_id, item.get("blogId"))
    post_ids = (item.get("postId"), item.get("id"))
    _validate_numeric_aliases(blog_ids, is_blog=True)
    _validate_numeric_aliases(post_ids, is_blog=False)
    return blog_ids, post_ids


def _validate_numeric_aliases(
    values: tuple[object, ...], *, is_blog: bool
) -> None:
    normalized: set[str] = set()
    try:
        for value in values:
            if value is None:
                continue
            normalized.add(
                decimal_post_id(value, 0)
                if is_blog else decimal_post_id(0, value)
            )
    except ValueError:
        raise SourceSchemaError("embedded.post.id") from None
    if len(normalized) > 1:
        raise SourceSchemaError("embedded.post.id")


def _embedded_numeric_identity(
    blog_ids: tuple[object, ...], post_ids: tuple[object, ...]
) -> str:
    blog_id = next((value for value in blog_ids if value is not None), None)
    post_id = next((value for value in post_ids if value is not None), None)
    try:
        return decimal_post_id(blog_id, post_id)
    except ValueError:
        raise SourceSchemaError("embedded.post.id") from None
def _embedded_candidate_owners(item: dict[str, object]) -> list[str]:
    owners = [owner for _, _, owner in _embedded_urls(item, strict=False)]
    blog = item.get("blogInfo")
    if isinstance(blog, dict) and isinstance(blog.get("blogName"), str):
        owners.append(blog["blogName"])
    return owners


def _embedded_candidate_ids(item: dict[str, object]) -> set[str]:
    identities: set[str] = set()
    for key in ("blogPageUrl", "postUrl", "permalink"):
        value = item.get(key)
        if not isinstance(value, str) or not value:
            continue
        try:
            identities.add(post_url_identity(value)[1])
        except ValueError:
            continue
    blog = item.get("blogInfo")
    structured_id = blog.get("blogId") if isinstance(blog, dict) else None
    for blog_id in (structured_id, item.get("blogId")):
        for post_id in (item.get("postId"), item.get("id")):
            try:
                identities.add(decimal_post_id(blog_id, post_id))
            except ValueError:
                continue
    return identities


def _find_embedded_posts(
    data: dict[str, object], expected_post_id: str
) -> list[dict[str, object]]:
    matches: list[dict[str, object]] = []
    owners: list[str] = []
    for item in _walk_mappings(data):
        try:
            identity = _embedded_identity(item)
        except SourceSchemaError:
            if expected_post_id in _embedded_candidate_ids(item):
                raise
            continue
        if identity != expected_post_id:
            continue
        owners.extend(_embedded_candidate_owners(item))
        if len(matches) >= MAX_ITEMS:
            raise SourceLimitError("items", MAX_ITEMS)
        matches.append(item)
    if not matches:
        raise SourceSchemaError("embedded.post")
    try:
        consistent_blog_owner(*owners)
    except ValueError:
        raise SourceSchemaError("embedded.post.owner") from None
    return matches
def _optional_text(
    item: dict[str, object], keys: tuple[str, ...], resource: str, limit: int
) -> tuple[str, bool]:
    aliases: list[str] = []
    for key in keys:
        if key not in item or item[key] is None:
            continue
        value = item[key]
        if not isinstance(value, str):
            raise SourceSchemaError(f"embedded.{key}")
        aliases.append(_plain_text(_check_limit(value, resource, limit)))
    if not aliases:
        return "", False
    if any(value != aliases[0] for value in aliases[1:]):
        raise SourceSchemaError("post.evidence")
    return aliases[0], True
def _plain_text(value: str) -> str:
    return BeautifulSoup(value, "lxml").get_text("\n", strip=True)
def _extract_embedded_tags(item: dict[str, object]) -> tuple[list[str], bool]:
    aliases = [
        _embedded_tag_value(item[key])
        for key in ("tags", "tag")
        if key in item and item[key] is not None
    ]
    if not aliases:
        return [], False
    expected = {tag.casefold() for tag in aliases[0]}
    if any({tag.casefold() for tag in tags} != expected for tags in aliases[1:]):
        raise SourceSchemaError("embedded.tags")
    return aliases[0], True


def _embedded_tag_value(value: object) -> list[str]:
    if isinstance(value, str):
        return [tag.strip() for tag in value.split(",") if tag.strip()]
    if not isinstance(value, list):
        raise SourceSchemaError("embedded.tags")
    tags: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            tag = entry.strip()
        elif isinstance(entry, dict) and isinstance(entry.get("tagName"), str):
            tag = entry["tagName"].strip()
        else:
            raise SourceSchemaError("embedded.tags[]")
        if tag:
            tags.append(tag)
    return tags
def _extract_embedded_images(
    item: dict[str, object]
) -> tuple[list[str], bool]:
    aliases = [
        _embedded_image_value(item[key])
        for key in ("images", "photoLinks", "firstImageUrl")
        if key in item and item[key] is not None
    ]
    if not aliases:
        return [], False
    expected = tuple(aliases[0])
    if any(tuple(images) != expected for images in aliases[1:]):
        raise SourceSchemaError("post.evidence")
    return aliases[0], True


def _embedded_image_value(value: object) -> list[str]:
    if isinstance(value, str) and value.lstrip().startswith("["):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            raise SourceSchemaError("embedded.images") from None
    entries = value if isinstance(value, list) else [value]
    images: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if isinstance(entry, dict):
            entry = next(
                (entry[key] for key in ("origin", "orign", "raw", "url", "middle") if isinstance(entry.get(key), str)),
                None,
            )
        if not isinstance(entry, str):
            raise SourceSchemaError("embedded.images[]")
        image_url = _check_limit(entry, "url", _URL_LIMIT).strip().split("?", 1)[0]
        if image_url and image_url not in seen:
            seen.add(image_url)
            images.append(image_url)
    return images
def _embedded_content_fields(
    item: dict[str, object],
) -> tuple[str, str, str, list[str], list[str], set[str]]:
    title, title_known = _optional_text(
        item, ("title",), "title", _TITLE_LIMIT
    )
    raw_summary, summary_known = _optional_text(
        item, ("dirContent", "description", "digest"),
        "content", _CONTENT_LIMIT,
    )
    raw_content, content_known = _optional_text(
        item, ("content", "postContent"), "content", _CONTENT_LIMIT
    )
    summary_text = raw_summary
    summary = summary_text[:300] + ("…" if len(summary_text) > 300 else "")
    content = raw_content
    tags, tags_known = _extract_embedded_tags(item)
    images, images_known = _extract_embedded_images(item)
    flags = (
        ("title", title_known),
        ("summary", summary_known),
        ("content", content_known),
        ("images", images_known),
        ("tags", tags_known),
    )
    known = {field for field, complete in flags if complete}
    return title, summary, content, tags, images, known


def _embedded_author_fields(
    item: dict[str, object], resolved_url: str
) -> tuple[str, str, set[str]]:
    blog = item.get("blogInfo") or {}
    if not isinstance(blog, dict):
        raise SourceSchemaError("embedded.blogInfo")
    nickname = blog.get("blogNickName")
    if nickname is not None and not isinstance(nickname, str):
        raise SourceSchemaError("embedded.blogInfo")
    author = nickname or ""
    blog_name = blog.get("blogName")
    if blog_name is not None and not isinstance(blog_name, str):
        raise SourceSchemaError("embedded.blogInfo")
    username = extract_lofter_username(resolved_url) or blog_name or ""
    known = {"author"} if isinstance(nickname, str) else set()
    if username:
        known.add("author_username")
    return author, username, known


def _embedded_publish_time(item: dict[str, object]) -> tuple[str, bool]:
    if "publishTime" not in item or item["publishTime"] is None:
        return "", False
    value = format_publish_time(item["publishTime"])
    return value, bool(value)


def _resolve_embedded_url(
    item: dict[str, object], request_url: str
) -> tuple[str, str]:
    urls = _embedded_urls(item, strict=True)
    request = post_url_identity(request_url)
    owners = [identity[2] for identity in urls]
    owners.append(request[2])
    blog = item.get("blogInfo")
    if isinstance(blog, dict) and isinstance(blog.get("blogName"), str):
        owners.append(blog["blogName"])
    try:
        consistent_blog_owner(*owners)
    except ValueError:
        raise SourceSchemaError("embedded.post.owner") from None
    identities = (*urls, request)
    resolved = next(
        (identity[0] for identity in identities if identity[2]), identities[0][0]
    )
    return resolved, request[1]


def _parse_embedded_item(
    item: dict[str, object], url: str, expected: str
) -> Post:
    resolved_url, request_id = _resolve_embedded_url(item, url)
    post_id = _embedded_identity(item)
    if post_id != expected or request_id != expected:
        raise SourceSchemaError("embedded.post.id")
    author, username, author_known = _embedded_author_fields(item, resolved_url)
    witness = _identity_witness(
        post_id, resolved_url, username, "embedded_json_identity"
    )
    try:
        title, summary, content, tags, images, known = _embedded_content_fields(item)
        publish_time, time_known = _embedded_publish_time(item)
    except SourceLimitError as exc:
        attach_source_evidence(exc, (witness,))
        mark_limit_identity_complete(exc)
        raise
    known.update(author_known | {"url"})
    if time_known:
        known.add("publish_time")
    return Post(
        post_id=post_id, title=title, summary=summary, images=images,
        author=author, author_username=username, url=resolved_url,
        tags=tags, content=content, publish_time=publish_time,
        **post_field_metadata("embedded_json", known),
    )


def _has_embedded_content(post: Post) -> bool:
    return any((post.title, post.summary, post.content, post.tags, post.images))


def _merge_embedded_posts(posts: list[Post]) -> Post:
    from .post_fields import merge_post_fields, validate_post_evidence

    validate_post_evidence(posts)
    usable = [post for post in posts if _has_embedded_content(post)]
    if not usable:
        raise SourceSchemaError("embedded.post.content")
    result = max(usable, key=lambda post: len(post.completeness))
    for post in posts:
        if post is not result:
            result = merge_post_fields(result, post)
    return result


def parse_embedded_post(
    html: str, url: str, *, expected_post_id: str | None = None
) -> Post:
    """从页面内嵌 JSON 映射单篇 Post；不会求值任何 JavaScript。"""
    if not isinstance(url, str):
        raise SourceSchemaError("embedded.url")
    _check_limit(url, "url", _URL_LIMIT)
    expected = _expected_post_id(url, expected_post_id)
    candidates = _find_embedded_posts(extract_initialize_data(html), expected)
    posts: list[Post] = []
    try:
        for item in candidates:
            posts.append(_parse_embedded_item(item, url, expected))
    except SourceLimitError as exc:
        if limit_identity_complete(exc):
            prepend_source_evidence(exc, posts)
        raise
    except Exception as exc:
        attach_source_evidence(exc, posts)
        raise
    return _merge_embedded_posts(posts)
