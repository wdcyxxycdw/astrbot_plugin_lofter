from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.e2e_steps_network import NetworkStepsMixin
from core.parser import Post
from core.utils import extract_message_body_text
from tests.test_command_permissions import _load_main_module


class FakePlain:
    type = "Plain"

    def __init__(self, text: str):
        self.text = text


class FakeReply:
    type = "Reply"

    def __init__(self, text: str):
        self.text = text

    def __str__(self) -> str:
        return self.text


class FakeMessageChain(list):
    def __str__(self) -> str:
        return "".join(str(item) for item in self)


class FakeAstrBotMessage:
    def __init__(self, message):
        self.message = message


def test_message_str_is_used_instead_of_reply_text():
    message_obj = FakeMessageChain([
        FakeReply("https://quoted.lofter.com/post/abc_123"),
        FakePlain("收到"),
    ])

    text = extract_message_body_text(message_obj, "收到")

    assert text == "收到"


def test_empty_message_str_falls_back_to_plain_components_only():
    message_obj = FakeMessageChain([
        FakeReply("https://quoted.lofter.com/post/abc_123"),
        FakePlain("https://body.lofter.com/post/def_456"),
    ])

    text = extract_message_body_text(message_obj, "")

    assert text == "https://body.lofter.com/post/def_456"


def test_reply_link_is_ignored_when_body_has_no_text():
    message_obj = FakeMessageChain([
        FakeReply("https://quoted.lofter.com/post/abc_123"),
    ])

    text = extract_message_body_text(message_obj, "")

    assert text == ""


def test_group_long_post_uses_formatter_diagnostic_once():
    main = _load_main_module()
    main.Comp = SimpleNamespace(
        Plain=lambda text: SimpleNamespace(text=text),
        Node=lambda **kwargs: SimpleNamespace(**kwargs),
        Nodes=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    event = SimpleNamespace(
        unified_msg_origin="GroupMessage:sess",
        chain_result=lambda chain: chain,
    )
    post = Post(
        post_id="abc_123",
        title="secret title",
        summary="",
        content="正文内容",
        url="https://demo.lofter.com/post/abc_123",
        source="embedded_json",
        completeness=frozenset({"content", "url"}),
        provenance={"content": "embedded_json", "url": "canonical_url"},
    )

    chain = main._auto_post_result(event, post, post.url, 3)
    nodes = chain[0].nodes
    texts = [part.text for node in nodes for part in node.content]

    assert "来源：embedded_json" in texts[0]
    assert "部分字段未知" in texts[0]
    assert "secret title" not in "\n".join(texts)
    assert "\n".join(texts).count(post.url) == 1


@pytest.mark.asyncio
async def test_auto_parse_keeps_post_with_unknown_body_and_images():
    main = _load_main_module()
    main.Comp = SimpleNamespace(
        Plain=lambda text: SimpleNamespace(text=text),
        Image=SimpleNamespace(fromURL=lambda url: SimpleNamespace(url=url)),
    )
    post = Post(
        post_id="abc_123",
        title="可见标题",
        summary="",
        url="https://demo.lofter.com/post/abc_123",
        source="mobile_tag",
        completeness=frozenset({"title", "url"}),
    )
    plugin = object.__new__(main.LofterPlugin)
    plugin._source = SimpleNamespace(get_post=AsyncMock(return_value=post))
    plugin._author_blocks = SimpleNamespace(
        list_by_session=AsyncMock(return_value=[])
    )
    plugin._max_images = 3
    event = SimpleNamespace(
        message_obj=[],
        message_str=post.url,
        unified_msg_origin="FriendMessage:sess",
        chain_result=lambda chain: chain,
    )

    results = [item async for item in plugin.auto_parse(event)]

    assert len(results) == 1
    assert "可见标题" in results[0][0].text
    assert post.url in results[0][0].text
    assert "部分字段未知" in results[0][0].text


class _NetworkStepProbe(NetworkStepsMixin):
    def __init__(self, post):
        self._artifacts = {"rich_post": post, "tag_posts": [post]}

    def _timed_start(self):
        return 0

    def _timed_end(self, started):
        return 0

    def _pass(self, name, duration, details):
        return SimpleNamespace(name=name, status="pass", details=details)

    def _fail(self, name, duration, error, details):
        return SimpleNamespace(
            name=name, status="fail", error=str(error), details=details
        )

    def _skip(self, name, reason):
        return SimpleNamespace(name=name, status="skip", details=[reason])


@pytest.mark.asyncio
async def test_e2e_post_diagnostic_reports_unknown_images():
    post = Post(
        post_id="abc_123",
        title="标题",
        summary="",
        images=["https://secret.invalid/image.jpg"],
        url="https://demo.lofter.com/post/abc_123",
        completeness=frozenset({"title", "url"}),
    )

    result = await _NetworkStepProbe(post)._step_08_post_parse()

    assert "images=unknown" in result.details[0]
    assert "images=1" not in result.details[0]


@pytest.mark.asyncio
async def test_e2e_format_step_accepts_partial_tag_post():
    post = Post(
        post_id="abc_123",
        title="标签结果",
        summary="可信摘要",
        url="https://demo.lofter.com/post/abc_123",
        source="mobile_tag",
        completeness=frozenset({"title", "summary", "url"}),
    )

    result = await _NetworkStepProbe(post)._step_11_format()

    assert result.status == "pass"
    assert result.details[-1] == "format_post(include_time=True) OK"


def test_message_object_message_list_is_used_for_plain_fallback():
    message_obj = FakeAstrBotMessage([
        FakeReply("https://quoted.lofter.com/post/abc_123"),
        FakePlain("https://body.lofter.com/post/def_456"),
    ])

    text = extract_message_body_text(message_obj, "")

    assert text == "https://body.lofter.com/post/def_456"
