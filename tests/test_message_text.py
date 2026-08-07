from core.utils import extract_message_body_text


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


def test_message_object_message_list_is_used_for_plain_fallback():
    message_obj = FakeAstrBotMessage([
        FakeReply("https://quoted.lofter.com/post/abc_123"),
        FakePlain("https://body.lofter.com/post/def_456"),
    ])

    text = extract_message_body_text(message_obj, "")

    assert text == "https://body.lofter.com/post/def_456"
