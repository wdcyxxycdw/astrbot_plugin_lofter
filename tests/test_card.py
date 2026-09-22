import json

import pytest

from core.card import card_links

LINK = "https://amingdeaierlankafei.lofter.com/post/1fcc68a3_34f550485?incantation=rzaeUZIxgKdy"

APP_SHARE = {
    "app": "com.tencent.tuwen.lua",
    "bizsrc": "qqconnect.sdkshare",
    "meta": {"news": {
        "desc": "爱尔兰咖啡屋 / 白夜",
        "jumpUrl": LINK,
        "preview": "https://pic.ugcimg.cn/df30ff5740e682bb58f478579aada67d/jpg1",
        "tag": "LOFTER",
        "title": "白夜",
    }},
    "prompt": "[分享]白夜",
    "view": "news",
}


class FakeJson:
    type = "Json"

    def __init__(self, data):
        self.data = data


class FakePlain:
    type = "Plain"

    def __init__(self, text):
        self.text = text


class FakeMessage:
    def __init__(self, message):
        self.message = message


def test_app_share_card_yields_the_shared_link():
    assert card_links(FakeMessage([FakeJson(APP_SHARE)])) == LINK


def test_card_json_left_as_a_string_is_unwrapped():
    assert card_links(FakeMessage([FakeJson(json.dumps(APP_SHARE))])) == LINK


def test_card_nested_under_a_data_key_is_unwrapped():
    nested = FakeJson({"data": json.dumps(APP_SHARE)})

    assert card_links(FakeMessage([nested])) == LINK


def test_detail_1_style_card_uses_qqdocurl():
    card = {"meta": {"detail_1": {"title": "白夜", "qqdocurl": LINK}}}

    assert card_links(FakeMessage([FakeJson(card)])) == LINK


def test_plain_text_segments_contribute_nothing():
    assert card_links(FakeMessage([FakePlain(LINK)])) == ""


def test_card_without_a_recognised_meta_is_skipped():
    assert card_links(FakeMessage([FakeJson({"meta": {"other": {"url": LINK}}})])) == ""
    assert card_links(FakeMessage([FakeJson({"app": "com.tencent.tuwen.lua"})])) == ""


def test_malformed_card_json_does_not_raise():
    assert card_links(FakeMessage([FakeJson("{不是 JSON")])) == ""


@pytest.mark.parametrize("nested", ["[1, 2]", "123", '"x"', "null", "true"])
def test_nested_data_that_is_not_an_object_is_ignored(nested):
    """卡片里塞一个能解析但不是对象的 data，不能把整条消息的处理链带崩。"""
    assert card_links(FakeMessage([FakeJson({"data": nested})])) == ""


def test_nested_data_that_is_not_an_object_falls_back_to_the_outer_card():
    card = dict(APP_SHARE, data="[1, 2]")

    assert card_links(FakeMessage([FakeJson(card)])) == LINK


def test_deeply_nested_card_json_does_not_raise():
    assert card_links(FakeMessage([FakeJson("[" * 200_000)])) == ""


def test_several_cards_are_listed_one_per_line():
    other = {"meta": {"news": {"jumpUrl": "https://b.lofter.com/post/aaaa_1111"}}}

    links = card_links(FakeMessage([FakeJson(APP_SHARE), FakeJson(other)]))

    assert links.splitlines() == [LINK, "https://b.lofter.com/post/aaaa_1111"]


def test_message_without_segments_is_empty():
    assert card_links(FakeMessage([])) == ""
    assert card_links(None) == ""
