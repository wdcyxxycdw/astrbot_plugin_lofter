from datetime import datetime, timezone

_MIN_SECONDS = 946_684_800
_MAX_SECONDS = 32_503_680_000
_MILLISECONDS_THRESHOLD = 100_000_000_000
_PUBLISH_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def format_publish_time(value: object) -> str:
    timestamp = _integer_value(value)
    if timestamp is None:
        return ""
    seconds = (
        timestamp / 1000
        if timestamp >= _MILLISECONDS_THRESHOLD
        else timestamp
    )
    if not _MIN_SECONDS <= seconds <= _MAX_SECONDS:
        return ""
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).strftime(
            _PUBLISH_TIME_FORMAT
        )
    except (OverflowError, OSError, ValueError):
        return ""


def parse_publish_time(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value, _PUBLISH_TIME_FORMAT).replace(
            tzinfo=timezone.utc
        )
        timestamp = int(parsed.timestamp())
    except (OverflowError, OSError, ValueError):
        return None
    if format_publish_time(timestamp) != value:
        return None
    return timestamp


def _integer_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return value if isinstance(value, int) else None
