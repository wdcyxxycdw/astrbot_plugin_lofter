import csv
from io import StringIO
from unittest.mock import AsyncMock

import pytest

from core import count_commands, llm_tools
from core.count_formatters import format_count_result
from core.errors import SourceSchemaError
from core.expression_planner import (
    CountExpressionError,
    minimum_cover_alternatives,
    parse_count_expression,
)
from core.parser import Post
from core.source_scan import SourcePage
from core.tag_count import CountResult, build_count_csv, count_posts


def _covers(expression: str) -> set[frozenset[str]]:
    return set(
        minimum_cover_alternatives(
            parse_count_expression(expression)
        )
    )


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("A -B", {frozenset({"A"})}),
        ("A&B", {frozenset({"A"}), frozenset({"B"})}),
        ("A|B", {frozenset({"A", "B"})}),
        ("A|-B", set()),
        ("A|-A", set()),
        ("-(A|-B)", {frozenset({"B"})}),
    ],
)
def test_expression_planner_frozen_examples(expression, expected):
    assert _covers(expression) == expected


def test_expression_planner_antichain_minimizes_supersets():
    assert _covers("(A&B)|(A&C)") == {
        frozenset({"A"}),
        frozenset({"B", "C"}),
    }


def test_expression_planner_limits_tokens():
    expression = "|".join(f"T{i}" for i in range(65))
    with pytest.raises(CountExpressionError, match="token 数超过限制"):
        parse_count_expression(expression)


def test_expression_planner_limits_ast_depth():
    with pytest.raises(CountExpressionError, match="AST 深度超过限制"):
        parse_count_expression("-" * 33 + "A")


def _page(items, *, complete=True):
    return SourcePage(
        items=items,
        source="mobile_tag",
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=len(items),
        dropped_count=0 if complete else 1,
        complete=complete,
    )


@pytest.mark.asyncio
async def test_count_success_when_one_and_cover_completes():
    source = AsyncMock()

    async def list_tag(tag, cursor, limit, sort):
        if tag == "A":
            return _page([])
        raise SourceSchemaError("response")

    source.list_tag.side_effect = list_tag
    result = await count_posts("A&B", source)

    assert result.status == "success"
    assert result.count == 0
    assert result.candidates == 0
    assert result.scanned_pages == {"A": 0}
    assert result.warnings == []


@pytest.mark.asyncio
async def test_count_contradiction_is_exact_zero_without_scanning():
    source = AsyncMock()

    result = await count_posts("A&-A", source)

    assert result.status == "success"
    assert result.count == 0
    assert result.error == ""
    assert result.candidates == 0
    assert result.scanned_pages == {}
    source.list_tag.assert_not_awaited()


@pytest.mark.asyncio
async def test_count_partial_for_reliable_empty_or_branch():
    source = AsyncMock()

    async def list_tag(tag, cursor, limit, sort):
        if tag == "A":
            return _page([])
        raise SourceSchemaError("response")

    source.list_tag.side_effect = list_tag
    result = await count_posts("A|B", source)

    assert result.status == "partial"
    assert result.count == 0
    assert result.candidates == 0
    assert result.scanned_pages == {"A": 0, "B": 0}
    assert "至少 0 个作品" in format_count_result(result)


@pytest.mark.asyncio
async def test_count_failed_without_reliable_scan_evidence():
    source = AsyncMock()
    source.list_tag.side_effect = SourceSchemaError("response")

    result = await count_posts("A|B", source)
    text = format_count_result(result)

    assert result.status == "failed"
    assert result.count == 0
    assert "统计失败" in text
    assert "候选作品" not in text
    assert "扫描页数" not in text


@pytest.mark.asyncio
async def test_non_enumerable_expression_returns_partial_lower_bound():
    source = AsyncMock()
    source.list_tag.return_value = _page([
        Post(
            post_id="p1", title="", summary="", tags=["A"],
            publish_time="2026-01-02 00:00:00",
        ),
        Post(
            post_id="p2", title="", summary="", tags=["A", "B"],
            publish_time="2026-01-01 00:00:00",
        ),
    ])

    result = await count_posts("A|-B", source)

    assert result.status == "partial"
    assert result.count == 2
    assert result.candidates == 2
    assert result.scanned_pages == {"A": 1}


@pytest.mark.asyncio
async def test_count_enriches_unknown_tags_before_matching():
    source = AsyncMock()
    partial = Post(
        post_id="a_b",
        title="",
        summary="",
        url="https://demo.lofter.com/post/a_b",
        source="mobile_tag",
        completeness=frozenset({"url"}),
    )
    detail = Post(
        post_id="a_b",
        title="",
        summary="",
        url="https://demo.lofter.com/post/a_b",
        tags=["A"],
        source="mobile_detail",
    )
    source.list_tag.return_value = _page([partial])
    source.get_post.return_value = detail

    result = await count_posts("A", source)

    assert result.status == "success"
    assert result.count == 1
    source.get_post.assert_awaited_once_with(partial.url)


@pytest.mark.asyncio
async def test_count_unknown_tags_without_detail_is_partial():
    source = AsyncMock()
    partial = Post(
        post_id="a_b",
        title="",
        summary="",
        url="https://demo.lofter.com/post/a_b",
        source="mobile_tag",
        completeness=frozenset({"url"}),
    )
    source.list_tag.return_value = _page([partial])
    source.get_post.side_effect = SourceSchemaError("tags")

    result = await count_posts("A", source)

    assert result.status == "partial"
    assert result.count == 0
    assert result.candidates == 1
    assert any("标签字段未知" in item for item in result.warnings)


def test_cli_llm_and_csv_share_partial_result_semantics():
    result = CountResult(
        name="条件",
        expression="A|B",
        count=2,
        status="partial",
        error="没有完整 cover 扫描完成",
        counted_at="2026-07-29 05:00:00",
        candidates=4,
        scanned_pages={"A": 1, "B": 0},
    )

    expected = format_count_result(result)
    assert count_commands._format_count_result(result) == expected
    assert llm_tools._format_count_result(result) == expected
    row = next(csv.DictReader(StringIO(build_count_csv([result]))))
    assert row["作品数"] == "至少 2"
    assert row["候选作品"] == "4"
    assert row["扫描页数"] == "A:1；B:0"
    assert row["状态"] == "部分"


def test_csv_failed_result_does_not_report_successful_zero():
    result = CountResult(
        name="条件",
        expression="-A",
        count=0,
        status="failed",
        error="没有获得可靠扫描证据",
        counted_at="2026-07-29 05:00:00",
    )

    row = next(csv.DictReader(StringIO(build_count_csv([result]))))
    assert row["作品数"] == ""
    assert row["候选作品"] == ""
    assert row["扫描页数"] == ""
    assert row["状态"] == "失败"
