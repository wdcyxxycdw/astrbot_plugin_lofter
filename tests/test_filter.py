import json

import pytest

from core.filter import (
    FilterRule,
    apply_filter,
    filter_rule_from_json,
    filter_rule_to_json,
    has_filter,
    matches,
    parse_filter_expr,
)
from core.parser import Post


def _make_post(tags: list[str], post_id: str = "1") -> Post:
    return Post(post_id=post_id, title="", summary="", tags=tags)


# ---------- parse_filter_expr ----------

def test_parse_simple_tag():
    rule = parse_filter_expr("原创")
    assert rule.search_tags == ["原创"]
    assert rule.include_tags == []
    assert rule.exclude_tags == []
    assert rule.or_tag_groups == []


def test_parse_and_filter():
    rule = parse_filter_expr("原神 +同人")
    assert rule.search_tags == ["原神"]
    assert rule.include_tags == ["同人"]


def test_parse_exclude_filter():
    rule = parse_filter_expr("原神 -R18")
    assert rule.search_tags == ["原神"]
    assert rule.exclude_tags == ["R18"]


def test_parse_or_search_tags():
    rule = parse_filter_expr("原创|插画")
    assert rule.search_tags == ["原创", "插画"]


def test_parse_or_filter_group():
    rule = parse_filter_expr("原神 钟离|达达利亚")
    assert rule.or_tag_groups == [["钟离", "达达利亚"]]


def test_parse_mixed():
    rule = parse_filter_expr("原神 +同人 -R18 钟离|达达利亚")
    assert rule.search_tags == ["原神"]
    assert rule.include_tags == ["同人"]
    assert rule.exclude_tags == ["R18"]
    assert rule.or_tag_groups == [["钟离", "达达利亚"]]


def test_parse_bare_word_as_include():
    rule = parse_filter_expr("原神 甜文")
    assert rule.include_tags == ["甜文"]


def test_parse_empty_string():
    rule = parse_filter_expr("")
    assert rule.search_tags == []


def test_parse_raw_preserved():
    raw = "原神 +同人 -R18"
    rule = parse_filter_expr(raw)
    assert rule.raw == raw


# ---------- matches ----------

def test_matches_empty_rule():
    rule = FilterRule(search_tags=[])
    post = _make_post(["任意标签"])
    assert matches(post, rule) is True


def test_matches_include_all_present():
    rule = FilterRule(search_tags=[], include_tags=["同人", "原创"])
    post = _make_post(["同人", "原创", "其他"])
    assert matches(post, rule) is True


def test_matches_include_missing_one():
    rule = FilterRule(search_tags=[], include_tags=["同人", "R18"])
    post = _make_post(["同人"])
    assert matches(post, rule) is False


def test_matches_exclude_hit():
    rule = FilterRule(search_tags=[], exclude_tags=["R18"])
    post = _make_post(["同人", "R18"])
    assert matches(post, rule) is False


def test_matches_exclude_miss():
    rule = FilterRule(search_tags=[], exclude_tags=["R18"])
    post = _make_post(["同人"])
    assert matches(post, rule) is True


def test_matches_or_group_hit():
    rule = FilterRule(search_tags=[], or_tag_groups=[["钟离", "达达利亚"]])
    post = _make_post(["钟离"])
    assert matches(post, rule) is True


def test_matches_or_group_miss():
    rule = FilterRule(search_tags=[], or_tag_groups=[["钟离", "达达利亚"]])
    post = _make_post(["雷电将军"])
    assert matches(post, rule) is False


def test_matches_case_insensitive():
    rule = FilterRule(search_tags=[], include_tags=["R18"], exclude_tags=["nsfw"])
    post = _make_post(["r18", "NSFW"])
    assert matches(post, rule) is False


def test_matches_combined():
    rule = FilterRule(
        search_tags=[],
        include_tags=["同人"],
        exclude_tags=["R18"],
        or_tag_groups=[["钟离", "达达利亚"]],
    )
    assert matches(_make_post(["同人", "钟离"]), rule) is True
    assert matches(_make_post(["同人", "钟离", "R18"]), rule) is False
    assert matches(_make_post(["同人"]), rule) is False
    assert matches(_make_post(["钟离"]), rule) is False


# ---------- apply_filter ----------

def test_apply_filter_empty_rule():
    rule = FilterRule(search_tags=[])
    posts = [_make_post(["a"], "1"), _make_post(["b"], "2")]
    assert apply_filter(posts, rule) == posts


def test_apply_filter_filters_and_keeps_order():
    rule = FilterRule(search_tags=[], include_tags=["同人"])
    posts = [
        _make_post(["同人"], "1"),
        _make_post(["原创"], "2"),
        _make_post(["同人", "原创"], "3"),
    ]
    result = apply_filter(posts, rule)
    assert [p.post_id for p in result] == ["1", "3"]


def test_apply_filter_all_filtered():
    rule = FilterRule(search_tags=[], include_tags=["不存在的标签"])
    posts = [_make_post(["同人"], "1"), _make_post(["原创"], "2")]
    assert apply_filter(posts, rule) == []


# ---------- JSON 序列化往返 ----------

def test_json_roundtrip():
    rule = FilterRule(
        search_tags=["原神", "插画"],
        include_tags=["同人"],
        exclude_tags=["R18"],
        or_tag_groups=[["钟离", "达达利亚"]],
        raw="原神 +同人 -R18 钟离|达达利亚",
    )
    restored = filter_rule_from_json(filter_rule_to_json(rule))
    assert restored == rule


# ---------- has_filter ----------

def test_has_filter_empty():
    assert has_filter(FilterRule(search_tags=[])) is False


def test_has_filter_include():
    assert has_filter(FilterRule(search_tags=[], include_tags=["同人"])) is True


def test_has_filter_exclude():
    assert has_filter(FilterRule(search_tags=[], exclude_tags=["R18"])) is True


def test_has_filter_or_groups():
    assert has_filter(FilterRule(search_tags=[], or_tag_groups=[["a", "b"]])) is True
