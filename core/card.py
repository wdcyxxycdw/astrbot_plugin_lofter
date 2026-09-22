"""QQ 分享卡片：把卡片携带的链接取出来。

从 LOFTER app 分享出来的是卡片而不是纯文本，message_str 和 Plain 段都是空的，
链接只存在于 json 消息段的 meta 里。
"""

import json


def card_links(message_obj) -> str:
    """卡片链接拼成一段文本，交给调用方用解析链接的正则去匹配。"""
    items = getattr(message_obj, "message", None) or []
    return "\n".join(url for url in map(_item_url, items) if url)


def _item_url(item) -> str:
    data = getattr(item, "data", None)
    if isinstance(data, str):
        data = _loads(data)
    if not isinstance(data, dict):
        return ""
    return _detail_url(data)


def _detail_url(data: dict) -> str:
    """有的适配器把卡片 JSON 原样留在 data 里，先剥一层再取 meta。"""
    nested = data.get("data")
    if isinstance(nested, str):
        data = _loads(nested) or data
    meta = data.get("meta")
    if not isinstance(meta, dict):
        return ""
    detail = meta.get("news") or meta.get("detail_1")
    if not isinstance(detail, dict):
        return ""
    return str(detail.get("jumpUrl") or detail.get("qqdocurl") or "")


def _loads(raw: str):
    try:
        return json.loads(raw)
    except ValueError:
        return None
