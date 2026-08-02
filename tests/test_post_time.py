import pytest

from core.post_time import format_publish_time, parse_publish_time


@pytest.mark.parametrize(
    ("text", "timestamp"),
    [
        ("2000-01-01 00:00:00", 946684800),
        ("2024-02-29 12:34:56", 1709210096),
        ("3000-01-01 00:00:00", 32503680000),
    ],
)
def test_publish_time_round_trip(text, timestamp):
    assert parse_publish_time(text) == timestamp
    assert format_publish_time(timestamp) == text


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        1709210096,
        "",
        "2024-2-29 12:34:56",
        "2024-02-29T12:34:56",
        "2024-02-30 12:34:56",
        "1999-12-31 23:59:59",
        "3001-01-01 00:00:00",
        "2024-02-29 12:34:56Z",
    ],
)
def test_parse_publish_time_rejects_noncanonical_values(value):
    assert parse_publish_time(value) is None
