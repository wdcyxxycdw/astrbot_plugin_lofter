import pytest

from core.dwr_parser import parse_dwr_response


@pytest.mark.asyncio
async def test_parse_dwr_response_accepts_valid_empty_result():
    posts = await parse_dwr_response('dwr.engine._remoteHandleCallback("0", "0", []);')

    assert posts == []


@pytest.mark.asyncio
async def test_parse_dwr_response_skips_non_dict_items_and_keeps_valid_post():
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
                blogInfo: {blogNickName: "作者", blogName: "fallbackuser"},
                tag: "tag-a, tag-b",
                publishTime: 1710000000000,
                firstImageUrl: '["https://img.example/a.jpg?x=1"]'
            }
        }
    ]);
    """

    posts = await parse_dwr_response(body)

    assert len(posts) == 1
    assert posts[0].post_id == "abc_123"
    assert posts[0].title == "有效帖子"
    assert posts[0].summary == "正文"
    assert posts[0].author == "作者"
    assert posts[0].author_username == "someuser"
    assert posts[0].tags == ["tag-a", "tag-b"]
    assert posts[0].images == ["https://img.example/a.jpg"]


@pytest.mark.asyncio
async def test_parse_dwr_response_uses_blog_name_when_url_has_no_lofter_username():
    body = """
    dwr.engine._remoteHandleCallback("0", "0", [
        {
            post: {
                blogPageUrl: "https://evil.com/redirect?u=https://someuser.lofter.com/post/abc_123",
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
    "body",
    [
        "<html><body>login</body></html>",
        "//#DWR-INSERT\nNot logged in",
        "请求过于频繁，请稍后再试",
    ],
)
async def test_parse_dwr_response_rejects_non_dwr_response(body):
    with pytest.raises(RuntimeError) as exc_info:
        await parse_dwr_response(body)

    message = str(exc_info.value)
    assert "LOFTER 返回非 DWR 响应" in message
    assert "Cookie 失效" in message
    assert "未登录" in message
    assert "风控" in message
    assert "unterminated statement" not in message


@pytest.mark.asyncio
async def test_parse_dwr_response_rejects_empty_response():
    with pytest.raises(RuntimeError, match="DWR 响应为空"):
        await parse_dwr_response("   ")
