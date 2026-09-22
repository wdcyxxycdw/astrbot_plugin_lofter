from lofter import Post, PostDetail
from lofter.models import POST_TYPE_PHOTO, POST_TYPE_TEXT
from core.formatter import format_post, is_photo_post

DIVIDER = "──────────────"


def make_post(**kwargs):
    defaults = dict(post_id="1", title="测试标题", summary="摘要内容", url="https://example.lofter.com/post/1")
    return Post(**{**defaults, **kwargs})


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


def test_divider_and_url_always_present():
    post = make_post(author="", tags=[], summary="")
    result = format_post(post)
    assert f"{DIVIDER}\nhttps://example.lofter.com/post/1" in result


def make_detail(post_type, images=()):
    return PostDetail(post=make_post(images=list(images)), post_type=post_type)


def test_is_photo_post_trusts_photo_type_even_without_images():
    assert is_photo_post(make_detail(POST_TYPE_PHOTO)) is True


def test_is_photo_post_trusts_text_type_even_when_images_exist():
    assert is_photo_post(make_detail(POST_TYPE_TEXT, images=["https://img/1.png"])) is False


def test_is_photo_post_falls_back_to_images_for_unknown_types():
    assert is_photo_post(make_detail(3, images=["https://img/1.png"])) is True
    assert is_photo_post(make_detail(3)) is False


def test_is_photo_post_treats_missing_type_as_unknown():
    assert is_photo_post(make_detail(0, images=["https://img/1.png"])) is True
    assert is_photo_post(make_detail(0)) is False


def test_word_count_appears_next_to_the_author():
    text = format_post(make_post(author="作者甲"), word_count=1234)

    assert "作者：作者甲  1234 字" in text


def test_word_count_is_omitted_when_zero():
    text = format_post(make_post(author="作者甲"), word_count=0)

    assert "字" not in text.split("\n")[1]


def test_word_count_stands_alone_when_there_is_no_author():
    text = format_post(make_post(author=""), word_count=99)

    assert "99 字" in text


def test_author_time_and_word_count_share_one_line():
    post = make_post(author="作者甲", publish_time="2026-01-02 03:04")
    text = format_post(post, include_time=True, word_count=50)

    assert "作者：作者甲  2026-01-02 03:04  50 字" in text
