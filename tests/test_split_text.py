import pytest
from core.utils import _split_text


def test_split_short_text():
    result = _split_text("短文本", limit=2000)
    assert result == ["短文本"]


def test_split_by_paragraphs():
    text = "\n\n".join([f"段落{i}" * 10 for i in range(5)])
    result = _split_text(text, limit=20)
    assert len(result) >= 2
    for chunk in result:
        assert len(chunk) <= 20 + 50  # 允许段落边界的少量超出


def test_split_hard_cut_long_paragraph():
    long_para = "字" * 3000
    result = _split_text(long_para, limit=2000)
    assert len(result) >= 2
    for chunk in result[:-1]:  # 最后一个可能是提示
        assert len(chunk) <= 2000


def test_split_max_chunks():
    text = "\n\n".join([f"段落{i}" for i in range(20)])
    result = _split_text(text, limit=10, max_chunks=5)
    assert len(result) <= 6  # 5 chunks + 可能的截断提示
    assert "全文过长" in result[-1]


def test_split_empty_text():
    result = _split_text("", limit=2000)
    assert isinstance(result, list)
    assert len(result) >= 1
