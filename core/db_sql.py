from __future__ import annotations


def strip_sql_comments(sql: str) -> str:
    result: list[str] = []
    index = 0
    quote = ""
    while index < len(sql):
        char = sql[index]
        if quote:
            index, quote = _copy_quoted_char(sql, index, quote, result)
            index += 1
            continue
        if char in "'\"`":
            quote = char
            result.append(char)
            index += 1
            continue
        if char == "[":
            quote = "]"
            result.append(char)
            index += 1
            continue
        if sql.startswith("--", index):
            index = _skip_line_comment(sql, index)
            result.append("\n")
            index += 1
            continue
        if sql.startswith("/*", index):
            index = _skip_block_comment(sql, index)
            result.append(" ")
            index += 1
            continue
        result.append(char)
        index += 1
    return "".join(result)


def canonical_sql(sql: str) -> str:
    cleaned = strip_sql_comments(sql)
    result: list[str] = []
    index = 0
    while index < len(cleaned):
        char = cleaned[index]
        if char == "'":
            literal, index = _quoted_text(cleaned, index, "'")
            result.append(literal)
            index += 1
            continue
        if char in "\"`[":
            closer = "]" if char == "[" else char
            identifier, index = _quoted_text(cleaned, index, closer)
            result.append(identifier[1:-1].lower())
            index += 1
            continue
        if not char.isspace():
            result.append(char.lower())
        index += 1
    return "".join(result)


def extract_checks(sql: str) -> tuple[str, ...]:
    canonical = canonical_sql(sql)
    checks: list[str] = []
    cursor = 0
    while True:
        start = _find_outside_literal(canonical, "check(", cursor)
        if start < 0:
            return tuple(checks)
        expression_start = start + len("check(")
        end = matching_parenthesis(canonical, expression_start)
        if end < 0:
            return tuple(checks)
        checks.append(canonical[expression_start:end])
        cursor = end + 1


def matching_parenthesis(sql: str, start: int) -> int:
    depth = 1
    index = start
    while index < len(sql):
        if sql[index] == "'":
            _, index = _quoted_text(sql, index, "'")
            index += 1
            continue
        depth += (sql[index] == "(") - (sql[index] == ")")
        if depth == 0:
            return index
        index += 1
    return -1


def contains_sql_keyword(sql: str, *keywords: str) -> bool:
    wanted = {keyword.lower() for keyword in keywords}
    return bool(wanted.intersection(sql_tokens(strip_sql_comments(sql))))


def explicit_column_collations(sql: str, columns: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for definition in table_definitions(sql):
        tokens = sql_tokens(definition)
        if not tokens or tokens[0] not in columns or "collate" not in tokens:
            continue
        position = tokens.index("collate")
        result[tokens[0]] = tokens[position + 1] if position + 1 < len(tokens) else ""
    return result


def table_definitions(sql: str) -> tuple[str, ...]:
    cleaned = strip_sql_comments(sql)
    start = _find_outside_quotes(cleaned, "(")
    if start < 0:
        return ()
    definitions: list[str] = []
    segment_start = start + 1
    depth = 1
    index = segment_start
    while index < len(cleaned):
        char = cleaned[index]
        if char in "'\"`[":
            closer = "]" if char == "[" else char
            _, index = _quoted_text(cleaned, index, closer)
            index += 1
            continue
        if char == "(":
            depth += 1
        if char == ")":
            depth -= 1
            if depth == 0:
                definitions.append(cleaned[segment_start:index])
                return tuple(definitions)
        if char == "," and depth == 1:
            definitions.append(cleaned[segment_start:index])
            segment_start = index + 1
        index += 1
    return ()


def sql_tokens(sql: str) -> tuple[str, ...]:
    tokens: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "'":
            _flush_token(tokens, current)
            _, index = _quoted_text(sql, index, "'")
            index += 1
            continue
        if char in "\"`[":
            _flush_token(tokens, current)
            closer = "]" if char == "[" else char
            text, index = _quoted_text(sql, index, closer)
            tokens.append(text[1:-1].lower())
            index += 1
            continue
        if char.isalnum() or char == "_":
            current.append(char.lower())
        else:
            _flush_token(tokens, current)
        index += 1
    _flush_token(tokens, current)
    return tuple(tokens)


def _copy_quoted_char(
    sql: str, index: int, quote: str, result: list[str]
) -> tuple[int, str]:
    char = sql[index]
    result.append(char)
    if char != quote:
        return index, quote
    if _is_escaped_quote(sql, index, quote):
        result.append(sql[index + 1])
        return index + 1, quote
    return index, ""


def _find_outside_literal(sql: str, needle: str, start: int) -> int:
    index = start
    while index <= len(sql) - len(needle):
        if sql[index] == "'":
            _, index = _quoted_text(sql, index, "'")
            index += 1
            continue
        if sql.startswith(needle, index):
            return index
        index += 1
    return -1


def _find_outside_quotes(sql: str, needle: str) -> int:
    index = 0
    while index < len(sql):
        if sql[index] in "'\"`[":
            closer = "]" if sql[index] == "[" else sql[index]
            _, index = _quoted_text(sql, index, closer)
            index += 1
            continue
        if sql[index] == needle:
            return index
        index += 1
    return -1


def _quoted_text(sql: str, start: int, closer: str) -> tuple[str, int]:
    index = start + 1
    while index < len(sql):
        if sql[index] != closer:
            index += 1
            continue
        if _is_escaped_quote(sql, index, closer):
            index += 2
            continue
        return sql[start:index + 1], index
    return sql[start:], len(sql) - 1


def _is_escaped_quote(sql: str, index: int, quote: str) -> bool:
    return index + 1 < len(sql) and sql[index + 1] == quote


def _skip_line_comment(sql: str, start: int) -> int:
    newline = sql.find("\n", start + 2)
    return len(sql) - 1 if newline < 0 else newline


def _skip_block_comment(sql: str, start: int) -> int:
    end = sql.find("*/", start + 2)
    return len(sql) - 1 if end < 0 else end + 1


def _flush_token(tokens: list[str], current: list[str]) -> None:
    if current:
        tokens.append("".join(current))
        current.clear()
