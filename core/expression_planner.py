from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MAX_EXPRESSION_TOKENS = 128
MAX_EXPRESSION_DEPTH = 32
MAX_COVER_ALTERNATIVES = 256
MAX_PLANNER_STEPS = 100_000


class CountExpressionError(ValueError):
    pass


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
Cover = frozenset[str]


@dataclass(frozen=True)
class Token:
    kind: Literal["tag", "and", "or", "not", "lparen", "rparen"]
    value: str


def parse_count_expression(raw: str) -> ExprNode:
    tokens = _tokenize(raw)
    return _Parser(tokens).parse()


def to_nnf(expr: ExprNode) -> ExprNode:
    result = _to_nnf(expr, False)
    _check_depth(result)
    return result


def minimum_cover_alternatives(expr: ExprNode) -> tuple[Cover, ...]:
    normalized = to_nnf(expr)
    names = _tag_names(normalized)
    budget = _PlannerBudget(MAX_PLANNER_STEPS)
    manager = _BDDManager(tuple(names), budget)
    root = manager.build(normalized)
    covers = _CoverSolver(manager, budget).solve(root)
    return tuple(_display_covers(list(covers), names))


def _tokenize(raw: str) -> list[Token]:
    if not isinstance(raw, str):
        raise CountExpressionError("表达式必须是字符串")
    text = raw.translate(str.maketrans({"（": "(", "）": ")", "｜": "|", "－": "-"}))
    tokens: list[Token] = []
    index = 0
    while index < len(text):
        index = _append_token(text, index, tokens)
        if len(tokens) > MAX_EXPRESSION_TOKENS:
            raise CountExpressionError("表达式 token 数超过限制")
    return tokens


def _append_token(text: str, index: int, tokens: list[Token]) -> int:
    char = text[index]
    kinds = {"|": "or", "&": "and", "-": "not", "(": "lparen", ")": "rparen"}
    if char.isspace():
        return index + 1
    if char in kinds:
        tokens.append(Token(kinds[char], char))
        return index + 1
    end = index
    while end < len(text) and not text[end].isspace() and text[end] not in kinds:
        end += 1
    tokens.append(Token("tag", text[index:end]))
    return end


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
        _check_depth(expr)
        return expr

    def _peek(self) -> Token | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _take(self) -> Token:
        token = self._tokens[self._pos]
        self._pos += 1
        return token

    def _parse_or(self) -> ExprNode:
        node = self._parse_and()
        while self._peek() and self._peek().kind == "or":
            self._take()
            node = BinaryNode("or", node, self._parse_and())
        return node

    def _parse_and(self) -> ExprNode:
        node = self._parse_unary()
        starts = {"and", "tag", "not", "lparen"}
        while self._peek() and self._peek().kind in starts:
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

    def _raise_remaining_error(self) -> None:
        if self._peek() and self._peek().kind == "rparen":
            raise CountExpressionError("括号不匹配")
        raise CountExpressionError("表达式语法错误")


def _check_depth(expr: ExprNode) -> None:
    if _depth(expr) > MAX_EXPRESSION_DEPTH:
        raise CountExpressionError("表达式 AST 深度超过限制")


def _depth(expr: ExprNode) -> int:
    if isinstance(expr, TagNode):
        return 1
    if isinstance(expr, UnaryNode):
        return 1 + _depth(expr.child)
    return 1 + max(_depth(expr.left), _depth(expr.right))


def _to_nnf(expr: ExprNode, negated: bool) -> ExprNode:
    if isinstance(expr, TagNode):
        return UnaryNode("not", expr) if negated else expr
    if isinstance(expr, UnaryNode):
        return _to_nnf(expr.child, not negated)
    op = _negated_op(expr.op) if negated else expr.op
    return BinaryNode(op, _to_nnf(expr.left, negated), _to_nnf(expr.right, negated))


def _negated_op(op: Literal["and", "or"]) -> Literal["and", "or"]:
    return "or" if op == "and" else "and"


def _tag_names(expr: ExprNode) -> dict[str, str]:
    names: dict[str, str] = {}

    def visit(node: ExprNode) -> None:
        if isinstance(node, TagNode):
            names.setdefault(node.tag.casefold(), node.tag)
            return
        if isinstance(node, UnaryNode):
            visit(node.child)
            return
        visit(node.left)
        visit(node.right)

    visit(expr)
    return names


@dataclass(frozen=True)
class _BDDNode:
    variable: int
    low: int
    high: int


class _PlannerBudget:
    def __init__(self, remaining: int):
        self.remaining = remaining

    def consume(self, amount: int = 1) -> None:
        self.remaining -= amount
        if self.remaining < 0:
            raise CountExpressionError("表达式 planner 计算量超过限制")


class _BDDManager:
    def __init__(self, variables: tuple[str, ...], budget: _PlannerBudget):
        self.variables = variables
        self._indices = {name: index for index, name in enumerate(variables)}
        self._budget = budget
        self._nodes: list[_BDDNode | None] = [None, None]
        self._unique: dict[tuple[int, int, int], int] = {}
        self._build_cache: dict[ExprNode, int] = {}
        self._apply_cache: dict[tuple[str, int, int], int] = {}
        self._negate_cache = {0: 1, 1: 0}

    def build(self, expr: ExprNode) -> int:
        self._budget.consume()
        if expr in self._build_cache:
            return self._build_cache[expr]
        if isinstance(expr, TagNode):
            result = self._make(self._indices[expr.tag.casefold()], 0, 1)
        elif isinstance(expr, UnaryNode):
            result = self._negate(self.build(expr.child))
        else:
            result = self._apply(expr.op, self.build(expr.left), self.build(expr.right))
        self._build_cache[expr] = result
        return result

    def node(self, node_id: int) -> _BDDNode:
        node = self._nodes[node_id]
        if node is None:
            raise CountExpressionError("表达式 planner 节点无效")
        return node

    def _make(self, variable: int, low: int, high: int) -> int:
        if low == high:
            return low
        self._budget.consume()
        key = (variable, low, high)
        existing = self._unique.get(key)
        if existing is not None:
            return existing
        self._nodes.append(_BDDNode(variable, low, high))
        node_id = len(self._nodes) - 1
        self._unique[key] = node_id
        return node_id

    def _negate(self, node_id: int) -> int:
        self._budget.consume()
        cached = self._negate_cache.get(node_id)
        if cached is not None:
            return cached
        node = self.node(node_id)
        result = self._make(
            node.variable, self._negate(node.low), self._negate(node.high)
        )
        self._negate_cache[node_id] = result
        return result

    def _apply(self, op: str, left: int, right: int) -> int:
        self._budget.consume()
        if left > right:
            left, right = right, left
        simplified = self._simplify(op, left, right)
        if simplified is not None:
            return simplified
        key = (op, left, right)
        if key in self._apply_cache:
            return self._apply_cache[key]
        variable = min(self.node(left).variable, self.node(right).variable)
        low = self._apply(op, self._branch(left, variable, False),
                          self._branch(right, variable, False))
        high = self._apply(op, self._branch(left, variable, True),
                           self._branch(right, variable, True))
        result = self._make(variable, low, high)
        self._apply_cache[key] = result
        return result

    @staticmethod
    def _simplify(op: str, left: int, right: int) -> int | None:
        if left == right:
            return left
        if op == "and":
            if left == 0 or right == 0:
                return 0
            if left == 1:
                return right
            if right == 1:
                return left
        if op == "or":
            if left == 1 or right == 1:
                return 1
            if left == 0:
                return right
            if right == 0:
                return left
        return None

    def _branch(self, node_id: int, variable: int, high: bool) -> int:
        if node_id < 2:
            return node_id
        node = self.node(node_id)
        if node.variable != variable:
            return node_id
        return node.high if high else node.low


class _CoverSolver:
    def __init__(self, manager: _BDDManager, budget: _PlannerBudget):
        self._manager = manager
        self._budget = budget
        self._cache: dict[int, tuple[Cover, ...]] = {}

    def solve(self, node_id: int) -> tuple[Cover, ...]:
        self._budget.consume()
        if node_id in self._cache:
            return self._cache[node_id]
        if node_id == 0:
            return (frozenset(),)
        if node_id == 1:
            return ()
        node = self._manager.node(node_id)
        if node.low == 1:
            result = ()
        elif node.high == 0:
            result = self.solve(node.low)
        elif node.high == 1:
            result = self._add_variable(node.variable, self.solve(node.low))
        else:
            low = self.solve(node.low)
            high = self.solve(node.high)
            forced = self._add_variable(node.variable, low)
            free = self._combine(low, high)
            result = self._minimal(forced + free)
        self._cache[node_id] = result
        return result

    def _add_variable(
        self, variable: int, covers: tuple[Cover, ...]
    ) -> tuple[Cover, ...]:
        self._budget.consume(len(covers))
        name = self._manager.variables[variable]
        return tuple(cover | {name} for cover in covers)

    def _combine(
        self, left: tuple[Cover, ...], right: tuple[Cover, ...]
    ) -> tuple[Cover, ...]:
        self._budget.consume(len(left) * len(right))
        return tuple(first | second for first in left for second in right)

    def _minimal(self, candidates: tuple[Cover, ...]) -> tuple[Cover, ...]:
        self._budget.consume(len(candidates))
        ordered = sorted(set(candidates), key=_cover_sort_key)
        result: list[Cover] = []
        for candidate in ordered:
            if self._is_dominated(candidate, result):
                continue
            result.append(candidate)
            if len(result) > MAX_COVER_ALTERNATIVES:
                raise CountExpressionError("表达式 cover alternatives 超过限制")
        return tuple(result)

    def _is_dominated(self, candidate: Cover, covers: list[Cover]) -> bool:
        for existing in covers:
            self._budget.consume()
            if existing <= candidate:
                return True
        return False


def _cover_sort_key(cover: Cover) -> tuple[int, tuple[str, ...]]:
    return len(cover), tuple(sorted(cover))


def _display_covers(
    covers: list[Cover], names: dict[str, str]
) -> list[Cover]:
    result = [frozenset(names[tag] for tag in cover) for cover in covers]
    return sorted(result, key=lambda item: (len(item), tuple(sorted(item))))
