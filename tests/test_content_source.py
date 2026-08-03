from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

import core.content_source as source_module
from core.content_source import DefaultContentSource
from core.dwr_parser import DWRParseResult
from core.errors import (
    SourceClosingError,
    SourceHTTPError,
    SourcePartialError,
    SourceSchemaError,
    attach_source_evidence,
)
from core.mobile_parser import MobilePage
from core.parser import Post
from core.source_scan import collect_pages


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
        sort="new",
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
    assert result.items == [expected]
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
async def test_tag_midscan_failure_restarts_dwr_from_zero(monkeypatch):
    client = FakeClient()
    client.search_tag.return_value = "dwr"
    source = DefaultContentSource(client)
    primary = make_post("primary", "2026-01-09 00:00")
    fallback = make_post("primary", "2026-01-09 00:00")
    source._mobile = SimpleNamespace(
        list_tag=AsyncMock(
            side_effect=[
                mobile_page([primary], cursor="mobile-2", exhausted=False),
                SourceSchemaError("mobile"),
            ]
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

    assert [item.post_id for item in result.items] == ["primary"]
    assert result.items[0] is fallback
    assert result.source == "dwr"
    assert client.search_tag.await_args_list == [
        call("demo", 0, 20),
        call("demo", 20, 20),
    ]


@pytest.mark.asyncio
async def test_incomplete_tag_page_restarts_dwr_and_discards_primary(monkeypatch):
    client = FakeClient()
    client.search_tag.return_value = "dwr"
    source = DefaultContentSource(client)
    primary = make_post("primary", "2026-01-09 00:00")
    fallback = make_post("primary", "2026-01-09 00:00")
    fallback_2 = make_post("mixed", "2026-01-07 00:00")
    source._mobile = SimpleNamespace(
        list_tag=AsyncMock(
            side_effect=[
                mobile_page([primary], cursor="mobile-2", exhausted=False),
                mobile_page(
                    [make_post("mixed", "2026-01-07 00:00")],
                    dropped=1,
                    complete=False,
                ),
            ]
        )
    )
    monkeypatch.setattr(
        source_module,
        "parse_dwr_response_result",
        AsyncMock(side_effect=[
            DWRParseResult([fallback, fallback_2], 2, 0, False),
            DWRParseResult([], 0, 0, True),
        ]),
    )

    result = await collect_pages(
        lambda cursor: source.list_tag("demo", cursor, 20, "new")
    )

    assert [item.post_id for item in result.items] == ["primary", "mixed"]
    assert result.items == [fallback, fallback_2]
    assert {item.post_id for item in result.evidence_items} == {"primary", "mixed"}
    assert result.source == "dwr"
    assert client.search_tag.await_args_list == [
        call("demo", 0, 20),
        call("demo", 20, 20),
    ]


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
async def test_midscan_fallback_failure_is_typed_partial():
    client = FakeClient()
    client.search_tag.side_effect = SourceSchemaError("dwr")
    source = DefaultContentSource(client)
    primary = make_post("primary")
    source._mobile = SimpleNamespace(
        list_tag=AsyncMock(
            side_effect=[
                mobile_page([primary], cursor="mobile-2", exhausted=False),
                SourceSchemaError("mobile"),
            ]
        )
    )

    from core.errors import SourcePartialError

    with pytest.raises(SourcePartialError) as exc_info:
        await collect_pages(
            lambda cursor: source.list_tag("demo", cursor, 20, "new")
        )

    assert exc_info.value.mapped_count == 1


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
