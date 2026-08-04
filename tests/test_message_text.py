from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.formatter import format_post
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


class FakePlainComponent:
    def __init__(self, text):
        self.text = text


class FakeImageComponent:
    def __init__(self, url):
        self.url = url


class FakeNodeComponent:
    def __init__(self, content, **kwargs):
        self.content = content
        self.__dict__.update(kwargs)


class FakeNodesComponent:
    def __init__(self, nodes):
        self.nodes = nodes


def _message_components():
    return SimpleNamespace(
        Plain=FakePlainComponent,
        Image=SimpleNamespace(fromURL=FakeImageComponent),
        Node=FakeNodeComponent,
        Nodes=FakeNodesComponent,
    )


def _auto_event(platform="aiocqhttp", group_id="", private=True, self_id="10000"):
    return SimpleNamespace(
        get_platform_name=lambda: platform,
        get_group_id=lambda: group_id,
        is_private_chat=lambda: private,
        get_self_id=lambda: self_id,
        chain_result=lambda chain: chain,
    )


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


def test_qq_group_long_post_uses_plain_then_nodes_with_self_uin():
    main = _load_main_module()
    main.Comp = _message_components()
    event = _auto_event(group_id="group", private=False, self_id="12345")
    post = Post(
        post_id="abc_123",
        title="secret title",
        summary="可信摘要",
        content="正文内容" * 150,
        author="作者",
        url="https://demo.lofter.com/post/abc_123",
        source="embedded_json",
        completeness=frozenset({
            "title", "summary", "content", "author", "url"
        }),
        provenance={"content": "embedded_json", "url": "canonical_url"},
    )

    chain = main._auto_post_result(event, post, 3)

    assert isinstance(chain[0], FakePlainComponent)
    assert chain[0].text == format_post(post)
    assert isinstance(chain[1], FakeNodesComponent)
    assert all(node.uin == "12345" for node in chain[1].nodes)
    assert "正文内容" in "\n".join(
        part.text for node in chain[1].nodes for part in node.content
    )


def test_qq_private_long_post_uses_plain_without_group_nodes():
    main = _load_main_module()
    main.Comp = _message_components()
    post = Post(
        post_id="abc_123", title="标题", summary="摘要",
        content="正文" * 300, url="https://demo.lofter.com/post/abc_123",
        completeness=frozenset({"title", "summary", "content", "url"}),
    )

    chain = main._auto_post_result(_auto_event(private=True), post, 3)

    assert len(chain) == 1
    assert isinstance(chain[0], FakePlainComponent)
    assert chain[0].text == format_post(post)


def test_non_qq_auto_parse_uses_plain_and_truncated_images():
    main = _load_main_module()
    main.Comp = _message_components()
    post = Post(
        post_id="abc_123", title="标题", summary="摘要",
        images=["https://img/1.jpg", "https://img/2.jpg", "https://img/3.jpg"],
        url="https://demo.lofter.com/post/abc_123",
        completeness=frozenset({"title", "summary", "images", "url"}),
    )

    chain = main._auto_post_result(
        _auto_event(platform="telegram"), post, 2
    )

    assert isinstance(chain[0], FakePlainComponent)
    assert [item.url for item in chain[1:]] == post.images[:2]
    assert not any(isinstance(item, FakeNodesComponent) for item in chain)


@pytest.mark.asyncio
async def test_auto_parse_keeps_post_with_unknown_body_and_images():
    main = _load_main_module()
    main.Comp = _message_components()
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
        unified_msg_origin="test:FriendMessage:sess",
        get_platform_name=lambda: "test",
        get_group_id=lambda: "",
        is_private_chat=lambda: True,
        get_self_id=lambda: "bot",
        chain_result=lambda chain: chain,
    )

    results = [item async for item in plugin.auto_parse(event)]

    assert len(results) == 1
    assert "可见标题" in results[0][0].text
    assert post.url in results[0][0].text
    assert "部分字段未知" in results[0][0].text


def test_message_object_message_list_is_used_for_plain_fallback():
    message_obj = FakeAstrBotMessage([
        FakeReply("https://quoted.lofter.com/post/abc_123"),
        FakePlain("https://body.lofter.com/post/def_456"),
    ])

    text = extract_message_body_text(message_obj, "")

    assert text == "https://body.lofter.com/post/def_456"
