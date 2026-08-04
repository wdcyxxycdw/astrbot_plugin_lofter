from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

import core.content_source as source_module
from core.content_source import DefaultContentSource, MobileTagDiagnostic
from core.dwr_parser import DWRParseResult
from core.errors import (
    SourceBusinessError,
    SourceChallengeError,
    SourceClosingError,
    SourceHTTPError,
    SourceLimitError,
    SourcePartialError,
    SourceRetryExhaustedError,
    SourceSchemaError,
    SourceTimeoutError,
    attach_source_evidence,
    mark_limit_identity_complete,
)
from core.mobile_parser import MobilePage
from core.parser import Post
from core.source_scan import ContentSource, collect_pages


def make_post(post_id, when="2026-01-01 00:00"):
    return Post(
        post_id=post_id,
        title=post_id,
        summary="",
        url=f"https://demo.lofter.com/post/{post_id}",
        publish_time=when,
    )


def mobile_page(
    items,
    *,
    cursor=None,
    exhausted=True,
    source="mobile_tag",
    sort="new",
    dropped=0,
    complete=True,
    evidence=(),
    identity_records=(),
):
    return MobilePage(
        items=items,
        source=source,
        next_cursor=cursor,
        exhausted=exhausted,
        sort=sort,
        mapped_count=len(items),
        dropped_count=dropped,
        complete=complete,
        evidence_items=evidence,
        identity_records=identity_records or tuple(items),
    )


class FakeClient:
    def __init__(self):
        self.get = AsyncMock()
        self.search_tag = AsyncMock()
        self.initialize = AsyncMock()
        self.close = AsyncMock()
        self.cookies = []

    def update_cookie(self, cookie):
        self.cookies.append(cookie)


@pytest.mark.asyncio
async def test_lifecycle_delegates_to_single_client():
    client = FakeClient()
    source = DefaultContentSource(client)

    source.update_cookie("demo=value")
    await source.initialize()
    await source.close()

    assert client.cookies == ["demo=value"]
    client.initialize.assert_awaited_once()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_hex_slug_uses_mobile_detail_with_decimal_ids():
    client = FakeClient()
    source = DefaultContentSource(client)
    expected = make_post("1a_2b")
    source._mobile = SimpleNamespace(get_post=AsyncMock(return_value=expected))

    result = await source.get_post("https://demo.lofter.com/post/1a_2b")

    assert result is expected
    source._mobile.get_post.assert_awaited_once_with("26", "43")
    client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_opaque_slug_skips_mobile_and_uses_embedded(monkeypatch):
    client = FakeClient()
    client.get.return_value = "<html>embedded</html>"
    source = DefaultContentSource(client)
    source._mobile = SimpleNamespace(get_post=AsyncMock())
    expected = make_post("opaque-id")
    monkeypatch.setattr(source_module, "parse_embedded_post", lambda *_, **__: expected)

    result = await source.get_post("https://demo.lofter.com/post/opaque-id")

    assert result is expected
    source._mobile.get_post.assert_not_awaited()
    client.get.assert_awaited_once_with(
        "https://demo.lofter.com/post/opaque-id", credentialed=False
    )


@pytest.mark.asyncio
async def test_post_cookie_only_used_for_authorized_last_fallback(monkeypatch):
    client = FakeClient()
    client.get.side_effect = ["public", "credentialed"]
    source = DefaultContentSource(client)
    source._mobile = SimpleNamespace(
        get_post=AsyncMock(side_effect=SourceSchemaError("mobile"))
    )
    monkeypatch.setattr(
        source_module, "parse_embedded_post",
        lambda *_, **__: (_ for _ in ()).throw(SourceSchemaError("embedded")),
    )
    expected = make_post("1a_2b")
    parser = AsyncMock(side_effect=[SourceSchemaError("html"), expected])
    monkeypatch.setattr(source_module, "parse_post_page", parser)

    result = await source.get_post("https://demo.lofter.com/post/1a_2b")

    assert result is expected
    assert client.get.await_args_list[0].kwargs == {"credentialed": False}
    assert client.get.await_args_list[1].kwargs == {"credentialed": True}


@pytest.mark.asyncio
async def test_tag_primary_first_page_failure_switches_to_dwr_start(monkeypatch):
    client = FakeClient()
    client.search_tag.return_value = "dwr"
    source = DefaultContentSource(client)
    source._mobile = SimpleNamespace(
        list_tag=AsyncMock(side_effect=SourceSchemaError("mobile"))
    )
    expected = make_post("fallback")
    monkeypatch.setattr(
        source_module,
        "parse_dwr_response_result",
        AsyncMock(return_value=DWRParseResult([expected], 1, 0, False)),
    )

    result = await source.list_tag("demo", None, 20, "new")

    assert result.source == "dwr"
    assert result.restarted is False
    assert result.restart_requires_prior_coverage is True
    assert result.items == [expected]
    assert result.diagnostics == (
        "fallback_dwr",
        "mobile_fallback:mobile_schema",
    )
    client.search_tag.assert_awaited_once_with("demo", 0, 20)


@pytest.mark.asyncio
async def test_ordered_terminal_mobile_tag_page_is_used_directly():
    client = FakeClient()
    source = DefaultContentSource(client)
    posts = [
        make_post("new", "2026-01-09 00:00:00"),
        make_post("old", "2026-01-07 00:00:00"),
    ]
    source._mobile = SimpleNamespace(
        list_tag=AsyncMock(return_value=mobile_page(posts))
    )

    result = await source.list_tag("demo", None, 20, "new")

    assert result.source == "mobile_tag"
    assert result.items == posts
    client.search_tag.assert_not_awaited()


@pytest.mark.asyncio
async def test_mobile_tag_diagnostic_accepts_eligible_page_without_dwr():
    client = FakeClient()
    source = DefaultContentSource(client)
    posts = [
        make_post("new", "2026-01-09 00:00:00"),
        make_post("old", "2026-01-07 00:00:00"),
    ]
    source._mobile = SimpleNamespace(
        list_tag=AsyncMock(return_value=mobile_page(posts))
    )

    result = await source.diagnose_mobile_tag("demo", 20)

    assert result.page is not None
    assert result.page.items == posts
    assert result.evidence_items == tuple(posts)
    assert result.fallback_reason is None
    assert result.error is None
    client.search_tag.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page", "reason"),
    [
        (mobile_page([], complete=False), "mobile_incomplete"),
        (mobile_page([], sort="hot"), "mobile_sort_mismatch"),
        (
            mobile_page([make_post("missing")]),
            "mobile_publish_time_invalid",
        ),
    ],
)
async def test_mobile_tag_diagnostic_reports_page_rejection(page, reason):
    client = FakeClient()
    source = DefaultContentSource(client)
    source._mobile = SimpleNamespace(list_tag=AsyncMock(return_value=page))

    result = await source.diagnose_mobile_tag("demo", 20)

    assert result.page is not None
    assert result.fallback_reason == reason
    assert result.error is None
    client.search_tag.assert_not_awaited()


@pytest.mark.asyncio
async def test_mobile_tag_diagnostic_normalizes_order_and_keeps_raw_scalars():
    client = FakeClient()
    source = DefaultContentSource(client)
    posts = [
        make_post("a", "2026-01-09 00:00:00"),
        make_post("b", "2026-01-07 00:00:00"),
        make_post("c", "2026-01-08 00:00:00"),
        make_post("d", "2026-01-08 00:00:00"),
        make_post("e", "2026-01-10 00:00:00"),
    ]
    source._mobile = SimpleNamespace(
        list_tag=AsyncMock(return_value=mobile_page(posts))
    )

    result = await source.diagnose_mobile_tag("demo", 20)

    assert result.fallback_reason is None
    assert result.page is not None
    assert [post.post_id for post in result.page.items] == ["e", "a", "c", "d", "b"]
    assert result.evidence_items == tuple(posts)
    assert result.item_count == 5
    assert result.time_count == 5
    assert result.regression_count == 2
    assert result.equal_count == 1
    assert result.first_regression_pair_ordinal == 2
    client.search_tag.assert_not_awaited()


@pytest.mark.asyncio
async def test_mobile_tag_normalization_returns_latest_limit_stably():
    client = FakeClient()
    source = DefaultContentSource(client)
    posts = [
        make_post("nine", "2026-01-09 00:00:00"),
        make_post("seven", "2026-01-07 00:00:00"),
        make_post("eight-a", "2026-01-08 00:00:00"),
        make_post("eight-b", "2026-01-08 00:00:00"),
        make_post("ten", "2026-01-10 00:00:00"),
    ]
    source._mobile = SimpleNamespace(
        list_tag=AsyncMock(return_value=mobile_page(posts))
    )

    result = await collect_pages(
        lambda cursor: source.list_tag("demo", cursor, 20, "new"),
        limit=3,
    )

    assert [post.post_id for post in result.items] == [
        "ten", "nine", "eight-a",
    ]
    assert result.source == "mobile_tag"
    client.search_tag.assert_not_awaited()


@pytest.mark.asyncio
async def test_mobile_tag_diagnostic_does_not_invent_order_counts_for_invalid_time():
    client = FakeClient()
    source = DefaultContentSource(client)
    posts = [
        make_post("a", "2026-01-09 00:00:00"),
        make_post("b", "private-invalid-time"),
    ]
    source._mobile = SimpleNamespace(
        list_tag=AsyncMock(return_value=mobile_page(posts))
    )

    result = await source.diagnose_mobile_tag("demo", 20)

    assert result.fallback_reason == "mobile_publish_time_invalid"
    assert result.item_count == 2
    assert result.time_count == 1
    assert result.regression_count == 0
    assert result.equal_count == 0
    assert result.first_regression_pair_ordinal == 0


def test_mobile_tag_diagnostic_keeps_legacy_positional_constructor():
    diagnostic = MobileTagDiagnostic(None, (), None, None)

    assert diagnostic.item_count == 0
    assert diagnostic.time_count == 0
    assert diagnostic.regression_count == 0
    assert diagnostic.equal_count == 0
    assert diagnostic.first_regression_pair_ordinal == 0


@pytest.mark.asyncio
async def test_mobile_tag_diagnostic_reports_missing_publish_time():
    client = FakeClient()
    source = DefaultContentSource(client)
    post = make_post("missing", "2026-01-09 00:00:00")
    post.completeness = post.completeness - {"publish_time"}
    source._mobile = SimpleNamespace(
        list_tag=AsyncMock(return_value=mobile_page([post]))
    )

    result = await source.diagnose_mobile_tag("demo", 20)

    assert result.fallback_reason == "mobile_publish_time_missing"
    client.search_tag.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (SourceHTTPError(503), "mobile_http"),
        (SourceTimeoutError(), "mobile_timeout"),
        (SourceRetryExhaustedError(3), "mobile_retry_exhausted"),
        (SourceChallengeError(), "mobile_challenge"),
        (SourceBusinessError(500), "mobile_business"),
        (SourceSchemaError("response"), "mobile_schema"),
        (SourcePartialError(0, 1), "mobile_partial"),
    ],
)
async def test_mobile_tag_diagnostic_classifies_fallback_errors(error, reason):
    client = FakeClient()
    source = DefaultContentSource(client)
    source._mobile = SimpleNamespace(list_tag=AsyncMock(side_effect=error))

    result = await source.diagnose_mobile_tag("demo", 20)

    assert result.page is None
    assert result.fallback_reason == reason
    assert result.error is error
    client.search_tag.assert_not_awaited()


@pytest.mark.asyncio
async def test_mobile_tag_diagnostic_classifies_safe_limit_error():
    client = FakeClient()
    source = DefaultContentSource(client)
    error = SourceLimitError("items", 100)
    mark_limit_identity_complete(error)
    source._mobile = SimpleNamespace(list_tag=AsyncMock(side_effect=error))

    result = await source.diagnose_mobile_tag("demo", 20)

    assert result.fallback_reason == "mobile_limit"
    assert result.error is error
    client.search_tag.assert_not_awaited()


@pytest.mark.asyncio
async def test_mobile_tag_diagnostic_propagates_identity_schema_error():
    client = FakeClient()
    source = DefaultContentSource(client)
    error = SourceSchemaError("post.id")
    source._mobile = SimpleNamespace(list_tag=AsyncMock(side_effect=error))

    with pytest.raises(SourceSchemaError) as exc_info:
        await source.diagnose_mobile_tag("demo", 20)

    assert exc_info.value is error
    client.search_tag.assert_not_awaited()


def test_content_source_protocol_keeps_diagnostic_concrete():
    assert "diagnose_mobile_tag" not in ContentSource.__dict__


@pytest.mark.asyncio
async def test_mobile_tag_page_with_unknown_publish_time_uses_dwr(monkeypatch):
    client = FakeClient()
    client.search_tag.return_value = "dwr"
    source = DefaultContentSource(client)
    primary = make_post("primary")
    primary.completeness = primary.completeness - {"publish_time"}
    fallback = make_post("primary")
    source._mobile = SimpleNamespace(
        list_tag=AsyncMock(return_value=mobile_page([primary]))
    )
    monkeypatch.setattr(
        source_module,
        "parse_dwr_response_result",
        AsyncMock(return_value=DWRParseResult([fallback], 1, 0, False)),
    )

    result = await source.list_tag("demo", None, 20, "new")

    assert result.items == [fallback]
    assert result.evidence_items == (primary,)
    assert result.diagnostics == (
        "fallback_dwr",
        "mobile_fallback:mobile_publish_time_missing",
    )
    client.search_tag.assert_awaited_once_with("demo", 0, 20)


@pytest.mark.asyncio
async def test_legacy_mobile_cursor_restarts_dwr_from_zero(monkeypatch):
    client = FakeClient()
    client.search_tag.return_value = "dwr"
    source = DefaultContentSource(client)
    source._mobile = SimpleNamespace(list_tag=AsyncMock())
    expected = make_post("fallback")
    monkeypatch.setattr(
        source_module,
        "parse_dwr_response_result",
        AsyncMock(return_value=DWRParseResult([expected], 1, 0, False)),
    )

    result = await source.list_tag(
        "demo", "v1:mobile_tag:mobile-2", 20, "new"
    )

    assert result.items == [expected]
    assert result.restarted is True
    assert result.restart_requires_prior_coverage is False
    assert result.diagnostics == (
        "fallback_dwr",
        "mobile_fallback:mobile_cursor_restart",
    )
    source._mobile.list_tag.assert_not_awaited()
    client.search_tag.assert_awaited_once_with("demo", 0, 20)


@pytest.mark.asyncio
async def test_mobile_list_identity_error_propagates_without_fallback():
    client = FakeClient()
    source = DefaultContentSource(client)
    source._mobile = SimpleNamespace(
        list_tag=AsyncMock(
            side_effect=SourceSchemaError("postData.postCount.blogId")
        )
    )

    with pytest.raises(SourceSchemaError) as exc_info:
        await source.list_tag("demo", None, 20, "new")

    assert exc_info.value.location == "postData.postCount.blogId"
    client.search_tag.assert_not_awaited()


@pytest.mark.asyncio
async def test_automatic_dwr_failure_keeps_type_and_mobile_reason(monkeypatch):
    client = FakeClient()
    client.search_tag.return_value = "dwr"
    source = DefaultContentSource(client)
    source._mobile = SimpleNamespace(
        list_tag=AsyncMock(side_effect=SourceTimeoutError())
    )
    error = SourceSchemaError("dwr.items")
    monkeypatch.setattr(
        source_module,
        "parse_dwr_response_result",
        AsyncMock(side_effect=error),
    )

    with pytest.raises(SourceSchemaError) as exc_info:
        await source.list_tag("demo", None, 20, "new")

    assert exc_info.value is error
    assert error.mobile_fallback_reason == "mobile_timeout"


@pytest.mark.asyncio
async def test_explicit_dwr_cursor_has_no_mobile_fallback_diagnostic(monkeypatch):
    client = FakeClient()
    client.search_tag.return_value = "dwr"
    source = DefaultContentSource(client)
    source._mobile = SimpleNamespace(list_tag=AsyncMock())
    expected = make_post("dwr", "2026-01-09 00:00:00")
    monkeypatch.setattr(
        source_module,
        "parse_dwr_response_result",
        AsyncMock(return_value=DWRParseResult([expected], 1, 0, False)),
    )

    result = await source.list_tag("demo", "v1:dwr:0", 20, "new")

    assert result.items == [expected]
    assert result.diagnostics == ()
    source._mobile.list_tag.assert_not_awaited()
    assert not hasattr(result, "mobile_fallback_reason")


@pytest.mark.asyncio
async def test_explicit_dwr_failure_has_no_mobile_fallback_reason(monkeypatch):
    client = FakeClient()
    client.search_tag.return_value = "dwr"
    source = DefaultContentSource(client)
    error = SourceSchemaError("dwr.items")
    monkeypatch.setattr(
        source_module,
        "parse_dwr_response_result",
        AsyncMock(side_effect=error),
    )

    with pytest.raises(SourceSchemaError) as exc_info:
        await source.list_tag("demo", "v1:dwr:0", 20, "new")

    assert exc_info.value is error
    assert not hasattr(error, "mobile_fallback_reason")


@pytest.mark.asyncio
async def test_dwr_identity_error_cannot_be_hidden_by_primary(monkeypatch):
    client = FakeClient()
    client.search_tag.return_value = "dwr"
    source = DefaultContentSource(client)
    source._mobile = SimpleNamespace(
        list_tag=AsyncMock(
            return_value=mobile_page(
                [make_post("primary")], dropped=1, complete=False
            )
        )
    )
    monkeypatch.setattr(
        source_module,
        "parse_dwr_response_result",
        AsyncMock(side_effect=SourceSchemaError("dwr.post.id")),
    )

    with pytest.raises(SourceSchemaError) as exc_info:
        await source.list_tag("demo", None, 20, "new")

    assert exc_info.value.location == "dwr.post.id"


@pytest.mark.asyncio
async def test_nonterminal_tag_page_restarts_dwr_from_zero(monkeypatch):
    client = FakeClient()
    client.search_tag.return_value = "dwr"
    source = DefaultContentSource(client)
    primary = make_post("primary", "2026-01-09 00:00:00")
    fallback = make_post("primary", "2026-01-09 00:00:00")
    source._mobile = SimpleNamespace(
        list_tag=AsyncMock(
            return_value=mobile_page(
                [primary], cursor="mobile-2", exhausted=False
            )
        )
    )
    parser = AsyncMock(side_effect=[
        DWRParseResult([fallback], 1, 0, False),
        DWRParseResult([], 0, 0, True),
    ])
    monkeypatch.setattr(source_module, "parse_dwr_response_result", parser)

    result = await collect_pages(
        lambda cursor: source.list_tag("demo", cursor, 20, "new")
    )

    assert result.items == [fallback]
    assert [item.post_id for item in result.evidence_items] == ["primary"]
    assert result.source == "dwr"
    assert result.restarted is True
    assert "mobile_fallback:mobile_cursor_restart" in result.diagnostics
    source._mobile.list_tag.assert_awaited_once_with("demo", None)
    assert client.search_tag.await_args_list == [
        call("demo", 0, 20),
        call("demo", 20, 20),
    ]


@pytest.mark.asyncio
async def test_mobile_cursor_restart_stops_at_dwr_business_limit(monkeypatch):
    client = FakeClient()
    client.search_tag.side_effect = [
        "dwr",
        AssertionError("new scope must not fetch a DWR tail page"),
    ]
    source = DefaultContentSource(client)
    primary = [
        make_post(f"mobile-{index}", f"2026-01-{31 - index:02d} 00:00:00")
        for index in range(9)
    ]
    fallback = [
        make_post(f"dwr-{index}", f"2026-01-{22 - index:02d} 00:00:00")
        for index in range(20)
    ]
    source._mobile = SimpleNamespace(
        list_tag=AsyncMock(
            return_value=mobile_page(
                primary, cursor="mobile-2", exhausted=False
            )
        )
    )
    monkeypatch.setattr(
        source_module,
        "parse_dwr_response_result",
        AsyncMock(return_value=DWRParseResult(fallback, 20, 0, False)),
    )

    result = await collect_pages(
        lambda cursor: source.list_tag("demo", cursor, 20, "new"),
        limit=20,
    )

    assert result.items == fallback
    assert [post.post_id for post in result.evidence_items] == [
        post.post_id for post in primary
    ]
    assert result.source == "dwr"
    assert result.restarted is True
    source._mobile.list_tag.assert_awaited_once_with("demo", None)
    client.search_tag.assert_awaited_once_with("demo", 0, 20)


@pytest.mark.asyncio
async def test_unordered_terminal_tag_page_is_normalized_without_dwr():
    client = FakeClient()
    source = DefaultContentSource(client)
    primary_old = make_post("old", "2026-01-07 00:00:00")
    primary_new = make_post("new", "2026-01-09 00:00:00")
    source._mobile = SimpleNamespace(
        list_tag=AsyncMock(return_value=mobile_page([primary_old, primary_new]))
    )

    result = await collect_pages(
        lambda cursor: source.list_tag("demo", cursor, 20, "new")
    )

    assert result.items == [primary_new, primary_old]
    assert result.evidence_items == ()
    assert result.source == "mobile_tag"
    client.search_tag.assert_not_awaited()


@pytest.mark.asyncio
async def test_incomplete_blog_page_restarts_html_and_discards_primary(monkeypatch):
    client = FakeClient()
    client.get.return_value = "html"
    source = DefaultContentSource(client)
    primary = make_post("primary", "2026-01-09 00:00")
    fallback = make_post("primary", "2026-01-09 00:00")
    fallback_2 = make_post("mixed", "2026-01-07 00:00")
    source._mobile = SimpleNamespace(
        list_blog=AsyncMock(
            side_effect=[
                mobile_page(
                    [primary], cursor="mobile-2", exhausted=False,
                    source="mobile_blog",
                ),
                mobile_page(
                    [make_post("mixed", "2026-01-07 00:00")],
                    source="mobile_blog",
                    dropped=1,
                    complete=False,
                ),
            ]
        )
    )
    monkeypatch.setattr(
        source_module,
        "parse_blog_posts",
        AsyncMock(return_value=[fallback, fallback_2]),
    )

    result = await collect_pages(
        lambda cursor: source.list_blog("demo", cursor, 20)
    )

    assert [item.post_id for item in result.items] == ["primary", "mixed"]
    assert result.items == [fallback, fallback_2]
    assert result.source == "html_blog"
    client.get.assert_awaited_once_with(
        "https://demo.lofter.com", credentialed=False
    )


@pytest.mark.asyncio
async def test_nonterminal_mobile_fallback_failure_is_typed_partial():
    client = FakeClient()
    client.search_tag.side_effect = SourceSchemaError("dwr")
    source = DefaultContentSource(client)
    primary = make_post("primary", "2026-01-09 00:00:00")
    source._mobile = SimpleNamespace(
        list_tag=AsyncMock(
            return_value=mobile_page(
                [primary], cursor="mobile-2", exhausted=False
            )
        )
    )

    with pytest.raises(SourcePartialError) as exc_info:
        await collect_pages(
            lambda cursor: source.list_tag("demo", cursor, 20, "new")
        )

    assert exc_info.value.mapped_count == 1
    assert [post.post_id for post in exc_info.value.evidence_items] == ["primary"]


@pytest.mark.asyncio
async def test_blog_mobile_failure_uses_credentialed_html_only_after_public_failure(monkeypatch):
    client = FakeClient()
    client.get.side_effect = [SourceSchemaError("public"), "credentialed html"]
    source = DefaultContentSource(client)
    source._mobile = SimpleNamespace(
        list_blog=AsyncMock(side_effect=SourceSchemaError("mobile"))
    )
    expected = make_post("blog")
    monkeypatch.setattr(
        source_module, "parse_blog_posts", AsyncMock(return_value=[expected])
    )

    result = await source.list_blog("demo", None, 20)

    assert result.source == "html_blog"
    assert result.exhausted is True
    assert result.items == [expected]
    assert client.get.await_args_list[0].kwargs == {"credentialed": False}
    assert client.get.await_args_list[1].kwargs == {"credentialed": True}


@pytest.mark.asyncio
async def test_public_blog_owner_conflict_propagates_without_credentials():
    public = """
    <link rel="canonical" href="https://alice.lofter.com/">
    <a href="https://bob.lofter.com/post/1a_2b">Bob</a>
    """
    credentialed = """
    <link rel="canonical" href="https://alice.lofter.com/">
    <a href="https://alice.lofter.com/post/1a_2b">Alice</a>
    """
    client = FakeClient()
    client.get.side_effect = [public, credentialed]
    source = DefaultContentSource(client)
    source._mobile = SimpleNamespace(
        list_blog=AsyncMock(side_effect=SourceSchemaError("response"))
    )

    with pytest.raises(SourceSchemaError) as exc_info:
        await source.list_blog("alice", None, 20)

    assert exc_info.value.location == "post.owner"
    client.get.assert_awaited_once_with(
        "https://alice.lofter.com", credentialed=False
    )


@pytest.mark.asyncio
async def test_detail_witness_owner_conflict_blocks_fallback():
    client = FakeClient()
    source = DefaultContentSource(client)
    error = SourceSchemaError("title")
    attach_source_evidence(error, (make_post("1a_2b"),))
    error.evidence_items[0].author_username = "other"
    error.evidence_items[0].url = "https://other.lofter.com/post/1a_2b"
    source._mobile = SimpleNamespace(get_post=AsyncMock(side_effect=error))

    with pytest.raises(SourceSchemaError) as exc_info:
        await source.get_post("https://demo.lofter.com/post/1a_2b")

    assert exc_info.value.location == "post.owner"
    client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_tag_fallback_must_cover_dropped_identity_record(monkeypatch):
    client = FakeClient()
    client.search_tag.return_value = "dwr"
    source = DefaultContentSource(client)
    visible = make_post("1a_2b", "2026-01-02 00:00")
    dropped = make_post("1a_2c", "2026-01-01 00:00")
    source._mobile = SimpleNamespace(
        list_tag=AsyncMock(return_value=mobile_page(
            [visible],
            dropped=1,
            complete=False,
            evidence=(dropped,),
            identity_records=(visible, dropped),
        ))
    )
    monkeypatch.setattr(
        source_module,
        "parse_dwr_response_result",
        AsyncMock(side_effect=[
            DWRParseResult([visible], 1, 0, False),
            DWRParseResult([], 0, 0, True),
        ]),
    )

    with pytest.raises(SourcePartialError) as exc_info:
        await collect_pages(
            lambda cursor: source.list_tag("demo", cursor, 20, "new")
        )

    assert {post.post_id for post in exc_info.value.evidence_items} == {
        "1a_2b", "1a_2c",
    }


@pytest.mark.asyncio
async def test_tag_fallback_covers_ordered_identity_records(monkeypatch):
    client = FakeClient()
    client.search_tag.return_value = "dwr"
    source = DefaultContentSource(client)
    visible = make_post("1a_2b", "2026-01-02 00:00")
    dropped = make_post("1a_2c", "2026-01-01 00:00")
    source._mobile = SimpleNamespace(
        list_tag=AsyncMock(return_value=mobile_page(
            [visible],
            dropped=1,
            complete=False,
            evidence=(dropped,),
            identity_records=(visible, dropped),
        ))
    )
    fallback = [
        make_post("1a_2b", "2026-01-02 00:00"),
        make_post("1a_2c", "2026-01-01 00:00"),
    ]
    monkeypatch.setattr(
        source_module,
        "parse_dwr_response_result",
        AsyncMock(side_effect=[
            DWRParseResult(fallback, 2, 0, False),
            DWRParseResult([], 0, 0, True),
        ]),
    )

    result = await collect_pages(
        lambda cursor: source.list_tag("demo", cursor, 20, "new")
    )

    assert result.items == fallback
    assert [post.post_id for post in result.evidence_items] == [
        "1a_2b", "1a_2c",
    ]


@pytest.mark.asyncio
async def test_zero_mapped_mobile_evidence_survives_fallback_failure():
    client = FakeClient()
    client.search_tag.side_effect = SourceHTTPError(503)
    source = DefaultContentSource(client)
    witness = make_post("1a_2c")
    mobile_error = SourcePartialError(0, 1)
    attach_source_evidence(mobile_error, (witness,))
    source._mobile = SimpleNamespace(list_tag=AsyncMock(side_effect=mobile_error))

    with pytest.raises(SourceHTTPError) as exc_info:
        await source.list_tag("demo", None, 20, "new")

    assert [post.post_id for post in exc_info.value.evidence_items] == ["1a_2c"]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 101, True, "20"])
async def test_page_limit_is_strict(limit):
    source = DefaultContentSource(FakeClient())

    from core.errors import SourceLimitError

    with pytest.raises(SourceLimitError):
        await source.list_tag("demo", None, limit, "new")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://demo.lofter.com/post/a_b",
        "https://evil.com/post/a_b",
        "https://evillofter.com/post/a_b",
        "https://user:pass@demo.lofter.com/post/a_b",
        "https://demo.lofter.com:0/post/a_b",
        "https://demo.lofter.com/post/a_b?query=1",
    ],
)
async def test_post_url_rejected_before_any_request(url):
    client = FakeClient()
    source = DefaultContentSource(client)
    with pytest.raises(SourceSchemaError):
        await source.get_post(url)
    client.get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("username", ["a.b", "demo/path", "demo?x=1", "-demo", "demo-"])
async def test_blog_username_rejected_before_any_request(username):
    client = FakeClient()
    source = DefaultContentSource(client)
    with pytest.raises(SourceSchemaError):
        await source.list_blog(username, None, 20)
    client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_post_fetch_failure_reaches_credentialed_fallback(monkeypatch):
    client = FakeClient()
    client.get.side_effect = [SourceHTTPError(503), "credentialed"]
    source = DefaultContentSource(client)
    source._mobile = SimpleNamespace(
        get_post=AsyncMock(side_effect=SourceSchemaError("mobile"))
    )
    expected = make_post("1a_2b")
    monkeypatch.setattr(source_module, "parse_post_page", AsyncMock(return_value=expected))
    result = await source.get_post("https://demo.lofter.com/post/1a_2b")
    assert result is expected
    assert [call.kwargs["credentialed"] for call in client.get.await_args_list] == [False, True]


@pytest.mark.asyncio
async def test_public_post_closing_error_propagates_without_fallback():
    client = FakeClient()
    client.get.side_effect = SourceClosingError()
    source = DefaultContentSource(client)
    source._mobile = SimpleNamespace(
        get_post=AsyncMock(side_effect=SourceSchemaError("mobile"))
    )
    with pytest.raises(SourceClosingError):
        await source.get_post("https://demo.lofter.com/post/1a_2b")
    assert client.get.await_count == 1
