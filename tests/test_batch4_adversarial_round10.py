from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

import core.parser as parser_module
from core.author_block import AuthorBlockStorage
from core.db import LofterDB
from core.errors import SourceLimitError, SourceSchemaError
from core.filter import FilterRule
from core.parser import POST_FIELDS, Post, parse_embedded_post
from core.scheduler import (
    _check_tag_session,
    _fetch_all_tag_targets,
    _poll_session,
    fetch_tag_posts,
)
from core.source_scan import SourcePage
from core.storage import Subscription
from core.tag_count import count_posts

POST_ID = "1a_2b"
POST_URL = f"https://demo.lofter.com/post/{POST_ID}"
TIME = "2026-01-01 00:00:00"


@pytest_asyncio.fixture
async def db(tmp_path):
    database = LofterDB(str(tmp_path / "batch4-round10.db"))
    await database.initialize()
    yield database
    await database.close()


def _embedded_html(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"<script>window.__initialize_data__ = {payload};</script>"


def _embedded_item(**changes) -> dict:
    item = {
        "postId": 43,
        "blogId": 26,
        "blogPageUrl": POST_URL,
        "title": "Demo",
        "dirContent": "Summary",
        "content": "Content",
        "blogInfo": {
            "blogId": 26,
            "blogNickName": "Demo",
            "blogName": "demo",
        },
    }
    item.update(changes)
    return item


@pytest.mark.parametrize(
    "changes",
    [
        {"dirContent": "First", "description": "Second"},
        {"content": "First", "postContent": "Second"},
        {
            "images": ["https://example.invalid/a.jpg"],
            "photoLinks": ["https://example.invalid/b.jpg"],
        },
        {"dirContent": "", "description": "Known"},
        {"dirContent": "x" * 301 + "a", "description": "x" * 301 + "b"},
    ],
)
def test_embedded_rejects_conflicting_aliases_in_one_mapping(changes):
    with pytest.raises(SourceSchemaError) as exc_info:
        parse_embedded_post(_embedded_html({"post": _embedded_item(**changes)}), POST_URL)

    assert exc_info.value.location == "post.evidence"


def test_embedded_aliases_accept_unknown_and_normalized_equivalence():
    item = _embedded_item(
        dirContent=None,
        description="<p>Same summary</p>",
        digest="Same summary",
        images=["https://example.invalid/a.jpg?quality=90"],
        photoLinks=["https://example.invalid/a.jpg?quality=80"],
    )

    post = parse_embedded_post(_embedded_html({"post": item}), POST_URL)

    assert post.summary == "Same summary"
    assert post.images == ["https://example.invalid/a.jpg"]
    assert {"summary", "images"} <= post.completeness


def test_embedded_validates_same_id_sibling_beyond_old_depth_limit():
    deep: dict = _embedded_item(title="Conflicting")
    for _ in range(10):
        deep = {"next": deep}
    data = {"visible": _embedded_item(), "deep": deep}

    with pytest.raises(SourceSchemaError) as exc_info:
        parse_embedded_post(_embedded_html(data), POST_URL)

    assert exc_info.value.location == "post.evidence"


def test_embedded_traversal_budget_fails_closed(monkeypatch):
    monkeypatch.setattr(parser_module, "_MAX_EMBEDDED_NODES", 5, raising=False)
    data = {"visible": _embedded_item(), "extra": [{"value": index} for index in range(8)]}

    with pytest.raises(SourceLimitError) as exc_info:
        parse_embedded_post(_embedded_html(data), POST_URL)

    assert exc_info.value.resource == "items"


def test_embedded_same_id_candidate_budget_fails_closed():
    data = {"posts": [_embedded_item() for _ in range(101)]}

    with pytest.raises(SourceLimitError) as exc_info:
        parse_embedded_post(_embedded_html(data), POST_URL)

    assert exc_info.value.resource == "items"


def _post(
    *,
    title: str = "Demo",
    summary: str = "Summary",
    content: str = "Content",
    images: list[str] | None = None,
    tags: list[str] | None = None,
    author: str = "Demo",
    username: str = "demo",
    url: str = POST_URL,
    fields: set[str] | None = None,
) -> Post:
    known = fields or set(POST_FIELDS)
    return Post(
        post_id=POST_ID,
        title=title,
        summary=summary,
        content=content,
        images=images if images is not None else ["https://example.invalid/a.jpg"],
        tags=tags if tags is not None else ["A", "B"],
        author=author,
        author_username=username,
        url=url,
        publish_time=TIME,
        source="test",
        completeness=frozenset(known),
    )


def _page(
    items: list[Post], *, evidence: tuple[Post, ...] = (), source: str = "mobile_tag"
) -> SourcePage:
    return SourcePage(
        items=items,
        source=source,
        next_cursor=None,
        exhausted=True,
        sort="new",
        mapped_count=len(items),
        dropped_count=0,
        complete=True,
        evidence_items=evidence,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "first", "second"),
    [
        ("title", "First", "Second"),
        ("summary", "First", "Second"),
        ("content", "First", "Second"),
        ("images", ["https://example.invalid/a.jpg"], ["https://example.invalid/b.jpg"]),
        ("author", "First", "Second"),
    ],
)
async def test_count_rejects_cross_scan_display_field_conflicts(
    field_name, first, second,
):
    source = AsyncMock()
    values = {"A": first, "B": second}

    async def list_tag(tag, cursor, limit, sort):
        changes = {field_name: values[tag]}
        return _page([_post(**changes)])

    source.list_tag.side_effect = list_tag
    result = await count_posts("A&B", source)

    assert result.status == "partial"
    assert result.count == 0
    assert "重复作品字段冲突" in result.warnings


@pytest.mark.asyncio
async def test_fetch_tag_posts_rejects_fields_before_cross_tag_dedup():
    source = AsyncMock()
    source.list_tag.side_effect = lambda tag, cursor, limit, sort: _page([
        _post(title=f"Title {tag}")
    ])

    with pytest.raises(SourceSchemaError) as exc_info:
        await fetch_tag_posts(["A", "B"], source)

    assert exc_info.value.location == "post.evidence"


@pytest.mark.asyncio
async def test_fetch_tag_posts_merges_compatible_later_known_fields():
    source = AsyncMock()
    first = _post(
        summary="",
        fields=set(POST_FIELDS) - {"summary"},
    )
    second = _post(summary="Known summary")
    source.list_tag.side_effect = [
        _page([first]),
        _page([second]),
    ]

    posts = await fetch_tag_posts(["A", "B"], source)

    assert len(posts) == 1
    assert posts[0].summary == "Known summary"
    assert "summary" in posts[0].completeness


@pytest.mark.asyncio
async def test_fetch_tag_posts_revalidates_restart_witness_after_detail_enrich():
    witness = _post(title="Witness")
    partial = _post(
        title="",
        summary="",
        content="",
        images=[],
        tags=[],
        author="",
        username="",
        url=f"https://lofter.com/post/{POST_ID}",
        fields={"url", "publish_time"},
    )
    detail = _post(title="Detail")
    source = AsyncMock()
    source.get_post.return_value = detail
    page = _page([partial], evidence=(witness,), source="dwr")

    with (
        patch("core.scheduler.collect_pages", return_value=page),
        pytest.raises(SourceSchemaError) as exc_info,
    ):
        await fetch_tag_posts(["A"], source)

    assert exc_info.value.location == "post.evidence"


def _sub(target: str, sub_type: str = "tag") -> Subscription:
    return Subscription(
        id=1,
        session_id="session",
        type=sub_type,
        role="subscribe",
        target=target,
    )


@pytest.mark.asyncio
async def test_tag_enriches_all_occurrences_before_db_or_dedup(db):
    await db.add_subscription("session", "tag", "A")
    await db.add_subscription("session", "tag", "B")
    await db.mark_seen_targets(
        "session", "tag", {"A": ["warmup"], "B": ["warmup"]}
    )
    await db.add_author_block("session", "name", "blocked", "Blocked")
    partial = _post(author="", fields=set(POST_FIELDS) - {"author"})
    blocked = _post(author="Blocked")
    source = AsyncMock()
    source.get_post.return_value = _post(author="Allowed")
    send = AsyncMock(return_value=True)

    async def fetch(tags, _source):
        return [partial] if tags == ["A"] else [blocked]

    with patch("core.scheduler.fetch_tag_posts", side_effect=fetch):
        await _check_tag_session(
            "session",
            [_sub("A"), _sub("B")],
            source,
            db,
            send,
            AuthorBlockStorage(db),
        )

    send.assert_not_awaited()
    assert await db.filter_unseen_targets(
        "session", "tag", {"A": [POST_ID], "B": [POST_ID]}
    ) == [POST_ID]
    assert await db.filter_unsent("session", [POST_ID]) == [POST_ID]


class _EvidenceList(list):
    def __init__(self, values=(), *, evidence=()):
        super().__init__(values)
        self.evidence_items = tuple(evidence)


@pytest.mark.asyncio
async def test_blog_session_validates_all_targets_before_first_send(db):
    await db.add_subscription("session", "blog", "alice")
    await db.add_subscription("session", "blog", "bob")
    await db.mark_seen_session("session", "blog", ["warmup"])
    events: list[str] = []

    async def fetch(sub, source):
        events.append(f"fetch:{sub.target}")
        return [_post()]

    async def send(session_id, post, header, source_types):
        events.append("send")
        return True

    with patch("core.scheduler.fetch_blog_posts", side_effect=fetch):
        await _poll_session(
            "session",
            {"tag": [], "blog": [_sub("alice", "blog"), _sub("bob", "blog")]},
            AsyncMock(),
            db,
            send,
            AuthorBlockStorage(db),
        )

    assert events[:2] == ["fetch:alice", "fetch:bob"]


@pytest.mark.asyncio
async def test_blog_cross_target_owner_conflict_has_zero_side_effects(db):
    await db.add_subscription("session", "blog", "alice")
    await db.add_subscription("session", "blog", "bob")
    await db.mark_seen_session("session", "blog", ["warmup"])
    posts = {
        "alice": _post(username="alice", url=f"https://alice.lofter.com/post/{POST_ID}"),
        "bob": _post(username="bob", url=f"https://bob.lofter.com/post/{POST_ID}"),
    }
    send = AsyncMock(return_value=True)

    async def fetch(sub, source):
        return [posts[sub.target]]

    with patch("core.scheduler.fetch_blog_posts", side_effect=fetch):
        await _poll_session(
            "session",
            {"tag": [], "blog": [_sub("alice", "blog"), _sub("bob", "blog")]},
            AsyncMock(),
            db,
            send,
            AuthorBlockStorage(db),
        )

    send.assert_not_awaited()
    assert await db.filter_unseen_session("session", "blog", [POST_ID]) == [POST_ID]
    assert await db.filter_unsent("session", [POST_ID]) == [POST_ID]


@pytest.mark.asyncio
async def test_blog_cross_target_witness_conflict_is_not_discarded(db):
    await db.add_subscription("session", "blog", "alice")
    await db.add_subscription("session", "blog", "bob")
    await db.mark_seen_session("session", "blog", ["warmup"])
    witness = _post(username="alice", url=f"https://alice.lofter.com/post/{POST_ID}")
    final = _post(username="bob", url=f"https://bob.lofter.com/post/{POST_ID}")
    send = AsyncMock(return_value=True)

    async def fetch(sub, source):
        if sub.target == "alice":
            return _EvidenceList(evidence=(witness,))
        return _EvidenceList([final])

    with patch("core.scheduler.fetch_blog_posts", side_effect=fetch):
        await _poll_session(
            "session",
            {"tag": [], "blog": [_sub("alice", "blog"), _sub("bob", "blog")]},
            AsyncMock(),
            db,
            send,
            AuthorBlockStorage(db),
        )

    send.assert_not_awaited()
    assert await db.filter_unseen_session("session", "blog", [POST_ID]) == [POST_ID]


@pytest.mark.asyncio
async def test_blog_cross_target_detail_conflict_precedes_send(db):
    await db.add_subscription("session", "blog", "alice")
    await db.add_subscription("session", "blog", "bob")
    await db.mark_seen_session("session", "blog", ["warmup"])
    raw = _post(
        title="",
        summary="",
        content="",
        images=[],
        tags=[],
        author="",
        username="",
        url=f"https://lofter.com/post/{POST_ID}",
        fields={"url", "publish_time"},
    )
    source = AsyncMock()
    source.get_post.side_effect = [
        _post(username="alice", url=f"https://alice.lofter.com/post/{POST_ID}"),
        _post(username="bob", url=f"https://bob.lofter.com/post/{POST_ID}"),
    ]
    send = AsyncMock(return_value=True)

    with patch("core.scheduler.fetch_blog_posts", side_effect=[[raw], [raw]]):
        await _poll_session(
            "session",
            {"tag": [], "blog": [_sub("alice", "blog"), _sub("bob", "blog")]},
            source,
            db,
            send,
            AuthorBlockStorage(db),
        )

    send.assert_not_awaited()
    assert await db.filter_unseen_session("session", "blog", [POST_ID]) == [POST_ID]


@pytest.mark.asyncio
async def test_blog_ordinary_target_fetch_failure_remains_isolated(db):
    await db.add_subscription("session", "blog", "alice")
    await db.add_subscription("session", "blog", "bob")
    await db.mark_seen_session("session", "blog", ["warmup"])
    send = AsyncMock(return_value=True)

    async def fetch(sub, source):
        if sub.target == "alice":
            raise RuntimeError("ordinary failure")
        return [_post()]

    with patch("core.scheduler.fetch_blog_posts", side_effect=fetch):
        await _poll_session(
            "session",
            {"tag": [], "blog": [_sub("alice", "blog"), _sub("bob", "blog")]},
            AsyncMock(),
            db,
            send,
            AuthorBlockStorage(db),
        )

    send.assert_awaited_once()
