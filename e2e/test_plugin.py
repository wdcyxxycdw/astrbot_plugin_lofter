import json
import pytest
from pathlib import Path
from urllib.parse import unquote, urlparse
import csv
from io import StringIO


BLOG_HEX = "1d038690"


def permalink(index):
    return f"{BLOG_HEX}_{index:x}"


def post(index, tag, timestamp=1720000000000):
    return {"post": {
        "blogPageUrl": f"https://author.lofter.com/post/{permalink(index)}",
        "permalink": permalink(index),
        "blogInfo": {"blogNickName": "测试作者", "blogName": "author"},
        "title": f"作品{index}", "tagList": [tag],
        "content": "<p>完整正文</p>", "publishTime": timestamp,
    }}


async def test_plugin_uses_installed_fetch_package(runtime):
    from lofter import LofterClient, Post

    assert type(runtime.plugin._client) is LofterClient
    assert runtime.client_module.__name__ == "lofter.client"
    runtime.pages[("依赖验证", 0)] = [post(1, "依赖验证")]
    parsed, = await runtime.plugin._client.fetch_tag_posts("依赖验证")
    assert type(parsed) is Post
    assert parsed.post_id == permalink(1)


@pytest.mark.parametrize("module_name", ["count_commands", "llm_tools"])
async def test_plugin_uses_astrbot_logger(runtime, module_name):
    import importlib

    from astrbot.api import logger

    package = runtime.plugin.__module__.rsplit(".", 1)[0]
    module = importlib.import_module(f"{package}.core.{module_name}")
    assert module.logger is logger


async def test_permalink_diagnostic_uses_public_package_helper(runtime):
    import importlib

    module = importlib.import_module(runtime.plugin.__module__.rsplit(".", 1)[0] + ".core.e2e_test")
    plugin = runtime.plugin
    runner = module.E2ETestRunner(
        plugin._db, plugin._client, plugin._storage, plugin._scheduler, plugin._send_push,
    )
    result = await runner._step_02_permalink_decode()
    assert result.status == "pass", result.error
    assert "样本 permalink 解码结果: blogId=486770320, postId=14215864220" in result.details


async def test_search_runs_through_plugin_loader_pipeline_and_onebot(runtime):
    runtime.pages[("开发测试", 0)] = [{"post": {
        "blogPageUrl": f"https://author.lofter.com/post/{permalink(1)}",
        "permalink": permalink(1), "title": "真实链路测试",
        "blogInfo": {"blogNickName": "测试作者", "blogName": "author"}, "tagList": ["开发测试"],
        "digest": "<p>正文内容</p>", "publishTime": 1720000000000,
    }}]
    event, requests = await runtime.message("/lofter search 开发测试")
    assert event.unified_msg_origin == "lofter-e2e:GroupMessage:20001"
    assert any(request["action"] == "send_group_msg" for request in requests)
    payload = json.dumps(requests, ensure_ascii=False)
    assert "真实链路测试" in payload
    assert "测试作者" in payload
    assert "正文内容" in payload
    assert runtime.api_requests[-1]["tag"] == "开发测试"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("sub-tag", "请提供标签名，例如：/lofter sub-tag 原创"),
        ("sub-tag-preview", "请提供标签名，例如：/lofter sub-tag-preview 原创"),
        ("sub-blog", "请提供博主用户名，例如：/lofter sub-blog username"),
        ("unsub-tag", "请提供标签名"),
        ("unexclude-tag", "请提供标签名"),
        ("unsub-blog", "请提供博主用户名"),
    ],
)
async def test_hyphenated_subscription_commands_are_registered(runtime, command, expected):
    _, requests = await runtime.message(f"/lofter {command}")
    text = json.dumps(requests, ensure_ascii=False)
    assert any(request["action"] == "send_group_msg" for request in requests)
    assert expected in text


async def test_count_command_scans_pages_and_sends_result(runtime):
    tag = "分页测试"
    runtime.pages[(tag, 0)] = [post(1, tag, 1720000000123)]
    runtime.pages[(tag, 1)] = [post(2, tag, 1710000000456)]
    start = len(runtime.api_requests)
    _, requests = await runtime.message(f"/lofter count 分页 = {tag}")
    text = json.dumps(requests, ensure_ascii=False)
    assert "已发现 2 个作品" in text
    assert "扫描结束" in text
    calls = runtime.api_requests[start:]
    assert [call["offset"] for call in calls] == ["0", "1", "2"]


async def test_count_errors_and_repeat_pages_are_not_success(runtime):
    runtime.pages[("失效测试", 0)] = "<html>请登录</html>"
    _, requests = await runtime.message("/lofter count 失效 = 失效测试")
    text = json.dumps(requests, ensure_ascii=False)
    assert "统计失败" in text
    assert "已发现 0" not in text
    repeat = [post(1, "重复测试")]
    runtime.pages[("重复测试", 0)] = repeat
    runtime.pages[("重复测试", 1)] = repeat
    _, requests = await runtime.message("/lofter count 重复 = 重复测试")
    text = json.dumps(requests, ensure_ascii=False)
    assert "部分完成" in text
    assert "重复页" in text


async def test_non_admin_cannot_change_global_cookie(runtime):
    original = runtime.plugin._client._cookie
    _, requests = await runtime.message("/lofter cookie invalid", user_id=10002)
    assert requests
    assert runtime.plugin._client._cookie == original


async def test_failed_send_retries_without_repeating_delivered_posts(runtime):
    from aiocqhttp.exceptions import ActionFailed

    tag = "推送重试"
    runtime.pages[(tag, 0)] = [post(900, tag)]
    event, _ = await runtime.message(f"/lofter sub-tag {tag}", group_id=21001)
    session_id = event.unified_msg_origin
    runtime.pages[(tag, 0)] = [post(i, tag) for i in range(8)]
    runtime.peer.fail_send_number = runtime.peer.send_count + 2
    with pytest.raises(ActionFailed):
        await runtime.plugin._scheduler._poll_all(session_id=session_id)
    db = runtime.plugin._db
    ids = [permalink(i) for i in range(8)]
    unsent = await db.filter_unsent(session_id, ids)
    assert len(unsent) == 7
    assert len(await db.pending_posts(session_id, "tag", "")) == 7
    runtime.pages[(tag, 0)] = []
    start = runtime.peer.send_count
    await runtime.plugin._scheduler._poll_all(session_id=session_id)
    await runtime.plugin._scheduler._poll_all(session_id=session_id)
    assert runtime.peer.send_count - start == 7
    assert await db.filter_unsent(session_id, ids) == []
    assert await db.pending_posts(session_id, "tag", "") == []


async def test_image_search_downloads_image_and_serializes_onebot_message(runtime):
    entry = post(2, "图片测试")
    entry["post"]["photoLinks"] = json.dumps([{"orign": str(runtime.http.make_url("/image.png"))}])
    runtime.pages[("图片测试", 0)] = [entry]
    before = runtime.image_requests
    _, requests = await runtime.message("/lofter search 图片测试")
    messages = [segment for request in requests for segment in request["params"].get("message", [])]
    image, = [segment for segment in messages if segment["type"] == "image"]
    assert image["data"]["file"].startswith("base64://")
    assert runtime.image_requests == before + 1


async def test_link_auto_parse_sends_long_text_as_group_forward_nodes(runtime):
    text = "开发测试正文" * 600
    entry = post(0x7E77, "长文测试")
    entry["post"]["content"] = f"<p>{text}</p>"
    runtime.post_pages[permalink(0x7E77)] = [entry]
    _, requests = await runtime.message(f"https://author.lofter.com/post/{permalink(0x7E77)}")
    forward, = [request for request in requests if request["action"] == "send_group_forward_msg"]
    nodes = forward["params"]["messages"]
    paragraphs = [segment["data"]["text"] for node in nodes[1:-1] for segment in node["data"]["content"]]
    assert "".join(paragraphs) == text
    assert len(nodes) >= 4


async def test_login_page_never_sends_an_empty_post(runtime):
    _, requests = await runtime.message(f"https://author.lofter.com/post/{permalink(0xB0B)}")
    assert not [request for request in requests if request["action"].startswith("send_")]


def emoji_reactions(requests):
    return [
        (request["params"]["emoji_id"], request["params"]["set"])
        for request in requests
        if request["action"] == "set_msg_emoji_like"
    ]


async def test_link_auto_parse_marks_progress_with_emoji_reaction(runtime):
    entry = post(0x5EAC, "表情测试")
    entry["post"]["content"] = "<p>正文</p>"
    runtime.post_pages[permalink(0x5EAC)] = [entry]

    _, requests = await runtime.message(f"https://author.lofter.com/post/{permalink(0x5EAC)}")

    assert emoji_reactions(requests) == [(128064, True), (128064, False), (124, True)]


async def test_link_auto_parse_marks_failure_reaction_when_post_is_unavailable(runtime):
    _, requests = await runtime.message(f"https://author.lofter.com/post/{permalink(0xB0B)}")

    assert emoji_reactions(requests) == [(128064, True), (128064, False), (123, True)]


async def test_reaction_is_cleared_when_the_block_list_query_fails(runtime, monkeypatch):
    """贴上 👀 之后的任何一步出错都得收尾，否则表情会永远留在用户消息上。"""
    entry = post(0x5EAD, "表情兜底")
    entry["post"]["content"] = "<p>正文</p>"
    runtime.post_pages[permalink(0x5EAD)] = [entry]

    async def unavailable(_session_id):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(runtime.plugin._author_blocks, "list_by_session", unavailable)

    _, requests = await runtime.message(f"https://author.lofter.com/post/{permalink(0x5EAD)}")

    assert emoji_reactions(requests) == [(128064, True), (128064, False), (123, True)]
    assert not [request for request in requests if request["action"].startswith("send_")]


async def test_subscription_scans_past_first_page(runtime):
    tag = "补抓测试"
    runtime.pages[(tag, 0)] = [post(901, tag)]
    event, _ = await runtime.message(f"/lofter sub-tag {tag}", group_id=21002)
    runtime.pages[(tag, 0)] = [post(i, tag) for i in range(20)]
    runtime.pages[(tag, 20)] = [post(20, tag), post(901, tag)]
    await runtime.plugin._scheduler._poll_all(session_id=event.unified_msg_origin)
    pending = await runtime.plugin._db.pending_posts(event.unified_msg_origin, "tag", "")
    assert len(pending) == 16
    assert permalink(20) in {item.post_id for item in pending}


async def test_interrupted_subscription_scan_resumes_after_already_sent_first_page(runtime):
    tag = "断点测试"
    runtime.pages[(tag, 0)] = [post(902, tag)]
    event, _ = await runtime.message(f"/lofter sub-tag {tag}", group_id=21003)
    session_id = event.unified_msg_origin
    runtime.pages[(tag, 0)] = [post(i, tag) for i in range(20)]
    runtime.pages[(tag, 20)] = "<html>临时失败</html>"
    await runtime.plugin._scheduler._poll_all(session_id=session_id)
    assert await runtime.plugin._db.tag_scan_cursor(session_id, tag) == 20
    runtime.pages[(tag, 20)] = [post(20, tag), post(902, tag)]
    start = len(runtime.api_requests)
    await runtime.plugin._scheduler._poll_all(session_id=session_id)
    assert runtime.api_requests[start]["offset"] == "20"
    pending = await runtime.plugin._db.pending_posts(session_id, "tag", "")
    assert len(pending) == 11
    assert permalink(20) in {item.post_id for item in pending}
    assert await runtime.plugin._db.tag_scan_cursor(session_id, tag) == 0


async def test_count_all_sends_a_readable_csv_with_incomplete_status(runtime):
    await runtime.plugin._db.upsert_count_condition("CSV重复", "CSV测试")
    page = [post(3, "CSV测试")]
    runtime.pages[("CSV测试", 0)] = page
    runtime.pages[("CSV测试", 1)] = page
    _, requests = await runtime.message("/lofter count-all")
    segments = [segment for request in requests for segment in request["params"].get("message", [])]
    file, = [segment for segment in segments if segment["type"] == "file"]
    file_path = Path(unquote(urlparse(file["data"]["file"]).path))
    rows = list(csv.DictReader(StringIO(file_path.read_text(encoding="utf-8-sig"))))
    row, = [row for row in rows if row["名称"] == "CSV重复"]
    assert row["作品数"] == "1"
    assert row["状态"] == "部分完成"
    assert "重复页" in row["错误信息"]


async def test_search_more_than_twenty_follows_server_offset(runtime):
    tag = "搜索翻页"
    runtime.pages[(tag, 0)] = [post(i, tag) for i in range(20)]
    runtime.pages[(tag, 20)] = [post(20, tag, 1710000000000)]
    previous = runtime.plugin._search_limit
    start = len(runtime.api_requests)
    runtime.plugin._search_limit = 21
    try:
        _, requests = await runtime.message(f"/lofter search {tag}")
    finally:
        runtime.plugin._search_limit = previous
    assert "作品20" in json.dumps(requests, ensure_ascii=False)
    assert len(requests) == 22
    assert [call["offset"] for call in runtime.api_requests[start:start + 2]] == ["0", "20"]
