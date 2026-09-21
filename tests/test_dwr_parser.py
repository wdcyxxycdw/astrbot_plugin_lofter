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


@pytest.mark.asyncio
async def test_shared_author_reference_is_preserved_without_recursing_forever():
    body = """
    var blog = {blogNickName: '同一个作者'};
    blog.self = blog;
    dwr.engine._remoteHandleCallback('0','0',[
        {post:{blogPageUrl:'https://a.lofter.com/post/a_1',blogInfo:blog}},
        {post:{blogPageUrl:'https://a.lofter.com/post/a_2',blogInfo:blog}}
    ]);
    """
    posts = await parse_dwr_response(body)
    assert [post.author for post in posts] == ["同一个作者", "同一个作者"]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    "dwr.engine._remoteHandleCallback('0','0',{status:4009});",
    "dwr.engine._remoteHandleCallback('0','0',null);",
    "dwr.engine._remoteHandleCallback('0','0',[{unexpected:'schema'}]);",
    "dwr.engine._remoteHandleException('0','0',{message:'not logged in'});",
    "// dwr.engine._remoteHandleCallback('0','0',[]);",
])
async def test_invalid_result_is_never_a_successful_empty_page(body):
    with pytest.raises(RuntimeError):
        await parse_dwr_response(body)


@pytest.mark.asyncio
async def test_dwr_preserves_tag_list_full_text_images_and_millisecond_cursor():
    body = """
    dwr.engine._remoteHandleCallback('0','0',[{post:{
        blogPageUrl:'https://a.lofter.com/post/a_1',
        tagList:['标签一','标签二'],
        content:'<p>第一段</p><p>第二段</p>',
        photoLinks:'[{"raw":"https://image/a.jpg?token=abc"},{"orign":"https://image/b.jpg"}]',
        publishTime:1720000000123
    }}]);
    """
    post, = await parse_dwr_response(body)
    assert post.tags == ["标签一", "标签二"]
    assert post.content == "第一段\n第二段"
    assert post.images == ["https://image/a.jpg?token=abc", "https://image/b.jpg"]
    assert post.publish_time_ms == 1720000000123
