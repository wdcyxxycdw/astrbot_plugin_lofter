import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]
PRODUCTION_FILES = [ROOT / "main.py", *(ROOT / "core").glob("*.py")]
LOW_LEVEL_FILES = {
    "core/client.py",
    "core/content_source.py",
    "core/dwr_parser.py",
    "core/mobile_adapter.py",
    "core/parser.py",
}
FORBIDDEN_SYMBOLS = {
    "LofterClient",
    "parse_blog_posts",
    "parse_dwr_response",
    "parse_dwr_response_result",
    "parse_post_page",
}
FORBIDDEN_METHODS = {
    "request_json",
    "request_text",
    "search_tag",
    "search_tag_paged",
}


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _forbidden_imports(relative: str, node: ast.AST) -> list[str]:
    if not isinstance(node, ast.ImportFrom):
        return []
    return [
        f"{relative}:{node.lineno} import {alias.name}"
        for alias in node.names
        if alias.name in FORBIDDEN_SYMBOLS
    ]


def _violations(path: Path) -> list[str]:
    relative = _relative(path)
    if relative in LOW_LEVEL_FILES:
        return []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    result: list[str] = []
    for node in ast.walk(tree):
        result.extend(_forbidden_imports(relative, node))
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_SYMBOLS:
            result.append(f"{relative}:{node.lineno} call {node.func.id}")
        if isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN_METHODS:
            result.append(f"{relative}:{node.lineno} call .{node.func.attr}")
    return result


def test_business_paths_use_content_source_boundary():
    violations = [
        violation
        for path in PRODUCTION_FILES
        for violation in _violations(path)
    ]
    assert violations == []
