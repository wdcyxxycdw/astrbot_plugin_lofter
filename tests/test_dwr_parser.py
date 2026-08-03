import json

import pytest

from core.dwr_parser import _map_post, parse_dwr_response, parse_dwr_response_result
from core.errors import SourceChallengeError, SourceLimitError, SourceSchemaError
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
    ("alias", "value"),
    [
        ("postUrl", "https://bob.lofter.com/post/1a_2b"),
        ("permalink", "https://alice.lofter.com/post/1a_2c"),
        ("postUrl", 1),
    ],
)
def test_dwr_rejects_conflicting_or_invalid_url_alias(alias, value):
    post = {
        "blogPageUrl": "https://alice.lofter.com/post/1a_2b",
        "blogInfo": {"blogName": "alice"},
        alias: value,
    }

    with pytest.raises(SourceSchemaError) as exc_info:
        _map_post({"post": post})

    assert exc_info.value.location == "dwr.post.id"


def test_dwr_enforces_unselected_url_alias_limit():
    post = {
        "blogPageUrl": "https://user.lofter.com/post/abc_123",
        "postUrl": _EXACT_POST_URL + "x",
    }

    with pytest.raises(SourceLimitError) as exc_info:
        _map_post({"post": post})

    assert (exc_info.value.resource, exc_info.value.limit) == (
        "url", MAX_URL_BYTES,
    )


def test_dwr_accepts_canonically_equivalent_url_aliases():
    mapped = _map_post({
        "post": {
            "blogPageUrl": "https://DEMO.lofter.com:443/post/001A_00002B/",
            "postUrl": "https://demo.lofter.com/post/1a_2b",
            "permalink": "https://lofter.com/post/1a_2b",
            "blogInfo": {"blogName": "demo"},
        }
    })

    assert mapped is not None
    assert mapped.post_id == "1a_2b"
    assert mapped.url == "https://demo.lofter.com/post/1a_2b"


@pytest.mark.asyncio
async def test_dwr_mixed_page_propagates_url_alias_identity_error():
    body = """
    dwr.engine._remoteHandleCallback("0", "0", [
        {post: {
            blogPageUrl: "https://demo.lofter.com/post/1a_2b",
            blogInfo: {blogName: "demo"}
        }},
        {post: {
            blogPageUrl: "https://demo.lofter.com/post/1a_2c",
            postUrl: "https://other.lofter.com/post/1a_2c"
        }}
    ]);
    """

    with pytest.raises(SourceSchemaError) as exc_info:
        await parse_dwr_response_result(body)

    assert exc_info.value.location == "dwr.post.id"


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
