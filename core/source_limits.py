from __future__ import annotations

from .errors import SourceLimitError, SourceSchemaError

MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_ITEMS = 100
MAX_TITLE_BYTES = 4 * 1024
MAX_URL_BYTES = 8 * 1024
MAX_CONTENT_BYTES = 2 * 1024 * 1024


def validate_text_bytes(value: object, resource: str, limit: int) -> str:
    if not isinstance(value, str):
        raise SourceSchemaError(resource)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise SourceSchemaError(resource) from None
    if size > limit:
        raise SourceLimitError(resource, limit)
    return value
