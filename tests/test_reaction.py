import pytest

from core.reaction import EMOJI_DONE, EMOJI_FAILED, EMOJI_PARSING, replace_reaction, set_reaction


class FakeBot:
    def __init__(self, error: Exception | None = None):
        self.calls = []
        self.error = error

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if self.error:
            raise self.error
        return {"status": "ok", "retcode": 0}


class FakeMessage:
    def __init__(self, message_id):
        self.message_id = message_id


class FakeEvent:
    def __init__(self, bot=None, message_id="12345"):
        if bot is not None:
            self.bot = bot
        self.message_obj = FakeMessage(message_id)


async def test_set_reaction_calls_napcat_action_with_expected_params():
    bot = FakeBot()

    assert await set_reaction(FakeEvent(bot), EMOJI_PARSING) is True
    assert bot.calls == [
        ("set_msg_emoji_like", {"message_id": "12345", "emoji_id": EMOJI_PARSING, "set": True}),
    ]


async def test_set_reaction_sends_string_message_id_for_numeric_ids():
    bot = FakeBot()

    await set_reaction(FakeEvent(bot, message_id=98765), EMOJI_PARSING)

    assert bot.calls[0][1]["message_id"] == "98765"


async def test_set_reaction_unsets_when_off():
    bot = FakeBot()

    await set_reaction(FakeEvent(bot), EMOJI_PARSING, on=False)

    assert bot.calls[0][1]["set"] is False


async def test_set_reaction_skips_platforms_without_call_action():
    assert await set_reaction(FakeEvent(bot=None), EMOJI_PARSING) is False


async def test_set_reaction_skips_events_without_message_id():
    bot = FakeBot()

    assert await set_reaction(FakeEvent(bot, message_id=""), EMOJI_PARSING) is False
    assert bot.calls == []


async def test_set_reaction_swallows_action_errors():
    bot = FakeBot(error=RuntimeError("emoji not in whitelist"))

    assert await set_reaction(FakeEvent(bot), 999999) is False


async def test_replace_reaction_removes_old_then_adds_new():
    bot = FakeBot()

    await replace_reaction(FakeEvent(bot), EMOJI_PARSING, EMOJI_DONE)

    assert [(params["emoji_id"], params["set"]) for _, params in bot.calls] == [
        (EMOJI_PARSING, False),
        (EMOJI_DONE, True),
    ]


async def test_replace_reaction_without_result_only_removes():
    bot = FakeBot()

    await replace_reaction(FakeEvent(bot), EMOJI_PARSING, None)

    assert [(params["emoji_id"], params["set"]) for _, params in bot.calls] == [(EMOJI_PARSING, False)]


async def test_replace_reaction_keeps_identical_emoji_untouched():
    bot = FakeBot()

    await replace_reaction(FakeEvent(bot), EMOJI_PARSING, EMOJI_PARSING)

    assert bot.calls == []


@pytest.mark.parametrize("emoji_id", [EMOJI_PARSING, EMOJI_DONE, EMOJI_FAILED])
def test_default_emoji_ids_are_plain_integers(emoji_id):
    assert isinstance(emoji_id, int)
