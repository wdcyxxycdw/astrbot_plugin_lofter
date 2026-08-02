import json
from pathlib import Path
from urllib.parse import urlencode

import pytest
from unittest.mock import AsyncMock

from core.errors import SourceSchemaError
from core.mobile_adapter import (
    MobileAdapter,
    build_blog_request,
    build_detail_request,
    build_tag_request,
)

FIXTURES = Path(__file__).parent / "fixtures" / "lofter"
EXPECTED_WIRES = {
    "post_detail.json": (
        "https://api.lofter.com/oldapi/post/detail.api?product=lofter-android-8.2.23",
        {"targetblogid", "postid"},
    ),
    "blog_home.json": (
        "https://api.lofter.com/v2.0/blogHomePage.api",
        {
            "blogdomain", "offset", "limit", "method", "supportposttypes",
            "postdigestnew", "returnData", "checkpwd", "needgetpoststat",
        },
    ),
    "tag_posts.json": (
        "https://api.lofter.com/newapi/tagPosts.json",
        {
            "tag", "offset", "type", "recentDay", "range", "protectedFlag",
            "postTypes", "postYm", "firstpermalink", "style",
        },
    ),
}


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", EXPECTED_WIRES)
def test_mobile_wire_contract_is_exact_and_form_encoded(name):
    fixture = _fixture(name)
    expected_url, expected_fields = EXPECTED_WIRES[name]

    assert fixture["sampled_at"] == "2026-07-26"
    assert fixture["redaction"]["mode"] == "synthetic-replacement"
    assert fixture["method"] == "POST"
    assert fixture["URL"] == expected_url
    assert fixture["Content-Type"] == "application/x-www-form-urlencoded"
    assert fixture["Accept"] == "application/json"
    assert set(fixture["form"]) == expected_fields
    assert all(isinstance(value, str) for value in fixture["form"].values())
    assert isinstance(urlencode(fixture["form"]), str)
    assert fixture["envelope"] and fixture["variants"]


@pytest.mark.parametrize(
    ("name", "wire_request"),
    [
        ("post_detail.json", build_detail_request("26", "43")),
        ("blog_home.json", build_blog_request("demo", None, 20)),
        ("tag_posts.json", build_tag_request("demo", None)),
    ],
)
def test_request_builder_matches_recorded_wire(name, wire_request):
    fixture = _fixture(name)

    assert wire_request.method == fixture["method"]
    assert wire_request.url == fixture["URL"]
    assert wire_request.form == fixture["form"]
    assert wire_request.headers["Content-Type"] == fixture["Content-Type"]
    assert wire_request.headers["Accept"] == fixture["Accept"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "method", "args", "wire_request"),
    [
        (
            "post_detail.json",
            "get_post",
            ("26", "43"),
            build_detail_request("26", "43"),
        ),
        (
            "blog_home.json",
            "list_blog",
            ("demo", None, 20),
            build_blog_request("demo", None, 20),
        ),
        (
            "tag_posts.json",
            "list_tag",
            ("demo", None),
            build_tag_request("demo", None),
        ),
    ],
)
async def test_mobile_adapter_sends_anonymous_recorded_request(
    name, method, args, wire_request
):
    client = AsyncMock()
    client.request_json.return_value = _fixture(name)["envelope"]
    adapter = MobileAdapter(client)

    await getattr(adapter, method)(*args)

    client.request_json.assert_awaited_once_with(
        wire_request.method,
        wire_request.url,
        data=wire_request.form,
        headers=wire_request.headers,
        credentialed=False,
    )


@pytest.mark.parametrize("name", EXPECTED_WIRES)
def test_fixture_redaction_and_safe_placeholder_policy(name):
    fixture = _fixture(name)
    removed = " ".join(fixture["redaction"]["removed"]).lower()
    wire = {key: value for key, value in fixture.items() if key not in {"redaction"}}
    serialized = json.dumps(wire, ensure_ascii=False).lower()

    assert "cookie" in removed and "token" in removed and "real" in removed
    assert '"cookie"' not in serialized
    assert '"authorization"' not in serialized
    assert '"set-cookie"' not in serialized
    assert "bearer " not in serialized and "session=" not in serialized
    assert "synthetic-author" not in serialized
    assert "example.invalid" in serialized or "tag_posts" in name


def test_detail_wire_records_exact_meta_response_and_sibling_item_shape():
    fixture = _fixture("post_detail.json")
    envelope = fixture["envelope"]
    item = envelope["response"]["posts"][0]

    assert envelope["meta"] == {"status": 200, "msg": "demo"}
    assert set(item) == {"post", "blogInfo", "permalink", "blogPageUrl"}
    assert set(item["post"]) == {
        "id", "blogId", "title", "publishTime", "tag", "digest", "content",
        "photoLinks", "photoCaptions",
    }
    assert set(item["blogInfo"]) == {
        "blogId", "blogName", "blogNickName", "homePageUrl",
    }


def test_blog_wire_records_exact_page_envelope():
    fixture = _fixture("blog_home.json")
    response = fixture["envelope"]["response"]

    assert fixture["envelope"]["meta"] == {"status": 200, "msg": "demo"}
    assert set(response) == {
        "archives", "minTimeStamp", "isMember", "offset", "firstPost", "posts",
    }
    assert isinstance(response["posts"], list) and response["posts"]
    assert response["offset"] == 20
    assert response["firstPost"] is None
    assert set(fixture["variants"]["valid_empty"]) == set(response)
    assert fixture["variants"]["valid_empty"]["firstPost"] is None


def test_tag_wire_records_exact_business_data_and_post_data_shape():
    fixture = _fixture("tag_posts.json")
    envelope = fixture["envelope"]
    item = envelope["data"]["list"][0]

    assert set(envelope) == {"code", "msg", "data"}
    assert envelope["code"] == 0
    assert set(envelope["data"]) == {"list", "offset"}
    assert set(item) == {"postData"}
    assert set(item["postData"]) == {"postView", "postCount"}
    assert set(item["postData"]["postView"]) == {
        "blogId", "title", "permalink", "photoCount", "publishTime",
    }
    assert set(item["postData"]["postCount"]) == {"blogId"}


@pytest.mark.asyncio
async def test_mobile_detail_response_must_match_requested_ids():
    client = AsyncMock()
    payload = _fixture("post_detail.json")["envelope"]
    client.request_json.return_value = payload
    adapter = MobileAdapter(client)
    with pytest.raises(SourceSchemaError):
        await adapter.get_post("26", "44")


@pytest.mark.asyncio
async def test_mobile_detail_dropped_witness_must_match_requested_ids():
    client = AsyncMock()
    payload = json.loads(json.dumps(_fixture("post_detail.json")["envelope"]))
    payload["response"]["posts"][0]["post"]["title"] = 123
    client.request_json.return_value = payload
    adapter = MobileAdapter(client)

    with pytest.raises(SourceSchemaError) as exc_info:
        await adapter.get_post("26", "44")

    assert exc_info.value.location == "post_id"


@pytest.mark.asyncio
async def test_mobile_detail_matching_witness_keeps_original_schema_error():
    client = AsyncMock()
    payload = json.loads(json.dumps(_fixture("post_detail.json")["envelope"]))
    payload["response"]["posts"][0]["post"]["title"] = 123
    client.request_json.return_value = payload
    adapter = MobileAdapter(client)

    with pytest.raises(SourceSchemaError) as exc_info:
        await adapter.get_post("26", "43")

    evidence = getattr(exc_info.value, "evidence_items", ())
    assert exc_info.value.location == "title"
    assert [post.post_id for post in evidence] == ["1a_2b"]


@pytest.mark.asyncio
async def test_mobile_detail_invalid_requested_ids_fail_before_request():
    client = AsyncMock()
    adapter = MobileAdapter(client)

    with pytest.raises(SourceSchemaError) as exc_info:
        await adapter.get_post("invalid", "43")

    assert exc_info.value.location == "post_id"
    client.request_json.assert_not_awaited()
