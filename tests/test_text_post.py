import pytest
from lofter import Post, PostDetail

from core.text_post import build_text_file, post_word_count, text_filename, write_text_file


def make_detail(content="正文", word_count=0, **kwargs):
    defaults = dict(
        post_id="abc_123",
        title="我的文章",
        summary="摘要",
        url="https://author.lofter.com/post/abc_123",
        author="作者甲",
        tags=["原创", "小说"],
        content=content,
    )
    return PostDetail(post=Post(**{**defaults, **kwargs}), word_count=word_count)


def test_word_count_prefers_the_number_lofter_returns():
    assert post_word_count(make_detail(content="短", word_count=4321)) == 4321


def test_word_count_falls_back_to_content_length_when_lofter_omits_it():
    assert post_word_count(make_detail(content="一" * 150)) == 150


def test_word_count_is_zero_for_empty_posts():
    assert post_word_count(make_detail(content="")) == 0


def test_text_filename_uses_the_article_title():
    assert text_filename("我的文章", "abc_123") == "我的文章.txt"


def test_text_filename_drops_path_separators():
    assert text_filename("上/下", "abc_123") == "上下.txt"


def test_text_filename_falls_back_to_post_id():
    assert text_filename("", "abc_123") == "abc_123.txt"


def test_text_file_keeps_the_whole_body():
    body = "段落一\n\n段落二" * 500
    content = build_text_file(make_detail(content=body).post, 3000)

    assert body in content


def test_text_file_header_carries_title_author_tags_count_and_link():
    content = build_text_file(make_detail().post, 1234)
    head = content.split("──────────────")[0]

    assert "我的文章" in head
    assert "作者：作者甲" in head
    assert "#原创 #小说" in head
    assert "字数：1234" in head
    assert "https://author.lofter.com/post/abc_123" in head


def test_text_file_tolerates_missing_title_author_and_tags():
    detail = make_detail(title="", author="", tags=[])
    content = build_text_file(detail.post, 10)

    assert content.startswith("(无标题)")
    assert "作者：" not in content


@pytest.mark.parametrize("title", ["我的文章", ""])
def test_write_text_file_creates_the_directory_and_returns_the_path(tmp_path, title):
    detail = make_detail(title=title, content="正文内容")

    path = write_text_file(tmp_path / "articles", detail.post, 4)

    assert path.parent.name == "articles"
    assert path.name == text_filename(title, "abc_123")
    assert "正文内容" in path.read_text(encoding="utf-8")


def test_write_text_file_overwrites_a_previous_run(tmp_path):
    detail = make_detail(content="第一版")
    first = write_text_file(tmp_path, detail.post, 3)
    second = write_text_file(tmp_path, make_detail(content="第二版").post, 3)

    assert first == second
    assert "第二版" in second.read_text(encoding="utf-8")
    assert "第一版" not in second.read_text(encoding="utf-8")
