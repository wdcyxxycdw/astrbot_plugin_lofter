import pytest
from core.parser import Post
from core.formatter import format_post, visible_images

DIVIDER = "──────────────"


def make_post(**kwargs):
    defaults = dict(
        post_id="1",
        title="测试标题",
        summary="摘要内容",
        url="https://example.lofter.com/post/1",
    )
    values = {**defaults, **kwargs}
    if "completeness" not in values:
        values["completeness"] = frozenset({
            "title", "summary", "content", "images", "tags", "author",
            "author_username", "publish_time", "url",
        })
    return Post(**values)


def test_basic_all_fields():
    post = make_post(author="作者A", tags=["tag1", "tag2"], summary="摘要内容")
    result = format_post(post)
    assert result == (
        "▸ 测试标题\n作者：作者A"
        "\n\n#tag1 #tag2"
        "\n\n摘要内容"
        f"\n\n{DIVIDER}\nhttps://example.lofter.com/post/1"
    )


def test_with_header():
    post = make_post(author="作者A", tags=[], summary="")
    result = format_post(post, header="【标签「原神」有新内容】")
    assert result.startswith("【标签「原神」有新内容】\n\n▸ 测试标题")


def test_no_author():
    post = make_post(author="", tags=[], summary="")
    result = format_post(post)
    assert "作者" not in result
    assert result.startswith("▸ 测试标题")


def test_no_tags():
    post = make_post(author="作者A", tags=[], summary="摘要")
    result = format_post(post)
    assert "#" not in result


def test_no_summary():
    post = make_post(author="作者A", tags=["tag1"], summary="")
    result = format_post(post)
    lines = result.split("\n\n")
    assert lines[-1] == f"{DIVIDER}\nhttps://example.lofter.com/post/1"


def test_no_title_fallback():
    post = make_post(title="", author="", tags=[], summary="")
    result = format_post(post)
    assert "▸ (无标题)" in result


def test_include_time():
    post = make_post(author="作者A", tags=[], summary="", publish_time="2024-01-01")
    result = format_post(post, include_time=True)
    assert "作者：作者A  2024-01-01" in result


def test_include_time_no_author():
    post = make_post(author="", tags=[], summary="", publish_time="2024-01-01")
    result = format_post(post, include_time=True)
    assert "2024-01-01" in result


def test_body_override():
    post = make_post(author="", tags=[], summary="原始摘要")
    result = format_post(post, body="覆盖内容")
    assert "覆盖内容" in result
    assert "原始摘要" not in result


def test_known_summary_renders_without_known_content():
    post = make_post(
        summary="可信摘要",
        content="poisoned content",
        source="mobile_detail",
        completeness=frozenset({"title", "summary", "url"}),
    )

    result = format_post(post)

    assert "可信摘要" in result
    assert "poisoned content" not in result


def test_unknown_fields_are_not_rendered_as_real_values():
    post = make_post(
        title="secret title",
        author="secret author",
        tags=["secret-tag"],
        summary="secret body",
        publish_time="2099-01-01",
        source="mobile_tag",
        completeness=frozenset({"url"}),
    )

    result = format_post(post, include_time=True)

    assert "(标题未知)" in result
    assert "secret" not in result
    assert "2099-01-01" not in result
    assert "部分字段未知" in result
    assert post.url in result
    assert visible_images(post) == []


def test_divider_and_url_always_present():
    post = make_post(author="", tags=[], summary="")
    result = format_post(post)
    assert f"{DIVIDER}\nhttps://example.lofter.com/post/1" in result
