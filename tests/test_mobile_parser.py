import json
from pathlib import Path

import pytest

from core.errors import (
    SourceBusinessError,
    SourceChallengeError,
    SourceClosingError,
    SourceError,
    SourceHTTPError,
    SourceLimitError,
    SourcePartialError,
    SourceRetryExhaustedError,
    SourceSchemaError,
    SourceTimeoutError,
)
from core.mobile_parser import (
    MAX_BODY_BYTES,
    MAX_CONTENT_BYTES,
    MAX_ITEMS,
    MAX_TITLE_BYTES,
    MAX_URL_BYTES,
    parse_mobile_blog_page,
    parse_mobile_post_detail,
    parse_mobile_tag_page,
)
from core.parser import Post

FIXTURES = Path(__file__).parent / "fixtures" / "lofter"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _detail_item(**post_changes):
    item = _fixture("post_detail.json")["envelope"]["response"]["posts"][0]
    item = json.loads(json.dumps(item))
    item["post"].update(post_changes)
    return item


def _blog_payload(posts, **changes):
    response = {
        "archives": [],
        "minTimeStamp": 1710000000000,
        "isMember": False,
        "offset": -1,
        "firstPost": None,
        "posts": posts,
    }
    response.update(changes)
    return {"meta": {"status": 200, "msg": "demo"}, "response": response}


def _nested_detail_item(**post_changes):
    item = _detail_item()
    post = item["post"]
    post["blogInfo"] = item.pop("blogInfo")
    post["permalink"] = "1a_2b"
    post["blogPageUrl"] = item.pop("blogPageUrl")
    item.pop("permalink")
    post.update(post_changes)
    return item


def _tag_item(**view_changes):
    item = _fixture("tag_posts.json")["envelope"]["data"]["list"][0]
    item = json.loads(json.dumps(item))
    item["postData"]["postView"].update(view_changes)
    return item


def _tag_payload(items, offset=-1):
    return {"code": 0, "msg": "demo", "data": {"list": items, "offset": offset}}


def test_detail_fixture_maps_exact_sibling_contract_to_shared_post():
    post = parse_mobile_post_detail(_fixture("post_detail.json")["envelope"])

    assert isinstance(post, Post)
    assert post.post_id == "1a_2b"
    assert post.title == "Demo"
    assert {"summary", "content"} <= post.completeness
    assert post.content == "Demo"
    assert post.author == "Demo"
    assert post.author_username == "demo"
    assert post.tags == ["demo", "example"]
    assert post.images == ["https://media.example.invalid/demo.jpg"]


def test_detail_maps_current_nested_contract():
    payload = {
        "meta": {"status": 200, "msg": "demo"},
        "response": {"posts": [_nested_detail_item()]},
    }

    post = parse_mobile_post_detail(payload)

    assert post.post_id == "1a_2b"
    assert post.url == "https://demo.lofter.com/post/1a_2b"
    assert post.author_username == "demo"
    assert {"url", "author_username", "publish_time"} <= post.completeness


def test_detail_nested_slug_must_match_numeric_identity():
    item = _nested_detail_item(
        permalink="1a_2c",
        blogPageUrl="https://demo.lofter.com/post/1a_2c",
    )
    payload = {
        "meta": {"status": 200, "msg": "demo"},
        "response": {"posts": [item]},
    }

    with pytest.raises(SourceSchemaError) as exc_info:
        parse_mobile_post_detail(payload)

    assert exc_info.value.location == "post.id"


def test_detail_duplicate_locations_must_not_conflict():
    item = _detail_item()
    item["post"]["permalink"] = "1a_2c"
    item["post"]["blogPageUrl"] = "https://other.lofter.com/post/1a_2c"
    item["post"]["blogInfo"] = dict(item["blogInfo"])
    payload = {
        "meta": {"status": 200, "msg": "demo"},
        "response": {"posts": [item]},
    }

    with pytest.raises(SourceSchemaError) as exc_info:
        parse_mobile_post_detail(payload)

    assert exc_info.value.location in {"post.id", "post.url"}


def test_detail_maps_json_encoded_photo_fields():
    item = _nested_detail_item(
        photoLinks=json.dumps([
            {
                "orign": "https://media.example.invalid/original.jpg",
                "raw": "https://media.example.invalid/raw.jpg",
                "middle": "https://media.example.invalid/middle.jpg",
            }
        ]),
        photoCaptions=json.dumps(["Demo"]),
    )
    payload = {
        "meta": {"status": 200, "msg": "demo"},
        "response": {"posts": [item]},
    }

    post = parse_mobile_post_detail(payload)

    assert post.images == ["https://media.example.invalid/original.jpg"]
    assert "images" in post.completeness


def test_detail_rejects_malformed_json_encoded_photo_fields():
    item = _nested_detail_item(photoLinks="[not-json")
    payload = {
        "meta": {"status": 200, "msg": "demo"},
        "response": {"posts": [item]},
    }

    with pytest.raises(SourceSchemaError) as exc_info:
        parse_mobile_post_detail(payload)

    assert exc_info.value.location == "photoLinks"


def test_detail_nullable_optional_fields_remain_unknown():
    item = _detail_item(content=None, tag=None, photoLinks=None)
    payload = {
        "meta": {"status": 200, "msg": "demo"},
        "response": {"posts": [item]},
    }

    post = parse_mobile_post_detail(payload)

    assert {"content", "tags", "images"}.isdisjoint(post.completeness)
    assert "summary" in post.completeness
    assert post.summary == "Demo"
    assert post.content == ""
    assert post.tags == []
    assert post.images == []


def test_detail_missing_digest_and_content_keeps_summary_unknown():
    item = _detail_item(content=None, digest=None)
    payload = {
        "meta": {"status": 200, "msg": "demo"},
        "response": {"posts": [item]},
    }

    post = parse_mobile_post_detail(payload)

    assert {"summary", "content"}.isdisjoint(post.completeness)
    assert post.summary == post.content == ""


def test_blog_fixture_maps_posts_and_offset():
    page = parse_mobile_blog_page(_fixture("blog_home.json")["envelope"])

    assert [post.post_id for post in page.items] == ["1a_2b"]
    assert page.source == "mobile_blog"
    assert page.next_cursor == "20"
    assert page.exhausted is False
    assert page.sort == "new"
    assert page.mapped_count == 1
    assert page.dropped_count == 0
    assert page.complete is True


def test_tag_fixture_maps_exact_post_data_contract():
    page = parse_mobile_tag_page(_fixture("tag_posts.json")["envelope"])

    assert page.source == "mobile_tag"
    assert page.items[0].post_id == "1a_2b"
    assert page.items[0].title == "Demo"
    assert page.items[0].author_username == "demo"
    assert page.items[0].summary == ""
    assert page.next_cursor == "20"


def test_tag_maps_current_permalink_slug():
    page = parse_mobile_tag_page(_tag_payload([_tag_item(
        id=43,
        permalink="1a_2b",
    )], offset=20))

    post = page.items[0]
    assert post.post_id == "1a_2b"
    assert post.url == "https://lofter.com/post/1a_2b"
    assert post.author_username == ""
    assert "author_username" not in post.completeness
    assert page.next_cursor == "20"


def test_tag_permalink_slug_must_match_numeric_ids():
    item = _tag_item(id=44, permalink="1a_2b")

    with pytest.raises(SourceSchemaError) as exc_info:
        parse_mobile_tag_page(_tag_payload([item]))

    assert exc_info.value.location == "post.id"


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (parse_mobile_blog_page, _blog_payload([])),
        (parse_mobile_tag_page, _tag_payload([])),
    ],
)
def test_valid_empty_is_complete(parser, payload):
    page = parser(payload)

    assert page.items == []
    assert page.exhausted is True
    assert page.complete is True
    assert page.mapped_count == page.dropped_count == 0


def test_blog_first_post_requires_recorded_null_variant():
    assert parse_mobile_blog_page(_blog_payload([], firstPost=None)).items == []
    for value in ({}, {"unexpected": True}, False, 0, "bad"):
        with pytest.raises(SourceSchemaError):
            parse_mobile_blog_page(_blog_payload([], firstPost=value))


def test_partial_blog_page_preserves_good_items_and_counts_drops():
    page = parse_mobile_blog_page(_blog_payload([_detail_item(), None, {"post": {}}]))

    assert len(page.items) == 1
    assert page.mapped_count == 1
    assert page.dropped_count == 2
    assert page.complete is False


def test_partial_tag_page_preserves_good_items_and_counts_drops():
    page = parse_mobile_tag_page(_tag_payload([_tag_item(), None, {"postData": {}}]))

    assert len(page.items) == 1
    assert page.mapped_count == 1
    assert page.dropped_count == 2
    assert page.complete is False


def test_nonempty_zero_mapped_is_typed_partial_failure():
    with pytest.raises(SourcePartialError) as exc_info:
        parse_mobile_tag_page(_tag_payload([None, {"postData": {}}]))

    assert exc_info.value.mapped_count == 0
    assert exc_info.value.dropped_count == 2


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (parse_mobile_post_detail, {"meta": {"status": 200, "msg": "demo"}, "response": {"posts": {}}}),
        (parse_mobile_blog_page, {"meta": {"status": 200, "msg": "demo"}, "response": {}}),
        (parse_mobile_tag_page, {"code": 0, "msg": "demo", "data": {"list": {}, "offset": -1}}),
        (parse_mobile_tag_page, {"code": 0, "msg": "demo", "data": {"list": [], "offset": "20"}}),
    ],
)
def test_endpoint_schema_failures_are_typed(parser, payload):
    with pytest.raises(SourceSchemaError):
        parser(payload)


@pytest.mark.parametrize(
    ("parser", "payload", "code"),
    [
        (parse_mobile_post_detail, {"meta": {"status": 403, "msg": "token=private"}}, 403),
        (parse_mobile_tag_page, {"code": 403, "msg": "token=private"}, 403),
    ],
)
def test_business_failures_are_typed_and_redacted(parser, payload, code):
    with pytest.raises(SourceBusinessError) as exc_info:
        parser(payload)

    assert exc_info.value.code == code
    assert "private" not in str(exc_info.value)


@pytest.mark.parametrize("raw", ["<html>login token=private</html>", "<!doctype html><title>challenge</title>"])
def test_html_login_or_challenge_is_typed_and_redacted(raw):
    with pytest.raises(SourceChallengeError) as exc_info:
        parse_mobile_tag_page(raw)

    assert "private" not in str(exc_info.value)
    assert "challenge" not in str(exc_info.value).lower()


def test_json_decode_failure_does_not_include_payload():
    with pytest.raises(SourceSchemaError) as exc_info:
        parse_mobile_tag_page('{"code":0,"token":"private",')
    assert "private" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (parse_mobile_blog_page, _blog_payload([None] * (MAX_ITEMS + 1))),
        (parse_mobile_tag_page, _tag_payload([None] * (MAX_ITEMS + 1))),
    ],
)
def test_items_limit_is_enforced_before_mapping(parser, payload):
    with pytest.raises(SourceLimitError) as exc_info:
        parser(payload)
    assert exc_info.value.resource == "items"
    assert exc_info.value.limit == MAX_ITEMS


@pytest.mark.parametrize(
    ("field", "value", "resource", "limit"),
    [
        ("title", "字" * (MAX_TITLE_BYTES // 3 + 1), "title", MAX_TITLE_BYTES),
        ("content", "字" * (MAX_CONTENT_BYTES // 3 + 1), "content", MAX_CONTENT_BYTES),
    ],
)
def test_detail_field_byte_limits(field, value, resource, limit):
    payload = {"meta": {"status": 200, "msg": "demo"}, "response": {"posts": [_detail_item(**{field: value})]}}
    with pytest.raises(SourceLimitError) as exc_info:
        parse_mobile_post_detail(payload)
    assert (exc_info.value.resource, exc_info.value.limit) == (resource, limit)


def test_detail_url_byte_limit():
    item = _detail_item()
    item["permalink"] = "https://demo.lofter.com/post/1a_2b?" + "x" * MAX_URL_BYTES
    payload = {"meta": {"status": 200, "msg": "demo"}, "response": {"posts": [item]}}
    with pytest.raises(SourceLimitError) as exc_info:
        parse_mobile_post_detail(payload)
    assert (exc_info.value.resource, exc_info.value.limit) == ("url", MAX_URL_BYTES)


def test_body_encoding_failure_is_typed_schema_error():
    with pytest.raises(SourceSchemaError) as exc_info:
        parse_mobile_tag_page("\ud800")
    assert exc_info.value.location == "response"


def test_body_limit_is_enforced_before_json_decode():
    with pytest.raises(SourceLimitError) as exc_info:
        parse_mobile_tag_page(" " * (MAX_BODY_BYTES + 1))
    assert (exc_info.value.resource, exc_info.value.limit) == ("body", MAX_BODY_BYTES)


def test_detail_sibling_blog_id_and_urls_must_match_post():
    item = _detail_item()
    item["blogInfo"]["blogId"] = 27
    payload = {"meta": {"status": 200, "msg": "demo"}, "response": {"posts": [item]}}
    with pytest.raises(SourceSchemaError, match="blogInfo.blogId"):
        parse_mobile_post_detail(payload)


@pytest.mark.parametrize("field", ["permalink", "blogPageUrl"])
def test_detail_sibling_urls_must_match_canonical_post_id(field):
    item = _detail_item()
    item[field] = "https://demo.lofter.com/post/1a_2c"
    payload = {"meta": {"status": 200, "msg": "demo"}, "response": {"posts": [item]}}
    with pytest.raises(SourceSchemaError, match="post.url"):
        parse_mobile_post_detail(payload)


def test_tag_post_count_blog_id_must_match_view():
    item = _tag_item()
    item["postData"]["postCount"]["blogId"] = 27
    with pytest.raises(SourceSchemaError) as exc_info:
        parse_mobile_tag_page(_tag_payload([item]))
    assert exc_info.value.location == "postData.postCount.blogId"


def test_archives_is_schema_metadata_not_post_item_limit():
    page = parse_mobile_blog_page(_blog_payload([], archives=[{}] * (MAX_ITEMS + 1)))
    assert page.items == []
    assert page.complete is True


def test_schema_error_only_preserves_known_static_locations():
    assert "cursor" in str(SourceSchemaError("cursor"))
    assert "session-secret" not in str(SourceSchemaError("session-secret"))


def test_error_hierarchy_and_constructors_never_expose_caller_payload():
    secret = "Cookie=session-secret response-body=private"
    errors = [
        SourceError(secret),
        SourceHTTPError(503, secret),
        SourceSchemaError(secret),
        SourceLimitError(secret),
        SourceClosingError(secret),
        SourceTimeoutError(secret),
        SourceRetryExhaustedError(secret),
    ]
    for error in errors:
        assert secret not in str(error)
        assert "session-secret" not in str(error)
