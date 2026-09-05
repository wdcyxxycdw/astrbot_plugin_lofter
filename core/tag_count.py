from __future__ import annotations

import asyncio
import csv
import aiohttp
from dataclasses import dataclass, field
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Literal

from .dwr_parser import parse_dwr_response
from .parser import Post


class CountExpressionError(ValueError):
    pass


@dataclass(frozen=True)
class CountResult:
    name: str
    expression: str
    count: int
    status: str
    error: str
    counted_at: str
    candidates: int = 0
    scanned_pages: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TagScanResult:
    tag: str
    candidate_ids: set[str]
    matched_ids: set[str]
    scanned_pages: int
    warnings: list[str]
    complete: bool = False


@dataclass(frozen=True)
class TagNode:
    tag: str


@dataclass(frozen=True)
class UnaryNode:
    op: Literal["not"]
    child: "ExprNode"


@dataclass(frozen=True)
class BinaryNode:
    op: Literal["and", "or"]
    left: "ExprNode"
    right: "ExprNode"


ExprNode = TagNode | UnaryNode | BinaryNode


@dataclass(frozen=True)
class Token:
    kind: Literal["tag", "and", "or", "not", "lparen", "rparen"]
    value: str


def _tokenize(raw: str) -> list[Token]:
    normalized = _normalize_expression(raw)
    tokens: list[Token] = []
    i = 0
    while i < len(normalized):
        i = _append_next_token(normalized, i, tokens)
    return tokens


def _normalize_expression(raw: str) -> str:
    return raw.translate(str.maketrans({"（": "(", "）": ")", "｜": "|", "－": "-"}))


def _append_next_token(text: str, i: int, tokens: list[Token]) -> int:
    ch = text[i]
    if ch.isspace():
        return i + 1
    if ch == "|":
        tokens.append(Token("or", ch))
        return i + 1
    if ch == "&":
        tokens.append(Token("and", ch))
        return i + 1
    if ch == "-":
        tokens.append(Token("not", ch))
        return i + 1
    if ch == "(":
        tokens.append(Token("lparen", ch))
        return i + 1
    if ch == ")":
        tokens.append(Token("rparen", ch))
        return i + 1
    end = _find_tag_end(text, i)
    tokens.append(Token("tag", text[i:end]))
    return end


def _find_tag_end(text: str, start: int) -> int:
    i = start
    while i < len(text) and not text[i].isspace() and text[i] not in "|&-()":
        i += 1
    return i


class _Parser:
    def __init__(self, tokens: list[Token]):
        self._tokens = tokens
        self._pos = 0

    def parse(self) -> ExprNode:
        if not self._tokens:
            raise CountExpressionError("表达式为空")
        expr = self._parse_or()
        if self._peek() is not None:
            self._raise_remaining_error()
        return expr

    def _peek(self) -> Token | None:
        if self._pos >= len(self._tokens):
            return None
        return self._tokens[self._pos]

    def _take(self) -> Token:
        token = self._tokens[self._pos]
        self._pos += 1
        return token

    def _raise_remaining_error(self):
        token = self._peek()
        if token and token.kind == "rparen":
            raise CountExpressionError("括号不匹配")
        raise CountExpressionError("表达式语法错误")

    def _parse_or(self) -> ExprNode:
        node = self._parse_and()
        while self._peek() and self._peek().kind == "or":
            self._take()
            node = BinaryNode("or", node, self._parse_and())
        return node

    def _parse_and(self) -> ExprNode:
        node = self._parse_unary()
        while self._peek() and self._peek().kind in {"and", "tag", "not", "lparen"}:
            if self._peek().kind == "and":
                self._take()
            node = BinaryNode("and", node, self._parse_unary())
        return node

    def _parse_unary(self) -> ExprNode:
        token = self._peek()
        if token and token.kind == "not":
            self._take()
            return UnaryNode("not", self._parse_unary())
        return self._parse_primary()

    def _parse_primary(self) -> ExprNode:
        token = self._peek()
        if token is None:
            raise CountExpressionError("表达式语法错误")
        if token.kind == "tag":
            return TagNode(self._take().value)
        if token.kind == "lparen":
            return self._parse_parenthesized()
        if token.kind == "rparen":
            raise CountExpressionError("括号不匹配")
        raise CountExpressionError("表达式语法错误")

    def _parse_parenthesized(self) -> ExprNode:
        self._take()
        if self._peek() is None:
            raise CountExpressionError("括号不匹配")
        node = self._parse_or()
        if not self._peek() or self._peek().kind != "rparen":
            raise CountExpressionError("括号不匹配")
        self._take()
        return node


def parse_count_expression(raw: str) -> ExprNode:
    return _Parser(_tokenize(raw)).parse()


def is_admin_event(event) -> bool:
    if _truthy_attr(event, "is_admin"):
        return True
    for holder in _admin_holders(event):
        if _holder_is_admin(holder):
            return True
    return False


def _admin_holders(event) -> list:
    return [getattr(event, "sender", None), getattr(event, "message_obj", None)]


def _holder_is_admin(holder) -> bool:
    if holder is None:
        return False
    names = ("is_admin", "is_group_admin", "is_group_owner")
    if any(_truthy_attr(holder, name) for name in names):
        return True
    return str(getattr(holder, "role", "")).lower() in {"admin", "owner", "administrator"}


def _truthy_attr(obj, name: str) -> bool:
    value = getattr(obj, name, False)
    if callable(value):
        try:
            return bool(value())
        except Exception:
            return False
    return bool(value)


def parse_count_command_arg(raw: str) -> tuple[str, str]:
    normalized = raw.replace("＝", "=")
    if "=" not in normalized:
        raise CountExpressionError("请使用：名称 = 表达式")
    name, expression = (part.strip() for part in normalized.split("=", 1))
    if not name or not expression:
        raise CountExpressionError("请使用：名称 = 表达式")
    return name, expression


def match_expression(expr: ExprNode, post: Post) -> bool:
    tags = {tag.lower() for tag in post.tags}
    return _match(expr, tags)


def _match(expr: ExprNode, tags: set[str]) -> bool:
    if isinstance(expr, TagNode):
        return expr.tag.lower() in tags
    if isinstance(expr, UnaryNode):
        return not _match(expr.child, tags)
    if expr.op == "and":
        return _match(expr.left, tags) and _match(expr.right, tags)
    return _match(expr.left, tags) or _match(expr.right, tags)


def extract_positive_tags(expr: ExprNode) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    _collect_positive_tags(expr, False, seen, result)
    return result


def _collect_positive_tags(
    node: ExprNode,
    negated: bool,
    seen: set[str],
    result: list[str],
):
    if isinstance(node, TagNode):
        _append_positive_tag(node, negated, seen, result)
        return
    if isinstance(node, UnaryNode):
        _collect_positive_tags(node.child, not negated, seen, result)
        return
    _collect_positive_tags(node.left, negated, seen, result)
    _collect_positive_tags(node.right, negated, seen, result)


def _append_positive_tag(
    node: TagNode,
    negated: bool,
    seen: set[str],
    result: list[str],
):
    key = node.tag.lower()
    if negated or key in seen:
        return
    seen.add(key)
    result.append(node.tag)


def _has_positive_anchor(node: ExprNode, negated: bool = False) -> bool:
    if isinstance(node, TagNode):
        return not negated
    if isinstance(node, UnaryNode):
        return _has_positive_anchor(node.child, not negated)
    left = _has_positive_anchor(node.left, negated)
    right = _has_positive_anchor(node.right, negated)
    is_and = (node.op == "and") != negated
    return (left or right) if is_and else (left and right)


async def count_posts(
    expression: str,
    client,
    *,
    parse_posts=parse_dwr_response,
    page_size: int = 20,
    tag_concurrency: int = 5,
) -> CountResult:
    expr = parse_count_expression(expression)
    positive_tags = extract_positive_tags(expr)
    if not positive_tags:
        raise CountExpressionError("至少需要一个正向 tag")
    if not _has_positive_anchor(expr):
        raise CountExpressionError("每个 OR 分支都需要正向 tag 约束，例如 A|-B 无法完整统计")
    if page_size < 1 or page_size > 20:
        raise ValueError("page_size 必须在 1 到 20 之间")

    results = await _scan_positive_tags(positive_tags, client, page_size, parse_posts, expr, tag_concurrency)
    seen_candidates: set[str] = set()
    matched: set[str] = set()
    scanned_pages: dict[str, int] = {}
    warnings: list[str] = []
    for item in results:
        seen_candidates.update(item.candidate_ids)
        matched.update(item.matched_ids)
        scanned_pages[item.tag] = item.scanned_pages
        warnings.extend(item.warnings)

    complete = all(item.complete for item in results)
    has_data = any(item.complete or item.scanned_pages for item in results)
    status = "扫描结束" if complete else ("部分完成" if has_data else "失败")
    return CountResult(
        name="",
        expression=expression,
        count=len(matched),
        status=status,
        error="；".join(warnings) if status == "失败" else "",
        counted_at=_now_text(),
        candidates=len(seen_candidates),
        scanned_pages=scanned_pages,
        warnings=warnings,
    )


async def _scan_positive_tags(
    tags: list[str],
    client,
    page_size: int,
    parse_posts,
    expr: ExprNode,
    tag_concurrency: int,
) -> list[TagScanResult]:
    semaphore = asyncio.Semaphore(max(1, tag_concurrency))
    tasks = [asyncio.create_task(_scan_tag_with_limit(tag, semaphore, client, page_size, parse_posts, expr)) for tag in tags]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _scan_tag_with_limit(
    tag: str,
    semaphore: asyncio.Semaphore,
    client,
    page_size: int,
    parse_posts,
    expr: ExprNode,
) -> TagScanResult:
    async with semaphore:
        return await _scan_tag_pages(tag, client, page_size, parse_posts, expr)


async def _scan_tag_pages(
    tag: str,
    client,
    page_size: int,
    parse_posts,
    expr: ExprNode,
) -> TagScanResult:
    offset = 0
    before = 0
    seen_for_tag: set[str] = set()
    candidate_ids: set[str] = set()
    matched_ids: set[str] = set()
    scanned_pages = 0
    warnings: list[str] = []
    while True:
        try:
            posts = await _fetch_page(tag, client, offset, page_size, parse_posts, before)
        except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            warnings.append(f"标签「{tag}」扫描失败：{e}")
            return TagScanResult(tag, candidate_ids, matched_ids, scanned_pages, warnings)
        if not posts:
            return TagScanResult(tag, candidate_ids, matched_ids, scanned_pages, warnings, complete=True)
        scanned_pages += 1
        if not _count_new_posts(posts, expr, seen_for_tag, candidate_ids, matched_ids):
            _append_repeat_page_warning(tag, offset, warnings)
            return TagScanResult(tag, candidate_ids, matched_ids, scanned_pages, warnings)
        offset += page_size
        timestamps = [post.publish_time_ms for post in posts if post.publish_time_ms > 0]
        if timestamps:
            before = min(timestamps)


def _append_repeat_page_warning(tag: str, offset: int, warnings: list[str]):
    if offset <= 0:
        return
    warnings.append(f"标签「{tag}」疑似分页未生效或接口返回重复页")


async def _fetch_page(tag: str, client, offset: int, page_size: int, parse_posts, before: int = 0) -> list[Post]:
    cursor = {"before": before} if before else {}
    raw = await client.search_tag(tag, offset=offset, limit=page_size, **cursor)
    return await parse_posts(raw)


def _count_new_posts(
    posts: list[Post],
    expr: ExprNode,
    seen_for_tag: set[str],
    seen_candidates: set[str],
    matched: set[str],
) -> bool:
    has_new = False
    for post in posts:
        if post.post_id in seen_for_tag:
            continue
        has_new = True
        seen_for_tag.add(post.post_id)
        if post.post_id in seen_candidates:
            continue
        seen_candidates.add(post.post_id)
        if match_expression(expr, post):
            matched.add(post.post_id)
    return has_new


def build_count_csv_path(base_dir, counted_at: str) -> Path:
    timestamp = datetime.strptime(counted_at, "%Y-%m-%d %H:%M:%S").strftime("%Y%m%d_%H%M%S")
    return Path(base_dir) / f"lofter_count_{timestamp}.csv"


def build_count_csv(rows: list[CountResult]) -> str:
    output = StringIO()
    fieldnames = ["名称", "条件", "作品数", "候选作品", "扫描页数", "状态", "错误信息", "统计时间"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(_csv_row(row))
    return output.getvalue()


def _csv_row(row: CountResult) -> dict[str, str | int]:
    return {
        "名称": row.name,
        "条件": row.expression,
        "作品数": row.count,
        "候选作品": row.candidates,
        "扫描页数": format_scanned_pages(row.scanned_pages),
        "状态": row.status,
        "错误信息": _format_error_with_warnings(row),
        "统计时间": row.counted_at,
    }


def format_scanned_pages(scanned_pages: dict[str, int]) -> str:
    return "；".join(f"{tag}:{pages}" for tag, pages in scanned_pages.items())


def _format_error_with_warnings(row: CountResult) -> str:
    parts = [row.error] if row.error else []
    parts.extend(warning for warning in row.warnings if warning not in row.error)
    return "；".join(parts)


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
