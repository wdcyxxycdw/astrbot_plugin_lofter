from __future__ import annotations

import csv
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .tag_count import CountResult

NormalizedCountStatus = Literal["success", "partial", "failed"]
_STATUS_ALIASES = {
    "success": "success",
    "成功": "success",
    "partial": "partial",
    "部分": "partial",
    "failed": "failed",
    "失败": "failed",
}
_STATUS_LABELS = {
    "success": "成功",
    "partial": "部分",
    "failed": "失败",
}


def normalize_count_status(status: str) -> NormalizedCountStatus:
    return _STATUS_ALIASES.get(status, "failed")


def format_count_result(result: CountResult) -> str:
    status = normalize_count_status(result.status)
    lines = [_headline(result, status)]
    if status != "failed":
        lines.append(f"候选作品：{result.candidates}")
        lines.append(
            f"扫描页数：{format_scanned_pages(result.scanned_pages) or '无'}"
        )
    lines.append(f"条件：{result.expression}")
    if result.error:
        lines.append(f"提示：{result.error}")
    lines.extend(f"提示：{warning}" for warning in result.warnings)
    return "\n".join(lines)


def _headline(
    result: CountResult, status: NormalizedCountStatus
) -> str:
    name = f"「{result.name}」" if result.name else "该条件"
    if status == "success":
        return f"{name}统计成功：{result.count} 个作品"
    if status == "partial":
        return f"{name}统计不完整：至少 {result.count} 个作品"
    return f"{name}统计失败"


def build_count_csv_path(base_dir, counted_at: str) -> Path:
    stamp = datetime.strptime(
        counted_at, "%Y-%m-%d %H:%M:%S"
    ).strftime("%Y%m%d_%H%M%S")
    return Path(base_dir) / f"lofter_count_{stamp}.csv"


def build_count_csv(rows: list[CountResult]) -> str:
    output = StringIO()
    fields = [
        "名称",
        "条件",
        "作品数",
        "候选作品",
        "扫描页数",
        "状态",
        "错误信息",
        "统计时间",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(_csv_row(row) for row in rows)
    return output.getvalue()


def _csv_row(row: CountResult) -> dict[str, str | int]:
    status = normalize_count_status(row.status)
    visible = status != "failed"
    return {
        "名称": row.name,
        "条件": row.expression,
        "作品数": _csv_count(row.count, status),
        "候选作品": row.candidates if visible else "",
        "扫描页数": (
            format_scanned_pages(row.scanned_pages) if visible else ""
        ),
        "状态": _STATUS_LABELS[status],
        "错误信息": _format_error(row),
        "统计时间": row.counted_at,
    }


def _csv_count(count: int, status: NormalizedCountStatus) -> str:
    if status == "failed":
        return ""
    if status == "partial":
        return f"至少 {count}"
    return str(count)


def format_scanned_pages(scanned_pages: dict[str, int]) -> str:
    return "；".join(
        f"{tag}:{pages}" for tag, pages in scanned_pages.items()
    )


def _format_error(row: CountResult) -> str:
    parts = [row.error] if row.error else []
    parts.extend(row.warnings)
    return "；".join(parts)
