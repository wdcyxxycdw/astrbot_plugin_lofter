def _split_text(text: str, limit: int = 2000, max_chunks: int = 10) -> list[str]:
    paragraphs = text.split("\n\n")
    chunks, current = [], ""
    for para in paragraphs:
        if len(para) > limit:
            if current.strip():
                chunks.append(current.strip())
                current = ""
            for i in range(0, len(para), limit):
                chunks.append(para[i:i + limit])
        elif len(current) + len(para) + 2 > limit and current:
            chunks.append(current.strip())
            current = para
        else:
            current = (current + "\n\n" + para) if current else para
    if current.strip():
        chunks.append(current.strip())
    if len(chunks) > max_chunks:
        chunks = chunks[:max_chunks]
        chunks.append("（全文过长，请点击链接查看完整内容）")
    return chunks or [text[:limit]]


def extract_message_body_text(message_obj, message_str: str) -> str:
    if message_str:
        return message_str
    if not message_obj:
        return ""
    parts: list[str] = []
    for item in _iter_message_items(message_obj):
        if _is_plain_message_item(item):
            text = getattr(item, "text", None) or getattr(item, "message", "")
            if text:
                parts.append(str(text))
    return "".join(parts)


def _iter_message_items(message_obj):
    items = getattr(message_obj, "message", None)
    if items is not None:
        try:
            return iter(items)
        except TypeError:
            return iter(())
    try:
        return iter(message_obj)
    except TypeError:
        return iter(())


def _is_plain_message_item(item) -> bool:
    item_type = str(getattr(item, "type", "") or getattr(item, "type_", ""))
    if item_type.lower() == "plain":
        return True
    return item.__class__.__name__.lower() == "plain"
