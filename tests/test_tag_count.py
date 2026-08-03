import asyncio
import csv
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.errors import SourceSchemaError, SourceTimeoutError
from core.parser import Post
from core.source_scan import SourcePage
from core.tag_count import (
    CountExpressionError,
    CountResult,
    build_count_csv,
    build_count_csv_path,
    count_posts,
    extract_positive_tags,
    match_expression,
    parse_count_command_arg,
    parse_count_expression,
)
from core.count_commands import LofterCountCommandsMixin, _file_constructor_candidates, _try_direct_file


def _post(tags: list[str]) -> Post:
    return Post(post_id="p", title="", summary="", tags=tags)


def _timed_post(post_id: str, tags: list[str], time: str) -> Post:
    return Post(
        post_id=post_id,
        title="",
        summary="",
        tags=tags,
        publish_time=time,
    )


def _page(items, cursor=None, *, exhausted=None, restarted=False, complete=True):
    if exhausted is None:
        exhausted = cursor is None
    return SourcePage(
        items=items,
        source="mobile_tag",
        next_cursor=cursor,
        exhausted=exhausted,
        sort="new",
        mapped_count=len(items),
        dropped_count=0 if complete else 1,
        complete=complete,
        restarted=restarted,
    )


def test_build_count_csv_path(tmp_path):
    path = build_count_csv_path(tmp_path, "2026-05-12 17:30:00")
    assert path == tmp_path / "lofter_count_20260512_173000.csv"


def test_parse_space_as_and():
    expr = parse_count_expression("原神 同人")
    assert match_expression(expr, _post(["原神", "同人"])) is True
    assert match_expression(expr, _post(["原神"])) is False


@pytest.mark.parametrize("expression", ["A&B", "A & B", "A　&　B"])
def test_parse_ampersand_as_explicit_and(expression):
    expr = parse_count_expression(expression)
    assert match_expression(expr, _post(["A", "B"])) is True
    assert match_expression(expr, _post(["A"])) is False


def test_parse_ampersand_before_not():
    expr = parse_count_expression("A&-R18")
    assert match_expression(expr, _post(["A"])) is True
    assert match_expression(expr, _post(["A", "R18"])) is False


def test_parse_ampersand_before_parentheses():
    expr = parse_count_expression("A&(B|C)")
    assert match_expression(expr, _post(["A", "B"])) is True
    assert match_expression(expr, _post(["A", "C"])) is True
    assert match_expression(expr, _post(["B"])) is False


def test_and_keyword_is_treated_as_real_tag():
    expr = parse_count_expression("A AND B")
    assert match_expression(expr, _post(["A", "B"])) is False
    assert match_expression(expr, _post(["A", "AND", "B"])) is True


def test_lowercase_and_keyword_is_treated_as_real_tag():
    expr = parse_count_expression("A and B")
    assert match_expression(expr, _post(["A", "B"])) is False
    assert match_expression(expr, _post(["A", "and", "B"])) is True


def test_parse_full_width_operators():
    expr = parse_count_expression("原神｜崩铁 －R18")
    assert match_expression(expr, _post(["原神"])) is True
    assert match_expression(expr, _post(["崩铁"])) is True
    assert match_expression(expr, _post(["崩铁", "R18"])) is False


def test_parse_count_command_arg_accepts_full_width_equals():
    name, expr = parse_count_command_arg("米哈游相关＝原神｜崩铁")
    assert name == "米哈游相关"
    assert expr == "原神｜崩铁"


def test_parse_pipe_as_or():
    expr = parse_count_expression("原神|崩铁")
    assert match_expression(expr, _post(["原神"])) is True
    assert match_expression(expr, _post(["崩铁"])) is True
    assert match_expression(expr, _post(["明日方舟"])) is False


def test_parse_not_and_parentheses():
    expr = parse_count_expression("原神 (崩铁 -R18)")
    assert match_expression(expr, _post(["原神", "崩铁"])) is True
    assert match_expression(expr, _post(["原神", "崩铁", "R18"])) is False
    assert match_expression(expr, _post(["原神"])) is False


def test_parse_full_width_parentheses():
    expr = parse_count_expression("原神 （崩铁 -R18）")
    assert match_expression(expr, _post(["原神", "崩铁"])) is True


def test_match_is_case_insensitive():
    expr = parse_count_expression("nsfw")
    assert match_expression(expr, _post(["NSFW"])) is True


def test_extract_positive_tags_excludes_not_terms():
    expr = parse_count_expression("原神|崩铁 -R18")
    assert extract_positive_tags(expr) == ["原神", "崩铁"]


def test_reject_empty_expression():
    with pytest.raises(CountExpressionError, match="表达式为空"):
        parse_count_expression("   ")


def test_reject_unbalanced_parenthesis():
    with pytest.raises(CountExpressionError, match="括号不匹配"):
        parse_count_expression("原神 (")


def test_reject_only_not_has_no_positive_tags():
    expr = parse_count_expression("-R18")
    assert extract_positive_tags(expr) == []


def test_and_has_higher_precedence_than_or():
    """Test that AND binds tighter than OR: A|B C means A OR (B AND C)"""
    expr = parse_count_expression("A|B C")
    # Should match A alone
    assert match_expression(expr, _post(["A"])) is True
    # Should match B AND C together
    assert match_expression(expr, _post(["B", "C"])) is True
    # Should NOT match B alone (would need A OR (B AND C))
    assert match_expression(expr, _post(["B"])) is False
    # Should NOT match C alone
    assert match_expression(expr, _post(["C"])) is False
    # Should NOT match if only B or only C, even if another unrelated tag exists
    assert match_expression(expr, _post(["B", "D"])) is False
    assert match_expression(expr, _post(["C", "D"])) is False


def test_extract_positive_tags_preserves_order_and_deduplicates():
    """Test that extract_positive_tags preserves first appearance order and deduplicates by case-insensitive key"""
    # Case 1: Simple deduplication with case-insensitive keys
    expr = parse_count_expression("原神 崩铁 原神 NSFW nsfw")
    tags = extract_positive_tags(expr)
    assert tags == ["原神", "崩铁", "NSFW"]
    # First "原神" kept, second ignored
    # First "NSFW" kept, "nsfw" ignored (case-insensitive duplicate)


def test_extract_positive_tags_excludes_negated_tags():
    """Test that extract_positive_tags excludes tags under NOT operators"""
    expr = parse_count_expression("原神 -R18 崩铁 -nsfw")
    tags = extract_positive_tags(expr)
    assert tags == ["原神", "崩铁"]
    # R18 and nsfw are negated, so excluded


def test_extract_positive_tags_mixed_complex():
    """Test extract_positive_tags with complex expression including duplicates and negations"""
    expr = parse_count_expression("A B|A -C D -D")
    tags = extract_positive_tags(expr)
    # A appears twice (first preserved), B appears once, C is negated, D appears twice (first preserved, second is negated)
    assert tags == ["A", "B", "D"]


def test_parentheses_override_precedence():
    """Test that parentheses override default AND-higher-than-OR precedence: (A|B) C means (A OR B) AND C"""
    expr = parse_count_expression("(A|B) C")
    # Should match A AND C together
    assert match_expression(expr, _post(["A", "C"])) is True
    # Should match B AND C together
    assert match_expression(expr, _post(["B", "C"])) is True
    # Should NOT match A alone (needs C)
    assert match_expression(expr, _post(["A"])) is False
    # Should NOT match B alone (needs C)
    assert match_expression(expr, _post(["B"])) is False
    # Different from default precedence: A|B C means A OR (B AND C), which matches A alone
    # But (A|B) C means (A OR B) AND C, which requires C to be present with either A or B


def test_parse_count_command_arg():
    name, expr = parse_count_command_arg("米哈游相关 = 原神 (崩铁 -R18)")
    assert name == "米哈游相关"
    assert expr == "原神 (崩铁 -R18)"


def test_parse_count_command_arg_rejects_missing_equals():
    with pytest.raises(CountExpressionError, match="名称 = 表达式"):
        parse_count_command_arg("米哈游相关 原神")


@pytest.mark.asyncio
async def test_count_posts_dedupes_candidates_and_matches_expression():
    source = AsyncMock()
    pages = {
        ("A", None): _page([
            _timed_post("1", ["A", "B"], "2026-01-10 00:00:00"),
            _timed_post("2", ["A"], "2026-01-09 00:00:00"),
        ], "a-2"),
        ("A", "a-2"): _page([]),
        ("B", None): _page([
            _timed_post("1", ["A", "B"], "2026-01-10 00:00:00"),
            _timed_post("3", ["B"], "2026-01-09 00:00:00"),
        ], "b-2"),
        ("B", "b-2"): _page([]),
    }

    async def list_tag(tag, cursor, limit, sort):
        assert (limit, sort) == (20, "new")
        return pages[(tag, cursor)]

    source.list_tag.side_effect = list_tag
    result = await count_posts("A B", source, page_size=20)

    assert result.status == "success"
    assert result.count == 1
    assert result.candidates == 2
    assert result.scanned_pages == {"A": 1}
    assert {call.args[0] for call in source.list_tag.call_args_list} == {
        "A",
        "B",
    }


@pytest.mark.asyncio
async def test_count_posts_continues_tag_pages_when_page_is_only_global_duplicates():
    source = AsyncMock()
    pages = {
        ("A", None): _page([
            _timed_post("p1", ["A", "B"], "2026-01-10 00:00:00"),
            _timed_post("p2", ["A", "B"], "2026-01-09 00:00:00"),
        ], "a-2"),
        ("A", "a-2"): _page([]),
        ("B", None): _page([
            _timed_post("p1", ["A", "B"], "2026-01-10 00:00:00"),
            _timed_post("p2", ["A", "B"], "2026-01-09 00:00:00"),
        ], "b-2"),
        ("B", "b-2"): _page([
            _timed_post("p3", ["B"], "2026-01-08 00:00:00")
        ], "b-4"),
        ("B", "b-4"): _page([]),
    }
    source.list_tag.side_effect = (
        lambda tag, cursor, limit, sort: pages[(tag, cursor)]
    )

    result = await count_posts("A|B", source, page_size=2)

    assert result.count == 3
    cursors_by_tag = {"A": [], "B": []}
    for item in source.list_tag.call_args_list:
        cursors_by_tag[item.args[0]].append(item.args[1])
    assert cursors_by_tag == {
        "A": [None, "a-2"],
        "B": [None, "b-2", "b-4"],
    }


@pytest.mark.asyncio
async def test_count_posts_scans_multiple_positive_tags_concurrently():
    source = AsyncMock()
    active_tags: set[str] = set()
    overlaps: list[set[str]] = []

    async def list_tag(tag, cursor, limit, sort):
        active_tags.add(tag)
        if len(active_tags) > 1:
            overlaps.append(set(active_tags))
        await asyncio.sleep(0.01)
        active_tags.remove(tag)
        return _page([])

    source.list_tag.side_effect = list_tag
    result = await count_posts("A|B", source, page_size=20)

    assert result.count == 0
    assert {"A", "B"} in overlaps


@pytest.mark.asyncio
async def test_count_posts_uses_one_deadline_and_cancels_all_tag_tasks():
    source = AsyncMock()
    started: set[str] = set()
    stopped: set[str] = set()

    async def list_tag(tag, cursor, limit, sort):
        started.add(tag)
        try:
            await asyncio.Event().wait()
        finally:
            stopped.add(tag)

    source.list_tag.side_effect = list_tag
    result = await count_posts(
        "A|B", source, tag_concurrency=2, _deadline=0.05
    )
    assert result.status == "failed"
    assert result.count == 0
    assert result.candidates == 0
    assert result.scanned_pages == {"A": 0, "B": 0}
    assert started == {"A", "B"}
    assert stopped == {"A", "B"}


@pytest.mark.asyncio
async def test_count_posts_does_not_return_success_after_absolute_deadline():
    source = AsyncMock()
    source.list_tag.return_value = _page([])
    clock = iter([0.0, 0.0, 2.0])
    result = await count_posts(
        "A", source, _deadline=1.0, _monotonic=lambda: next(clock)
    )
    assert result.status == "partial"
    assert result.count == 0


@pytest.mark.asyncio
async def test_count_posts_propagates_source_timeout_without_success():
    source = AsyncMock()
    source.list_tag.side_effect = SourceTimeoutError()

    result = await count_posts("A", source)

    assert result.status == "failed"
    assert result.count == 0
    assert result.candidates == 0


@pytest.mark.asyncio
async def test_count_posts_preserves_scan_completed_before_deadline():
    source = AsyncMock()
    source.list_tag.return_value = _page([])
    clock = iter([0.0, 0.0, 0.5, 2.0])
    result = await count_posts(
        "A", source, _deadline=1.0, _monotonic=lambda: next(clock)
    )
    assert result.status == "success"
    assert result.count == 0
    assert result.warnings == []


@pytest.mark.asyncio
async def test_count_posts_reports_failed_without_positive_scan_evidence():
    source = AsyncMock()
    result = await count_posts("-R18", source)

    assert result.status == "failed"
    assert result.count == 0
    assert result.candidates == 0
    assert result.scanned_pages == {}
    source.list_tag.assert_not_awaited()


@pytest.mark.asyncio
async def test_count_posts_records_warning_when_one_tag_scan_fails():
    source = AsyncMock()

    async def list_tag(tag, cursor, limit, sort):
        if tag == "A" and cursor is None:
            return _page([
                _timed_post("p1", ["A"], "2026-01-10 00:00:00")
            ], "a-20")
        if tag == "A":
            return _page([])
        raise SourceSchemaError("response")

    source.list_tag.side_effect = list_tag
    result = await count_posts("A|B", source)

    assert result.count == 1
    assert result.candidates == 1
    assert result.scanned_pages == {"A": 1, "B": 0}
    assert result.warnings == [
        "标签「B」扫描失败：内容源响应结构无效（response）"
    ]


@pytest.mark.asyncio
async def test_count_posts_propagates_unexpected_scan_errors():
    source = AsyncMock()
    source.list_tag.side_effect = ValueError("source bug")

    with pytest.raises(ValueError, match="source bug"):
        await count_posts("A", source)


@pytest.mark.asyncio
async def test_count_posts_reports_scanned_pages():
    source = AsyncMock()
    source.list_tag.side_effect = [
        _page([
            _timed_post("p1", ["A"], "2026-01-10 00:00:00")
        ], "a-1"),
        _page([
            _timed_post("p2", ["A"], "2026-01-09 00:00:00")
        ], "a-2"),
        _page([]),
    ]

    result = await count_posts("A", source, page_size=1)

    assert result.scanned_pages == {"A": 2}
    assert result.warnings == []


@pytest.mark.asyncio
async def test_count_posts_warns_when_positive_cursor_page_repeats_tag_posts():
    source = AsyncMock()
    post = _timed_post("p1", ["A"], "2026-01-10 00:00:00")
    source.list_tag.side_effect = [_page([post], "a-1"), _page([post], "a-2")]

    result = await count_posts("A", source, page_size=1)

    assert result.scanned_pages == {"A": 2}
    assert result.warnings == ["标签「A」疑似分页未生效或接口返回重复页"]


def test_build_count_csv():
    rows = [
        CountResult(
            name="米哈游相关",
            expression="原神 -R18",
            count=12,
            status="成功",
            error="",
            counted_at="2026-05-12 17:30:00",
            scanned_pages={"原神": 3},
            warnings=["标签「原神」疑似分页未生效或接口返回重复页"],
        ),
        CountResult(name="异常", expression="原神 (", count=0, status="失败", error="括号不匹配", counted_at="2026-05-12 17:30:00"),
    ]
    content = build_count_csv(rows)
    parsed = list(csv.DictReader(StringIO(content)))
    assert parsed[0]["名称"] == "米哈游相关"
    assert parsed[0]["作品数"] == "12"
    assert parsed[0]["扫描页数"] == "原神:3"
    assert parsed[0]["错误信息"] == "标签「原神」疑似分页未生效或接口返回重复页"
    assert parsed[1]["状态"] == "失败"


def test_file_constructor_candidates_official_first(tmp_path):
    """验证官方标准构造（name, file=path）是第一个候选"""
    path = tmp_path / "test_file.csv"
    candidates = _file_constructor_candidates(path)

    # 第一个候选必须是官方标准构造
    assert len(candidates) >= 1
    args, kwargs = candidates[0]
    assert args == (path.name,), f"第一个候选的 args 应该是 ('{path.name}',)，实际 {args}"
    assert kwargs == {"file": str(path)}, f"第一个候选的 kwargs 应该是 {{'file': '{str(path)}'}}, 实际 {kwargs}"


def test_try_direct_file_with_official_constructor(tmp_path):
    """验证 _try_direct_file 使用官方构造器（name, file=path）成功构造 File 对象"""

    class FakeFile:
        """模拟官方 astrbot File 类"""
        def __init__(self, name: str, file: str = "", url: str = ""):
            self.name = name
            self.file = file
            self.url = url

    path = tmp_path / "report.csv"
    path.write_text("test content")

    result = _try_direct_file(FakeFile, path)

    assert result is not None, "_try_direct_file 应该成功构造 FakeFile 对象"
    assert result.name == path.name, f"name 应该是文件名 '{path.name}'，实际 '{result.name}'"
    assert result.file == str(path), f"file 应该是完整路径 '{str(path)}'，实际 '{result.file}'"
    assert result.url == "", f"url 应该是空字符串，实际 '{result.url}'"


@pytest.mark.asyncio
async def test_handle_count_all_falls_back_when_csv_send_raises(tmp_path):
    class FakeDB:
        def __init__(self, path: Path):
            self._path = path

        async def list_count_conditions(self):
            return [("全部", "A")]

    class FakeEvent:
        def is_admin(self):
            return True

        def plain_result(self, text: str):
            return text

    class Runner(LofterCountCommandsMixin):
        def __init__(self, db):
            self._db = db
            self._client = None

        async def _count_condition(self, name: str, expression: str, counted_at: str):
            return CountResult(name, expression, 1, "成功", "", counted_at)

        def _send_count_csv(self, event, path: Path):
            raise RuntimeError("adapter failed")

    db_path = tmp_path / "lofter.db"
    runner = Runner(FakeDB(db_path))

    results = [item async for item in runner.handle_count_all(FakeEvent())]

    assert len(results) == 1
    assert results[0].startswith("CSV 已生成，但发送失败：")
    assert str(tmp_path) in results[0]
