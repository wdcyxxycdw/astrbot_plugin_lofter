import json
import os

import pytest
from harness import API_URLS
from lofter import LofterClient


pytestmark = pytest.mark.live


@pytest.fixture
async def live_client(runtime):
    original = runtime.plugin._client
    fixture_urls = {name: getattr(runtime.client_module, name) for name in API_URLS}
    for name, url in runtime.original_api_urls.items():
        setattr(runtime.client_module, name, url)
    async with LofterClient(os.getenv("LOFTER_COOKIE", "")) as client:
        runtime.plugin._client = client
        try:
            yield client
        finally:
            runtime.plugin._client = original
            for name, url in fixture_urls.items():
                setattr(runtime.client_module, name, url)


async def test_real_tag_search_reaches_onebot_receiver(runtime, live_client):
    tag = os.environ["LOFTER_TAG"]
    _, requests = await runtime.message(f"/lofter search {tag}", timeout=120)
    assert any("标签搜索结果" in json.dumps(request, ensure_ascii=False) for request in requests), "插件未报告成功获取搜索结果"
    content_messages = [request for request in requests if "/post/" in json.dumps(request)]
    assert content_messages, "真实接口未产生可发送作品；检查标签是否有公开作品和风控状态"
    assert all(request["action"] == "send_group_msg" for request in content_messages)


async def test_real_pagination_returns_new_ids(runtime, live_client):
    tag = os.environ["LOFTER_TAG"]
    first = await live_client.fetch_tag_posts(tag)
    assert first, "分页样本标签必须有作品"
    second = await live_client.fetch_tag_posts(tag, offset=len(first))
    assert second, "分页样本标签需要足够多的作品以验证第二页"
    assert {post.post_id for post in second} - {post.post_id for post in first}, "第二页没有新增 ID，分页仍未生效"
    assert min(post.publish_time_ms for post in second) <= min(post.publish_time_ms for post in first)


async def test_real_count_against_known_expected_number(runtime, live_client):
    expression = os.getenv("LOFTER_COUNT_EXPRESSION")
    expected = os.getenv("LOFTER_EXPECTED_COUNT")
    if expression is None or expected is None:
        pytest.skip("精确统计验收需要 LOFTER_COUNT_EXPRESSION 和 LOFTER_EXPECTED_COUNT")
    _, requests = await runtime.message(f"/lofter count 真实验收 = {expression}", timeout=600)
    text = json.dumps(requests, ensure_ascii=False)
    assert f"扫描结束：已发现 {int(expected)} 个作品" in text
    assert "部分完成" not in text
    assert "统计失败" not in text
