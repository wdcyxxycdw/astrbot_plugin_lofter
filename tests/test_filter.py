import pytest

from core.filter import (
    FilterRule,
    apply_filter,
    matches,
    parse_tag_expr,
)
from core.parser import Post


def _make_post(tags: list[str], post_id: str = "1") -> Post:
    return Post(post_id=post_id, title="", summary="", tags=tags)


# ---------- parse_tag_expr ----------

def test_parse_simple_tag():
    subs, excls = parse_tag_expr("原创")
    assert subs == ["原创"]
    assert excls == []


def test_parse_exclude_only():
    """纯 exclude bug 回归：-B 不应被当成 search_tag"""
    subs, excls = parse_tag_expr("-B")
    assert subs == []
    assert excls == ["B"]


def test_parse_subscribe_and_exclude():
    subs, excls = parse_tag_expr("原神 -R18")
    assert subs == ["原神"]
    assert excls == ["R18"]


def test_parse_plus_prefix_is_subscribe():
    subs, excls = parse_tag_expr("+原神")
    assert subs == ["原神"]
    assert excls == []


def test_parse_or_in_subscribe():
    subs, excls = parse_tag_expr("原创|插画")
    assert subs == ["原创", "插画"]
    assert excls == []


def test_parse_or_in_exclude():
    subs, excls = parse_tag_expr("原神 -R18|NSFW")
    assert subs == ["原神"]
    assert set(excls) == {"R18", "NSFW"}


def test_parse_multiple_subscribes():
    subs, excls = parse_tag_expr("原神 崩铁")
    assert subs == ["原神", "崩铁"]
    assert excls == []


def test_parse_empty_string():
    subs, excls = parse_tag_expr("")
    assert subs == []
    assert excls == []


def test_parse_tag_expr_pure_exclude():
    """确认 -B 只产生 exclude，不产生 subscribe"""
    subs, excls = parse_tag_expr("-B")
    assert excls == ["B"]
    assert subs == []


def test_parse_mixed():
    subs, excls = parse_tag_expr("原神 +同人 -R18")
    assert "原神" in subs
    assert "同人" in subs
    assert excls == ["R18"]


# ---------- matches ----------

def test_matches_empty_rule():
    rule = FilterRule(search_tags=[])
    post = _make_post(["任意标签"])
    assert matches(post, rule) is True


def test_matches_exclude_hit():
    rule = FilterRule(search_tags=[], exclude_tags=["R18"])
    post = _make_post(["同人", "R18"])
    assert matches(post, rule) is False


def test_matches_exclude_miss():
    rule = FilterRule(search_tags=[], exclude_tags=["R18"])
    post = _make_post(["同人"])
    assert matches(post, rule) is True


def test_matches_case_insensitive():
    rule = FilterRule(search_tags=[], exclude_tags=["nsfw"])
    post = _make_post(["NSFW"])
    assert matches(post, rule) is False


def test_matches_multiple_excludes():
    rule = FilterRule(search_tags=[], exclude_tags=["R18", "NSFW"])
    assert matches(_make_post(["同人"]), rule) is True
    assert matches(_make_post(["R18"]), rule) is False
    assert matches(_make_post(["NSFW"]), rule) is False


# ---------- apply_filter ----------

def test_apply_filter_empty_rule():
    rule = FilterRule(search_tags=[])
    posts = [_make_post(["a"], "1"), _make_post(["b"], "2")]
    assert apply_filter(posts, rule) == posts


def test_apply_filter_exclude():
    rule = FilterRule(search_tags=[], exclude_tags=["R18"])
    posts = [
        _make_post(["原创"], "1"),
        _make_post(["R18"], "2"),
        _make_post(["原创", "R18"], "3"),
    ]
    result = apply_filter(posts, rule)
    assert [p.post_id for p in result] == ["1"]


def test_apply_filter_all_filtered():
    rule = FilterRule(search_tags=[], exclude_tags=["R18"])
    posts = [_make_post(["R18"], "1"), _make_post(["R18", "NSFW"], "2")]
    assert apply_filter(posts, rule) == []
