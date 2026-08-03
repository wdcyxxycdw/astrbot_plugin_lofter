import json

import pytest

from core.dwr_parser import _map_post, parse_dwr_response, parse_dwr_response_result
from core.errors import (
    DWRIdentityError,
    SourceChallengeError,
    SourceLimitError,
    SourceSchemaError,
)
from core.source_limits import MAX_CONTENT_BYTES, MAX_TITLE_BYTES, MAX_URL_BYTES


def _exact_utf8(prefix: str, limit: int) -> str:
    remaining = limit - len(prefix.encode("utf-8"))
    return prefix + "界" * (remaining // 3) + "x" * (remaining % 3)


_EXACT_TITLE = _exact_utf8("", MAX_TITLE_BYTES)
_POST_URL_PREFIX = "https://user.lofter.com/post/"
_EXACT_POST_URL = _POST_URL_PREFIX + "a" * (
    MAX_URL_BYTES - len(_POST_URL_PREFIX)
)
_EXACT_IMAGE_URL = _exact_utf8("https://img.example/a.jpg?", MAX_URL_BYTES)
_EXACT_CONTENT = _exact_utf8("", MAX_CONTENT_BYTES)


def test_dwr_invalid_optional_values_remain_unknown():
    mapped = _map_post({
        "post": {
            "blogPageUrl": "https://user.lofter.com/post/abc_123",
            "title": "Demo",
            "tag": None,
            "publishTime": 0,
            "firstImageUrl": None,
            "blogInfo": {
                "blogNickName": None,
                "blogName": None,
            },
        }
    })

    assert mapped is not None
    assert mapped.completeness == frozenset({
        "title", "url", "author_username",
    })
    assert mapped.publish_time == ""


@pytest.mark.asyncio
async def test_parse_dwr_response_accepts_valid_empty_result():
    body = 'dwr.engine._remoteHandleCallback("0", "0", []);'

    result = await parse_dwr_response_result(body)

    assert result.posts == []
    assert result.mapped_count == 0
    assert result.dropped_count == 0
    assert result.is_empty is True
    assert result.exhausted is True
    assert result.complete is True
    assert await parse_dwr_response(body) == []


@pytest.mark.asyncio
async def test_parse_dwr_response_reports_partial_malformed_items():
    body = """
    dwr.engine._remoteHandleCallback("0", "0", [
        null,
        "bad",
        1,
        {
            post: {
                blogPageUrl: "https://someuser.lofter.com/post/abc_123",
                title: "有效帖子",
                dirContent: "<p>正文</p>",
                blogInfo: {blogNickName: "作者", blogName: "someuser"},
                tag: "tag-a, tag-b",
                publishTime: 1710000000000,
                firstImageUrl: '["https://img.example/a.jpg?x=1"]'
            }
        }
    ]);
    """

    result = await parse_dwr_response_result(body)

    assert result.mapped_count == 1
    assert result.dropped_count == 3
    assert result.is_empty is False
    assert result.exhausted is None
    assert result.complete is False
    assert result.posts[0].post_id == "abc_123"
    assert result.posts[0].title == "有效帖子"
    assert result.posts[0].summary == "正文"
    assert "summary" in result.posts[0].completeness
    assert "content" not in result.posts[0].completeness
    assert result.posts[0].author == "作者"
    assert result.posts[0].author_username == "someuser"
    assert result.posts[0].tags == ["tag-a", "tag-b"]
    assert result.posts[0].images == ["https://img.example/a.jpg"]
    assert "images" not in result.posts[0].completeness


@pytest.mark.asyncio
async def test_unknown_publish_time_preserves_remote_order():
    body = """
    dwr.engine._remoteHandleCallback("0", "0", [
        {post: {
            blogPageUrl: "https://user.lofter.com/post/abc_1",
            title: "远端第一条",
            publishTime: 0
        }},
        {post: {
            blogPageUrl: "https://user.lofter.com/post/abc_2",
            title: "远端第二条",
            publishTime: 1710000000000
        }}
    ]);
    """

    posts = await parse_dwr_response(body)

    assert [post.title for post in posts] == ["远端第一条", "远端第二条"]


@pytest.mark.asyncio
async def test_parse_dwr_response_keeps_list_compatibility_for_partial_result():
    body = """
    dwr.engine._remoteHandleCallback("0", "0", [
        null,
        {post: {blogPageUrl: "https://user.lofter.com/post/abc_123"}}
    ]);
    """

    posts = await parse_dwr_response(body)

    assert [post.post_id for post in posts] == ["abc_123"]


@pytest.mark.asyncio
async def test_parse_dwr_response_uses_blog_name_when_url_has_no_lofter_username():
    body = """
    dwr.engine._remoteHandleCallback("0", "0", [
        {
            post: {
                blogPageUrl: "https://lofter.com/post/abc_123",
                title: "有效帖子",
                blogInfo: {blogNickName: "作者", blogName: "fallbackuser"},
                publishTime: 1710000000000
            }
        }
    ]);
    """

    posts = await parse_dwr_response(body)

    assert len(posts) == 1
    assert posts[0].author_username == "fallbackuser"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.example/post/abc_123",
        "https://evillofter.com/post/abc_123",
        "http://user.lofter.com/post/abc_123",
        "https://name:secret@user.lofter.com/post/abc_123",
        "https://user.lofter.com:444/post/abc_123",
    ],
)
async def test_parse_dwr_response_rejects_non_first_party_post_url(url):
    body = (
        'dwr.engine._remoteHandleCallback("0", "0", '
        f'[{{post: {{blogPageUrl: {json.dumps(url)}}}}}]);'
    )

    with pytest.raises(SourceSchemaError) as exc_info:
        await parse_dwr_response(body)

    assert exc_info.value.location == "dwr.items"


@pytest.mark.asyncio
async def test_parse_dwr_response_uses_explicit_default_https_port():
    body = """
    dwr.engine._remoteHandleCallback("0", "0", [
        {post: {blogPageUrl: "https://user.lofter.com:443/post/abc_123"}}
    ]);
    """

    posts = await parse_dwr_response(body)

    assert [post.post_id for post in posts] == ["abc_123"]


@pytest.mark.parametrize(
    ("post_url", "permalink", "reason"),
    [
        (
            "https://alice.lofter.com/post/1a_2b",
            "https://alice.lofter.com/post/1a_2c",
            "post_url_conflict",
        ),
        (
            "https://alice.lofter.com/post/1a_2b",
            "https://bob.lofter.com/post/1a_2b",
            "owner_conflict",
        ),
    ],
)
def test_dwr_rejects_authoritative_url_conflict(
    post_url, permalink, reason
):
    with pytest.raises(DWRIdentityError) as exc_info:
        _map_post({
            "post": {
                "postUrl": post_url,
                "permalink": permalink,
            }
        })

    assert exc_info.value.location == "dwr.post.id"
    assert exc_info.value.reason == reason
    assert exc_info.value.fields == ("permalink", "postUrl")


@pytest.mark.parametrize("value", [1, "https://attacker.example/post/1a_2b"])
def test_dwr_rejects_invalid_authoritative_url(value):
    with pytest.raises(DWRIdentityError) as exc_info:
        _map_post({"post": {"postUrl": value}})

    assert exc_info.value.reason in {
        "invalid_identity_type", "invalid_post_url",
    }
    assert exc_info.value.fields == ("postUrl",)


@pytest.mark.parametrize(
    ("value", "shape"),
    [
        ("", "empty"),
        ("   ", "blank"),
        (" 1a_2b ", "surrounding_whitespace"),
        ("/post/1a_2b", "relative_post_path"),
        ("post/1a_2b", "relative_post_path"),
        ("//demo.lofter.com/post/1a_2b", "protocol_relative"),
        ("http://demo.lofter.com/post/1a_2b", "http_url"),
        ("https://demo.lofter.com/archive", "first_party_non_post_url"),
        (
            "https://demo.lofter.com/post/1a_2b?token=private-token",
            "first_party_post_url_with_query",
        ),
        (
            "https://demo.lofter.com/post/1a_2b#private-fragment",
            "first_party_post_url_with_fragment",
        ),
        (
            "https://name:private-password@demo.lofter.com/post/1a_2b",
            "first_party_post_url_invalid_authority",
        ),
        ("https://attacker.example/post/1a_2b", "external_https_url"),
        ("mailto:private@example.com", "other_url"),
        ("https://[invalid", "malformed_url"),
        ("opaque private text", "text"),
    ],
)
def test_dwr_invalid_permalink_reports_only_safe_value_shape(value, shape):
    with pytest.raises(DWRIdentityError) as exc_info:
        _map_post({"post": {"permalink": value}})

    error = exc_info.value
    assert error.reason == "invalid_post_url"
    assert error.fields == ("permalink",)
    assert error.fingerprint == "invalid_post_url:permalink"
    assert error.value_shape == shape
    assert error.diagnostic == f"invalid_post_url:permalink;shape={shape}"
    if value:
        assert value not in str(error)
        assert value not in error.diagnostic


def test_dwr_accepts_permalink_slug_as_post_id_evidence():
    mapped = _map_post({
        "post": {
            "postUrl": "https://alice.lofter.com/post/001A_00002B/",
            "permalink": "1a_2b",
            "postId": 43,
            "blogId": 26,
            "blogInfo": {"blogId": 26, "blogName": "alice"},
        }
    })

    assert mapped is not None
    assert mapped.post_id == "1a_2b"
    assert mapped.url == "https://alice.lofter.com/post/1a_2b"
    assert mapped.author_username == "alice"


def test_dwr_accepts_permalink_slug_without_full_post_url():
    mapped = _map_post({
        "post": {
            "permalink": "001A_00002B",
            "postId": 43,
            "blogId": 26,
            "blogInfo": {"blogId": 26, "blogName": "alice"},
        }
    })

    assert mapped is not None
    assert mapped.post_id == "1a_2b"
    assert mapped.url == "https://lofter.com/post/1a_2b"
    assert mapped.author_username == "alice"


def test_dwr_rejects_permalink_slug_conflicting_with_post_url():
    with pytest.raises(DWRIdentityError) as exc_info:
        _map_post({
            "post": {
                "postUrl": "https://alice.lofter.com/post/1a_2b",
                "permalink": "1a_2c",
            }
        })

    assert exc_info.value.reason == "post_url_conflict"
    assert exc_info.value.fields == ("permalink", "postUrl")


def test_dwr_rejects_permalink_slug_conflicting_with_numeric_post_id():
    with pytest.raises(DWRIdentityError) as exc_info:
        _map_post({
            "post": {
                "permalink": "1a_2b",
                "postId": 44,
                "blogId": 26,
                "blogInfo": {"blogId": 26},
            }
        })

    assert exc_info.value.reason == "post_id_conflict"
    assert exc_info.value.fields == ("permalink", "postId")


def test_dwr_identity_shape_is_allowlisted():
    error = DWRIdentityError(
        "invalid_post_url",
        "permalink",
        value_shape="private-upstream-shape",
    )

    assert error.value_shape == "unknown"
    assert error.diagnostic == "invalid_post_url:permalink;shape=unknown"
    assert "private-upstream-shape" not in str(error)
    assert "private-upstream-shape" not in error.diagnostic


@pytest.mark.parametrize(
    "fallback",
    [
        "https://legacy.lofter.com/post/ff_ee",
        "https://legacy.lofter.com/",
        1,
    ],
)
def test_dwr_authoritative_url_ignores_legacy_fallback_drift(fallback):
    mapped = _map_post({
        "post": {
            "blogPageUrl": fallback,
            "postUrl": "https://alice.lofter.com/post/1a_2b",
            "blogInfo": {"blogName": "alice"},
        }
    })

    assert mapped is not None
    assert mapped.post_id == "1a_2b"
    assert mapped.url == "https://alice.lofter.com/post/1a_2b"


def test_dwr_uses_legacy_fallback_without_authoritative_url():
    mapped = _map_post({
        "post": {
            "blogPageUrl": "https://alice.lofter.com/post/1a_2b",
            "blogInfo": {"blogName": "alice"},
        }
    })

    assert mapped is not None
    assert mapped.post_id == "1a_2b"
    assert mapped.url == "https://alice.lofter.com/post/1a_2b"


def test_dwr_enforces_unselected_legacy_fallback_limit():
    post = {
        "blogPageUrl": _EXACT_POST_URL + "x",
        "postUrl": "https://user.lofter.com/post/abc_123",
    }

    with pytest.raises(SourceLimitError) as exc_info:
        _map_post({"post": post})

    assert (exc_info.value.resource, exc_info.value.limit) == (
        "url", MAX_URL_BYTES,
    )


def test_dwr_accepts_canonically_equivalent_authoritative_urls():
    mapped = _map_post({
        "post": {
            "blogPageUrl": "https://legacy.lofter.com/post/ff_ee",
            "postUrl": "https://DEMO.lofter.com:443/post/001A_00002B/",
            "permalink": "https://lofter.com/post/1a_2b",
            "blogInfo": {"blogName": "demo"},
        }
    })

    assert mapped is not None
    assert mapped.post_id == "1a_2b"
    assert mapped.url == "https://demo.lofter.com/post/1a_2b"


def test_dwr_generic_id_is_not_post_local_identity():
    mapped = _map_post({
        "post": {
            "postUrl": "https://demo.lofter.com/post/1a_2b",
            "id": 999,
            "postId": 43,
            "blogId": 26,
            "blogInfo": {"blogId": 26, "blogName": "demo"},
        }
    })

    assert mapped is not None
    assert mapped.post_id == "1a_2b"


def test_dwr_rejects_post_id_conflict_with_safe_fingerprint():
    secret_url = "https://private-owner.lofter.com/post/1a_2b"
    with pytest.raises(DWRIdentityError) as exc_info:
        _map_post({
            "post": {
                "postUrl": secret_url,
                "postId": 44,
                "blogInfo": {"blogName": "private-owner"},
            }
        })

    error = exc_info.value
    assert error.fingerprint == "post_id_conflict:postId+postUrl"
    assert secret_url not in str(error)
    assert "44" not in str(error)
    assert "private-owner" not in str(error)


@pytest.mark.asyncio
async def test_dwr_mixed_page_propagates_authoritative_identity_error():
    body = """
    dwr.engine._remoteHandleCallback("0", "0", [
        {post: {
            blogPageUrl: "https://demo.lofter.com/post/1a_2b",
            blogInfo: {blogName: "demo"}
        }},
        {post: {
            postUrl: "https://demo.lofter.com/post/1a_2c",
            permalink: "https://other.lofter.com/post/1a_2c"
        }}
    ]);
    """

    with pytest.raises(DWRIdentityError) as exc_info:
        await parse_dwr_response_result(body)

    assert exc_info.value.reason == "owner_conflict"


@pytest.mark.asyncio
async def test_parse_dwr_response_rejects_first_party_post_url_query():
    body = """
    dwr.engine._remoteHandleCallback("0", "0", [
        {post: {blogPageUrl: "https://user.lofter.com/post/abc_123?demo=1"}}
    ]);
    """

    with pytest.raises(SourceSchemaError) as exc_info:
        await parse_dwr_response(body)

    assert exc_info.value.location == "dwr.items"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "<html><body>login</body></html>",
        "//#DWR-INSERT\nNot logged in",
        "请求过于频繁，请稍后再试",
    ],
)
async def test_parse_dwr_response_rejects_challenge_without_preview(body):
    with pytest.raises(SourceChallengeError) as exc_info:
        await parse_dwr_response(body)

    message = str(exc_info.value)
    assert body not in message
    assert "响应片段" not in message


@pytest.mark.asyncio
async def test_parse_dwr_response_rejects_empty_response():
    with pytest.raises(SourceSchemaError) as exc_info:
        await parse_dwr_response("   ")

    assert exc_info.value.location == "dwr.body"


@pytest.mark.asyncio
async def test_parse_dwr_response_rejects_non_list_callback():
    body = 'dwr.engine._remoteHandleCallback("0", "0", {post: {}});'

    with pytest.raises(SourceSchemaError) as exc_info:
        await parse_dwr_response(body)

    assert exc_info.value.location == "dwr.callback"


@pytest.mark.asyncio
async def test_parse_dwr_response_rejects_nonempty_zero_mapping():
    body = 'dwr.engine._remoteHandleCallback("0", "0", [null, "bad"]);'

    with pytest.raises(SourceSchemaError) as exc_info:
        await parse_dwr_response(body)

    assert exc_info.value.location == "dwr.items"


@pytest.mark.asyncio
async def test_parse_dwr_response_enforces_item_limit_before_mapping():
    body = 'dwr.engine._remoteHandleCallback("0", "0", [' + ','.join(
        'null' for _ in range(101)
    ) + ']);'
    with pytest.raises(SourceLimitError) as exc_info:
        await parse_dwr_response(body)
    assert (exc_info.value.resource, exc_info.value.limit) == ("items", 100)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", _EXACT_TITLE),
        ("blogPageUrl", _EXACT_POST_URL),
        ("firstImageUrl", json.dumps([_EXACT_IMAGE_URL])),
        ("dirContent", _EXACT_CONTENT),
    ],
)
def test_dwr_mapping_accepts_exact_utf8_byte_limits(field, value):
    post = {
        "blogPageUrl": "https://user.lofter.com/post/abc_123",
        "title": "ok",
        field: value,
    }
    assert _map_post({"post": post}) is not None


@pytest.mark.parametrize(
    ("field", "value", "resource", "limit"),
    [
        ("title", _EXACT_TITLE + "x", "title", MAX_TITLE_BYTES),
        ("blogPageUrl", _EXACT_POST_URL + "x", "url", MAX_URL_BYTES),
        (
            "firstImageUrl",
            json.dumps([_EXACT_IMAGE_URL + "x"]),
            "url",
            MAX_URL_BYTES,
        ),
        ("dirContent", _EXACT_CONTENT + "x", "content", MAX_CONTENT_BYTES),
    ],
)
def test_dwr_mapping_rejects_one_byte_over_utf8_limits(
    field, value, resource, limit
):
    post = {
        "blogPageUrl": "https://user.lofter.com/post/abc_123",
        "title": "ok",
        field: value,
    }
    with pytest.raises(SourceLimitError) as exc_info:
        _map_post({"post": post})
    assert (exc_info.value.resource, exc_info.value.limit) == (resource, limit)


@pytest.mark.asyncio
async def test_parse_dwr_response_enforces_input_limit_before_execution():
    body = "x" * (5 * 1024 * 1024 + 1)

    with pytest.raises(SourceLimitError) as exc_info:
        await parse_dwr_response(body)

    assert exc_info.value.resource == "body"
    assert exc_info.value.limit == 5 * 1024 * 1024
