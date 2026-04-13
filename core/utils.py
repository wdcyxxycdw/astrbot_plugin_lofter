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
