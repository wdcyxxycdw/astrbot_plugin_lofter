import json

import pytest

from core.errors import SourceChallengeError, SourceLimitError, SourceSchemaError
from core.parser import extract_initialize_data, parse_embedded_post


POST_URL = "https://synthetic.lofter.com/post/abc_123"


def embedded_html(data: object) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"<html><script>window.__initialize_data__ = {payload};</script></html>"


def synthetic_post(**overrides):
    post = {
        "postId": 291,
        "blogPageUrl": POST_URL,
        "title": "合成标题",
        "dirContent": "<p>合成摘要</p>",
        "content": "<p>第一段</p><p>第二段</p>",
        "tag": "标签甲,标签乙",
        "photoLinks": [
            {"origin": "https://imglf1.lf127.net/img/a.jpg?quality=90"},
            {"origin": "https://imglf1.lf127.net/img/a.jpg?quality=80"},
        ],
        "blogInfo": {"blogNickName": "合成作者", "blogName": "synthetic"},
    }
    post.update(overrides)
    return {"state": {"detail": {"post": post}}}


def test_extract_initialize_data_accepts_only_pure_assignment():
    data = extract_initialize_data(embedded_html({"ok": {"value": 1}}))
    assert data == {"ok": {"value": 1}}


@pytest.mark.parametrize(
    "script",
    [
        'window.__initialize_data__ = {"ok": true}; alert(1);',
        'window.__initialize_data__ = {ok: true};',
        'const x = {}; window.__initialize_data__ = {"ok": true};',
        'window.__initialize_data__ = null;',
    ],
)
def test_extract_initialize_data_rejects_javascript_or_bad_schema(script):
    with pytest.raises(SourceSchemaError):
        extract_initialize_data(f"<script>{script}</script>")


def test_extract_initialize_data_rejects_duplicate_assignments():
    html = '<script>window.__initialize_data__ = {"a": 1};</script>' * 2
    with pytest.raises(SourceSchemaError, match="embedded.assignment"):
        extract_initialize_data(html)


def test_embedded_login_page_is_typed_challenge():
    html = '<html><head><title>安全验证</title></head><script>window.__initialize_data__ = {};</script></html>'
    with pytest.raises(SourceChallengeError):
        extract_initialize_data(html)


def test_parse_embedded_post_maps_clear_post_fields():
    post = parse_embedded_post(embedded_html(synthetic_post()), POST_URL)
    assert post.post_id == "abc_123"
    assert post.title == "合成标题"
    assert post.summary == "合成摘要"
    assert post.content == "第一段\n第二段"
    assert post.tags == ["标签甲", "标签乙"]
    assert post.images == ["https://imglf1.lf127.net/img/a.jpg"]
    assert post.author == "合成作者"
    assert {"summary", "content"} <= post.completeness
    assert post.author_username == "synthetic"


def test_embedded_nullable_fields_remain_unknown():
    data = synthetic_post(
        content=None,
        tag=None,
        photoLinks=None,
        blogInfo={"blogNickName": None, "blogName": None},
    )

    post = parse_embedded_post(embedded_html(data), POST_URL)

    assert {"content", "tags", "images", "author"}.isdisjoint(
        post.completeness
    )
    assert post.content == ""
    assert post.tags == []
    assert post.images == []
    assert post.author == ""


def test_embedded_summary_and_content_have_independent_completeness():
    data = synthetic_post(dirContent=None, content="<p>可信全文</p>")

    post = parse_embedded_post(embedded_html(data), POST_URL)

    assert "summary" not in post.completeness
    assert "content" in post.completeness
    assert post.summary == ""
    assert post.content == "可信全文"


def test_parse_embedded_post_prefers_requested_post():
    other = synthetic_post(blogPageUrl="https://other.lofter.com/post/def_456", title="错误帖子")
    requested = synthetic_post()
    data = {"items": [other["state"]["detail"]["post"], requested["state"]["detail"]["post"]]}
    assert parse_embedded_post(embedded_html(data), POST_URL).title == "合成标题"


def test_parse_embedded_post_rejects_only_wrong_post_identity():
    wrong = synthetic_post(blogPageUrl="https://other.lofter.com/post/def_456")
    with pytest.raises(SourceSchemaError):
        parse_embedded_post(embedded_html(wrong), POST_URL)


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"state": {"detail": {"post": {"postId": 291, "title": ""}}}},
        synthetic_post(title=12),
        synthetic_post(tags={"bad": True}),
        synthetic_post(photoLinks=[{"height": 10}]),
    ],
)
def test_parse_embedded_post_validates_schema(data):
    with pytest.raises(SourceSchemaError):
        parse_embedded_post(embedded_html(data), POST_URL)


@pytest.mark.parametrize(
    ("overrides", "resource"),
    [
        ({"title": "题" * 4097}, "title"),
        ({"blogPageUrl": "https://synthetic.lofter.com/post/" + "a" * 8192}, "url"),
        ({"content": "文" * (2 * 1024 * 1024 + 1)}, "content"),
    ],
)
def test_parse_embedded_post_field_limits(overrides, resource):
    with pytest.raises(SourceLimitError) as exc_info:
        parse_embedded_post(embedded_html(synthetic_post(**overrides)), POST_URL)
    assert exc_info.value.resource == resource
