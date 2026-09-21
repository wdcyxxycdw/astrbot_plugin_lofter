import asyncio
import csv
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.parser import Post
from core.tag_count import (
    CountExpressionError,
    CountResult,
    build_count_csv,
    build_count_csv_path,
    count_posts,
    extract_positive_tags,
    is_admin_event,
    match_expression,
    parse_count_command_arg,
    parse_count_expression,
)
from core.count_commands import LofterCountCommandsMixin, _file_constructor_candidates, _try_direct_file


def _post(tags: list[str]) -> Post:
    return Post(post_id="p", title="", summary="", tags=tags)


class DummyEvent:
    def __init__(self, admin):
        self.is_admin = admin


def test_is_admin_event_reads_boolean_attribute():
    assert is_admin_event(DummyEvent(True)) is True
    assert is_admin_event(DummyEvent(False)) is False


class CallableAdminEvent:
    def __init__(self, admin):
        self._admin = admin

    def is_admin(self):
        return self._admin


def test_is_admin_event_reads_callable_attribute():
    assert is_admin_event(CallableAdminEvent(True)) is True
    assert is_admin_event(CallableAdminEvent(False)) is False


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
    client = AsyncMock()
    client.search_tag.side_effect = ["raw-a-1", "", "raw-b-1", ""]

    async def parser(raw):
        if raw == "raw-a-1":
            return [
                Post(post_id="1", title="", summary="", tags=["A", "B"]),
                Post(post_id="2", title="", summary="", tags=["A"]),
            ]
        if raw == "raw-b-1":
            return [
                Post(post_id="1", title="", summary="", tags=["A", "B"]),
                Post(post_id="3", title="", summary="", tags=["B"]),
            ]
        return []

    result = await count_posts("A B", client, parse_posts=parser, page_size=20)

    assert result.count == 1
    assert result.candidates == 3
    assert client.search_tag.call_args_list[0].kwargs == {"offset": 0, "limit": 20}


@pytest.mark.asyncio
async def test_count_posts_continues_tag_pages_when_page_is_only_global_duplicates():
    client = AsyncMock()

    async def search_tag(tag, *, offset, limit):
        pages = {
            ("A", 0): "raw-a-1",
            ("A", 2): "",
            ("B", 0): "raw-b-1",
            ("B", 2): "raw-b-2",
            ("B", 4): "",
        }
        return pages[(tag, offset)]

    client.search_tag.side_effect = search_tag

    async def parser(raw):
        if raw == "raw-a-1":
            return [
                Post(post_id="p1", title="", summary="", tags=["A"]),
                Post(post_id="p2", title="", summary="", tags=["A"]),
            ]
        if raw == "raw-b-1":
            return [
                Post(post_id="p1", title="", summary="", tags=["B"]),
                Post(post_id="p2", title="", summary="", tags=["B"]),
            ]
        if raw == "raw-b-2":
            return [Post(post_id="p3", title="", summary="", tags=["B"])]
        return []

    result = await count_posts("A|B", client, parse_posts=parser, page_size=2)

    assert result.count == 3
    offsets_by_tag = {"A": [], "B": []}
    for item in client.search_tag.call_args_list:
        offsets_by_tag[item.args[0]].append(item.kwargs["offset"])
    assert offsets_by_tag == {"A": [0, 2], "B": [0, 2, 4]}


@pytest.mark.asyncio
async def test_count_posts_scans_multiple_positive_tags_concurrently():
    client = AsyncMock()
    active_tags: set[str] = set()
    overlaps: list[set[str]] = []

    async def search_tag(tag, *, offset, limit):
        active_tags.add(tag)
        if len(active_tags) > 1:
            overlaps.append(set(active_tags))
        await asyncio.sleep(0.01)
        active_tags.remove(tag)
        return ""

    client.search_tag.side_effect = search_tag

    async def parser(raw):
        return []

    result = await count_posts("A|B", client, page_size=20, parse_posts=parser)

    assert result.count == 0
    assert {"A", "B"} in overlaps


@pytest.mark.asyncio
async def test_count_posts_rejects_expression_without_positive_tag():
    client = AsyncMock()
    with pytest.raises(CountExpressionError, match="至少需要一个正向 tag"):
        await count_posts("-R18", client)


@pytest.mark.asyncio
async def test_count_posts_records_warning_when_one_tag_scan_fails():
    client = AsyncMock()

    async def search_tag(tag, *, offset, limit):
        return f"raw-{tag}-{offset}"

    client.search_tag.side_effect = search_tag

    async def parser(raw):
        if raw == "raw-A-0":
            return [Post(post_id="p1", title="", summary="", tags=["A"])]
        if raw == "raw-A-20":
            return []
        raise RuntimeError("LOFTER 返回非 DWR 响应：响应片段：{ status: 4009 }")

    result = await count_posts("A|B", client, parse_posts=parser)

    assert result.count == 1
    assert result.status == "部分完成"
    assert result.candidates == 1
    assert result.scanned_pages == {"A": 1, "B": 0}
    assert result.warnings == ["标签「B」扫描失败：LOFTER 返回非 DWR 响应：响应片段：{ status: 4009 }"]


@pytest.mark.asyncio
async def test_count_posts_propagates_unexpected_scan_errors():
    client = AsyncMock()
    client.search_tag.return_value = "raw-a-1"

    async def parser(raw):
        raise ValueError("parser bug")

    with pytest.raises(ValueError, match="parser bug"):
        await count_posts("A", client, parse_posts=parser)


@pytest.mark.asyncio
async def test_count_posts_reports_scanned_pages():
    client = AsyncMock()
    client.search_tag.side_effect = ["raw-a-1", "raw-a-2", ""]

    async def parser(raw):
        if raw == "raw-a-1":
            return [Post(post_id="p1", title="", summary="", tags=["A"])]
        if raw == "raw-a-2":
            return [Post(post_id="p2", title="", summary="", tags=["A"])]
        return []

    result = await count_posts("A", client, parse_posts=parser, page_size=1)

    assert result.scanned_pages == {"A": 2}
    assert result.warnings == []


@pytest.mark.asyncio
async def test_count_posts_warns_when_positive_offset_page_repeats_tag_posts():
    client = AsyncMock()
    client.search_tag.side_effect = ["raw-a-1", "raw-a-duplicate"]

    async def parser(raw):
        if raw == "raw-a-1":
            return [Post(post_id="p1", title="", summary="", tags=["A"])]
        if raw == "raw-a-duplicate":
            return [Post(post_id="p1", title="", summary="", tags=["A"])]
        return []

    result = await count_posts("A", client, parse_posts=parser, page_size=1)

    assert result.scanned_pages == {"A": 2}
    assert result.status == "部分完成"
    assert result.warnings == ["标签「A」疑似分页未生效或接口返回重复页"]


@pytest.mark.asyncio
async def test_all_failed_scans_are_not_reported_as_zero_matches():
    client = AsyncMock()
    client.search_tag.side_effect = RuntimeError("Cookie 失效")
    result = await count_posts("A|B", client)
    assert result.status == "失败"
    assert "Cookie 失效" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("expression", ["A|-B", "-(A -B)", "(A B)|(-C -D)"])
async def test_count_rejects_unbounded_negative_branches(expression):
    client = AsyncMock()
    with pytest.raises(CountExpressionError, match="每个 OR 分支"):
        await count_posts(expression, client)
    client.search_tag.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("expression", ["A -B", "A|-(-B)", "A (B|-C)", "-(A|-B)"])
async def test_count_accepts_bounded_branches(expression):
    client = AsyncMock()
    client.search_tag.return_value = "dwr.engine._remoteHandleCallback('0','0',[]);"
    result = await count_posts(expression, client)
    assert result.status == "扫描结束"
    assert result.count == 0


@pytest.mark.asyncio
async def test_count_passes_oldest_raw_timestamp_to_next_page():
    client = AsyncMock()
    client.search_tag.side_effect = ["first", "second", "empty"]

    async def parser(raw):
        if raw == "first":
            return [Post("a", "", "", tags=["A"], publish_time_ms=1720000000123)]
        if raw == "second":
            return [Post("b", "", "", tags=["A"], publish_time_ms=1710000000456)]
        return []

    result = await count_posts("A", client, parse_posts=parser)
    assert result.count == 2
    assert result.status == "扫描结束"
    calls = client.search_tag.call_args_list
    assert calls[1].kwargs == {"offset": 20, "limit": 20, "before": 1720000000123}
    assert calls[2].kwargs == {"offset": 40, "limit": 20, "before": 1710000000456}


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
        is_admin = True

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
