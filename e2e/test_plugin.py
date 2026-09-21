import json
import pytest
from pathlib import Path
from urllib.parse import unquote, urlparse
import csv
from io import StringIO


def post(post_id, tag, timestamp=1720000000000):
    return {"post": {
        "blogPageUrl": f"https://author.lofter.com/post/a_{post_id}",
        "title": f"作品{post_id}", "tagList": [tag],
        "content": "<p>完整正文</p>", "publishTime": timestamp,
    }}


def dwr_posts(posts):
    return "dwr.engine._remoteHandleCallback('0','0'," + json.dumps(posts) + ");"


async def test_search_runs_through_plugin_loader_pipeline_and_onebot(runtime):
    runtime.pages[("开发测试", 0)] = dwr_posts([{"post": {
        "blogPageUrl": "https://author.lofter.com/post/a_1", "title": "真实链路测试",
        "blogInfo": {"blogNickName": "测试作者"}, "tagList": ["开发测试"],
        "digest": "<p>正文内容</p>", "publishTime": 1720000000000,
    }}])
    event, requests = await runtime.message("/lofter search 开发测试")
    assert event.unified_msg_origin == "lofter-e2e:GroupMessage:20001"
    assert any(request["action"] == "send_group_msg" for request in requests)
    payload = json.dumps(requests, ensure_ascii=False)
    assert "真实链路测试" in payload
    assert "测试作者" in payload
    assert "正文内容" in payload
    assert runtime.dwr_requests[-1]["c0-param0"] == "string:%E5%BC%80%E5%8F%91%E6%B5%8B%E8%AF%95"


async def test_count_command_scans_pages_and_sends_result(runtime):
    tag = "分页测试"
    runtime.pages[(tag, 0)] = dwr_posts([post("one", tag, 1720000000123)])
    runtime.pages[(tag, 20)] = dwr_posts([post("two", tag, 1710000000456)])
    start = len(runtime.dwr_requests)
    _, requests = await runtime.message(f"/lofter count 分页 = {tag}")
    text = json.dumps(requests, ensure_ascii=False)
    assert "已发现 2 个作品" in text
    assert "扫描结束" in text
    calls = runtime.dwr_requests[start:]
    assert [call["c0-param7"] for call in calls] == ["number:0", "number:20", "number:40"]
    assert [call["c0-param8"] for call in calls] == ["number:0", "number:1720000000123", "number:1710000000456"]


async def test_count_errors_and_repeat_pages_are_not_success(runtime):
    runtime.pages[("失效测试", 0)] = "<html>请登录</html>"
    _, requests = await runtime.message("/lofter count 失效 = 失效测试")
    text = json.dumps(requests, ensure_ascii=False)
    assert "统计失败" in text
    assert "已发现 0" not in text
    repeat = dwr_posts([post("repeat", "重复测试")])
    runtime.pages[("重复测试", 0)] = repeat
    runtime.pages[("重复测试", 20)] = repeat
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
    runtime.pages[(tag, 0)] = dwr_posts([post("seed", tag)])
    event, _ = await runtime.message(f"/lofter subtag {tag}", group_id=21001)
    session_id = event.unified_msg_origin
    runtime.pages[(tag, 0)] = dwr_posts([post(str(i), tag) for i in range(8)])
    runtime.peer.fail_send_number = runtime.peer.send_count + 2
    with pytest.raises(ActionFailed):
        await runtime.plugin._scheduler._poll_all(session_id=session_id)
    db = runtime.plugin._db
    ids = [f"a_{i}" for i in range(8)]
    unsent = await db.filter_unsent(session_id, ids)
    assert len(unsent) == 7
    assert len(await db.pending_posts(session_id, "tag", "")) == 7
    runtime.pages[(tag, 0)] = dwr_posts([])
    start = runtime.peer.send_count
    await runtime.plugin._scheduler._poll_all(session_id=session_id)
    await runtime.plugin._scheduler._poll_all(session_id=session_id)
    assert runtime.peer.send_count - start == 7
    assert await db.filter_unsent(session_id, ids) == []
    assert await db.pending_posts(session_id, "tag", "") == []


async def test_image_search_downloads_image_and_serializes_onebot_message(runtime):
    entry = post("image", "图片测试")
    entry["post"]["photoLinks"] = json.dumps([{"orign": str(runtime.http.make_url("/image.png"))}])
    runtime.pages[("图片测试", 0)] = dwr_posts([entry])
    before = runtime.image_requests
    _, requests = await runtime.message("/lofter search 图片测试")
    messages = [segment for request in requests for segment in request["params"].get("message", [])]
    image, = [segment for segment in messages if segment["type"] == "image"]
    assert image["data"]["file"].startswith("base64://")
    assert runtime.image_requests == before + 1


async def test_link_auto_parse_sends_long_text_as_group_forward_nodes(runtime):
    text = "开发测试正文" * 600
    runtime.html_pages["a_text"] = f'<html><title>长文-作者</title><p id="p_1">{text}</p></html>'
    _, requests = await runtime.message("https://author.lofter.com/post/a_text")
    forward, = [request for request in requests if request["action"] == "send_group_forward_msg"]
    nodes = forward["params"]["messages"]
    paragraphs = [segment["data"]["text"] for node in nodes[1:-1] for segment in node["data"]["content"]]
    assert "".join(paragraphs) == text
    assert len(nodes) >= 4


async def test_login_page_never_sends_an_empty_post(runtime):
    _, requests = await runtime.message("https://author.lofter.com/post/a_login")
    assert not requests


async def test_subscription_scans_past_first_page(runtime):
    tag = "补抓测试"
    runtime.pages[(tag, 0)] = dwr_posts([post("boundary", tag)])
    event, _ = await runtime.message(f"/lofter subtag {tag}", group_id=21002)
    runtime.pages[(tag, 0)] = dwr_posts([post(f"backfill{i}", tag) for i in range(20)])
    runtime.pages[(tag, 20)] = dwr_posts([post("backfill20", tag), post("boundary", tag)])
    await runtime.plugin._scheduler._poll_all(session_id=event.unified_msg_origin)
    pending = await runtime.plugin._db.pending_posts(event.unified_msg_origin, "tag", "")
    assert len(pending) == 16
    assert "a_backfill20" in {item.post_id for item in pending}


async def test_interrupted_subscription_scan_resumes_after_already_sent_first_page(runtime):
    tag = "断点测试"
    runtime.pages[(tag, 0)] = dwr_posts([post("resume_boundary", tag)])
    event, _ = await runtime.message(f"/lofter subtag {tag}", group_id=21003)
    session_id = event.unified_msg_origin
    runtime.pages[(tag, 0)] = dwr_posts([post(f"resume{i}", tag) for i in range(20)])
    runtime.pages[(tag, 20)] = "<html>临时失败</html>"
    await runtime.plugin._scheduler._poll_all(session_id=session_id)
    assert (await runtime.plugin._db.tag_scan_cursor(session_id, tag))[0] == 20
    runtime.pages[(tag, 20)] = dwr_posts([post("resume20", tag), post("resume_boundary", tag)])
    start = len(runtime.dwr_requests)
    await runtime.plugin._scheduler._poll_all(session_id=session_id)
    assert runtime.dwr_requests[start]["c0-param7"] == "number:20"
    pending = await runtime.plugin._db.pending_posts(session_id, "tag", "")
    assert len(pending) == 11
    assert "a_resume20" in {item.post_id for item in pending}
    assert await runtime.plugin._db.tag_scan_cursor(session_id, tag) == (0, 0)


async def test_count_all_sends_a_readable_csv_with_incomplete_status(runtime):
    await runtime.plugin._db.upsert_count_condition("CSV重复", "CSV测试")
    page = dwr_posts([post("csv", "CSV测试")])
    runtime.pages[("CSV测试", 0)] = page
    runtime.pages[("CSV测试", 20)] = page
    _, requests = await runtime.message("/lofter count-all")
    segments = [segment for request in requests for segment in request["params"].get("message", [])]
    file, = [segment for segment in segments if segment["type"] == "file"]
    file_path = Path(unquote(urlparse(file["data"]["file"]).path))
    rows = list(csv.DictReader(StringIO(file_path.read_text(encoding="utf-8-sig"))))
    row, = [row for row in rows if row["名称"] == "CSV重复"]
    assert row["作品数"] == "1"
    assert row["状态"] == "部分完成"
    assert "重复页" in row["错误信息"]


async def test_search_more_than_twenty_uses_timestamp_cursor(runtime):
    tag = "搜索翻页"
    runtime.pages[(tag, 0)] = dwr_posts([post(f"search{i}", tag) for i in range(20)])
    runtime.pages[(tag, 20)] = dwr_posts([post("search20", tag, 1710000000000)])
    previous = runtime.plugin._search_limit
    start = len(runtime.dwr_requests)
    runtime.plugin._search_limit = 21
    try:
        _, requests = await runtime.message(f"/lofter search {tag}")
    finally:
        runtime.plugin._search_limit = previous
    assert "作品search20" in json.dumps(requests, ensure_ascii=False)
    assert len(requests) == 22
    assert runtime.dwr_requests[start + 1]["c0-param8"] == "number:1720000000000"
