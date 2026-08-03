from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

import core.scheduler as scheduler_module
from core.author_block import AuthorBlockStorage
from core.content_source import DefaultContentSource
from core.db import LofterDB
from core.dwr_parser import _map_items
from core.errors import SourcePartialError, SourceSchemaError
from core.mobile_parser import parse_mobile_post_detail
from core.parser import POST_FIELDS, Post, parse_embedded_post
from core.scheduler import (
    SubscriptionScheduler,
    _merge_eligible_tag_posts,
    _poll_session,
    fetch_tag_posts,
)
from core.source_scan import SourcePage, collect_pages
from core.storage import Subscription, SubscriptionStorage
from core.tag_count import count_posts

FIXTURES = Path(__file__).parent / "fixtures" / "lofter"
TIME = "2026-01-03 00:00:00"


@pytest_asyncio.fixture
async def db(tmp_path):
    database = LofterDB(str(tmp_path / "batch4-round11.db"))
    await database.initialize()
    yield database
    await database.close()


def _post(
    post_id: str = "1a_2b",
    *,
    owner: str = "demo",
    summary: str = "Summary",
    tags: list[str] | None = None,
    publish_time: str = TIME,
    fields: set[str] | None = None,
) -> Post:
    known = set(POST_FIELDS) if fields is None else set(fields)
    host = f"{owner}.lofter.com" if owner else "lofter.com"
    return Post(
        post_id=post_id,
        title="Demo",
        summary=summary,
        content="Content",
        images=["https://example.invalid/a.jpg"],
        author=owner.title() if "author" in known and owner else "",
        author_username=owner if "author_username" in known else "",
        url=f"https://{host}/post/{post_id}",
        tags=tags if tags is not None else ["A", "B"],
        publish_time=publish_time,
        source="test",
        completeness=frozenset(known),
    )


def _page(
    items: list[Post],
    *,
    source: str = "mobile_tag",
    cursor: str | None = None,
    exhausted: bool = True,
    restarted: bool = False,
    evidence: tuple[Post, ...] = (),
) -> SourcePage:
    return SourcePage(
        items=items,
        source=source,
        next_cursor=cursor,
        exhausted=exhausted,
        sort="new",
        mapped_count=len(items),
        dropped_count=0,
        complete=True,
        restarted=restarted,
        evidence_items=evidence,
    )


def _sub(target: str, sub_type: str) -> Subscription:
    return Subscription(
        id=1,
        session_id="session",
        type=sub_type,
        role="subscribe",
        target=target,
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"dirContent": "First", "content": "Second"},
        {"dirContent": "", "content": "Second"},
        {"dirContent": {"content": "First", "text": "Second"}},
    ],
)
def test_dwr_rejects_conflicting_summary_aliases(fields):
    post = {
        "blogPageUrl": "https://demo.lofter.com/post/1a_2b",
        "title": "Demo",
        "blogInfo": {"blogName": "demo"},
        **fields,
    }

    with pytest.raises(SourceSchemaError) as exc_info:
        _map_items([{"post": post}])

    assert exc_info.value.location == "post.evidence"


@pytest.mark.parametrize("bad_alias", ["", -1, True, "not-a-number"])
def test_mobile_rejects_present_invalid_post_id_alias(bad_alias):
    payload = json.loads(
        (FIXTURES / "post_detail.json").read_text(encoding="utf-8")
    )["envelope"]
    payload = json.loads(json.dumps(payload))
    payload["response"]["posts"][0]["post"]["postId"] = bad_alias

    with pytest.raises(SourceSchemaError) as exc_info:
        parse_mobile_post_detail(payload)

    assert exc_info.value.location == "post.id"


def test_embedded_rejects_empty_and_nonempty_url_aliases():
    item = {
        "postId": 43,
        "blogId": 26,
        "blogPageUrl": "",
        "postUrl": "https://demo.lofter.com/post/1a_2b",
        "title": "Demo",
        "content": "Content",
        "blogInfo": {"blogId": 26, "blogName": "demo"},
    }
    html = (
        "<script>window.__initialize_data__ = "
        + json.dumps({"post": item})
        + ";</script>"
    )

    with pytest.raises(SourceSchemaError) as exc_info:
        parse_embedded_post(html, "https://demo.lofter.com/post/1a_2b")

    assert exc_info.value.location == "post.evidence"


@pytest.mark.asyncio
async def test_count_restart_witness_is_validation_only():
    witness = _post("1a_2b", tags=["A"])
    final = _post(
        "1a_2c", tags=["A"], publish_time="2026-01-02 00:00:00"
    )
    source = AsyncMock()
    source.list_tag.side_effect = [
        _page([witness], cursor="next", exhausted=False),
        _page(
            [final],
            source="dwr",
            restarted=True,
            evidence=(witness,),
        ),
    ]

    result = await count_posts("A", source)

    assert result.status == "partial"
    assert result.count == 1
    assert result.candidates == 1


def _mobile_witness_payload() -> dict:
    payload = json.loads(
        (FIXTURES / "tag_posts.json").read_text(encoding="utf-8")
    )["envelope"]
    dropped = json.loads(json.dumps(payload["data"]["list"][0]))
    view = dropped["postData"]["postView"]
    view["permalink"] = "https://demo.lofter.com/post/1a_2c"
    view["photoCount"] = -1
    payload["data"]["list"].append(dropped)
    return payload


def _dwr_post(
    post_id: str, *, owner: str = "demo", tags: str = "A"
) -> dict:
    url = f"https://{owner}.lofter.com/post/{post_id}"
    return {"post": {
        "blogPageUrl": url,
        "blogId": 26,
        "id": int(post_id.split("_")[1], 16),
        "title": "Demo" if post_id == "1a_2b" else "WITNESS_MUST_NOT_SEND",
        "dirContent": f"Content {post_id}",
        "tag": tags,
        "publishTime": 1710000000000,
        "firstImageUrl": "[]",
        "blogInfo": {
            "blogId": 26,
            "blogName": owner,
            "blogNickName": owner.title(),
        },
    }}


def _mobile_detail_payload(post_id: str) -> dict:
    title = "Demo" if post_id == "1a_2b" else "WITNESS_MUST_NOT_SEND"
    tags = "A" if post_id == "1a_2b" else "A, X"
    post_number = int(post_id.split("_")[1], 16)
    url = f"https://demo.lofter.com/post/{post_id}"
    return {
        "meta": {"status": 200, "msg": "demo"},
        "response": {"posts": [{
            "post": {
                "id": post_number,
                "blogId": 26,
                "title": title,
                "publishTime": 1710000000000,
                "tag": tags,
                "digest": f"Content {post_id}",
                "content": f"Content {post_id}",
                "photoLinks": [f"https://media.example.invalid/{post_id}.jpg"],
                "photoCaptions": [title],
            },
            "blogInfo": {
                "blogId": 26,
                "blogName": "demo",
                "blogNickName": "Demo",
                "homePageUrl": "https://demo.lofter.com/",
            },
            "permalink": url,
            "blogPageUrl": url,
        }]},
    }


def _dwr_callback(items: list[dict]) -> str:
    value = json.dumps(items, ensure_ascii=False)
    return f'dwr.engine._remoteHandleCallback("0", "0", {value});'


async def _run_mobile_witness_poll(db, mode: str):
    storage = SubscriptionStorage(db)
    await storage.add("session", "tag", "A")
    await storage.add("session", "tag", "X", "exclude")
    await db.mark_seen_targets("session", "tag", {"A": ["warmup"]})
    posts = [_dwr_post("1a_2b")]
    if mode != "missing_cover":
        owner = "other" if mode == "owner_conflict" else "demo"
        posts.append(_dwr_post("1a_2c", owner=owner, tags="A, X"))
    client = AsyncMock()
    tag_payload = _mobile_witness_payload()

    def request_json(_method, url, **kwargs):
        if "tagPosts.json" in url:
            return tag_payload
        form = kwargs["data"]
        post_id = f"{int(form['targetblogid']):x}_{int(form['postid']):x}"
        return _mobile_detail_payload(post_id)

    client.request_json.side_effect = request_json
    client.search_tag.side_effect = [_dwr_callback(posts), _dwr_callback([])]
    send = AsyncMock(return_value=True)
    scheduler = SubscriptionScheduler(
        storage,
        DefaultContentSource(client),
        db,
        send,
        block_storage=AuthorBlockStorage(db),
    )
    selected: list[str] = []
    errors: list[Exception] = []
    real_fetch = scheduler_module._fetch_snapshot_batches

    async def capture_fetch(*args, **kwargs):
        try:
            batches = await real_fetch(*args, **kwargs)
        except Exception as exc:
            errors.append(exc)
            raise
        selected.extend(
            post.post_id for batch in batches for post in batch.posts
        )
        return batches

    with patch(
        "core.scheduler._fetch_snapshot_batches",
        side_effect=capture_fetch,
    ) as fetch_spy:
        await scheduler._poll_all()
    return send, client, selected, errors, fetch_spy


@pytest.mark.asyncio
async def test_scheduler_mobile_identity_witness_is_validation_only(db):
    send, client, selected, errors, fetch_spy = await _run_mobile_witness_poll(
        db, "success"
    )

    assert errors == []
    assert selected == ["1a_2b", "1a_2c"]
    fetch_spy.assert_awaited_once()
    send.assert_awaited_once()
    sent_post = send.await_args.args[1]
    assert sent_post.url.endswith("/post/1a_2b")
    assert sent_post.post_id != "1a_2c"
    assert "WITNESS_MUST_NOT_SEND" not in sent_post.title
    assert await db.filter_unseen_targets(
        "session", "tag", {"A": ["1a_2b", "1a_2c"]}
    ) == []
    assert await db.filter_unsent("session", ["1a_2b", "1a_2c"]) == ["1a_2c"]
    assert client.request_json.await_count == 3
    assert client.search_tag.await_count == 2
    client.get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "error_type", "location"),
    [
        ("missing_cover", SourcePartialError, None),
        ("owner_conflict", SourceSchemaError, "post.owner"),
    ],
)
async def test_scheduler_mobile_identity_witness_guards_preflight(
    db, mode, error_type, location
):
    send, _, selected, errors, fetch_spy = await _run_mobile_witness_poll(
        db, mode
    )

    assert len(errors) == 1
    assert isinstance(errors[0], error_type)
    if location is not None:
        assert errors[0].location == location
    assert selected == []
    fetch_spy.assert_awaited_once()
    send.assert_not_awaited()
    assert await db.filter_unseen_targets(
        "session", "tag", {"A": ["1a_2b", "1a_2c"]}
    ) == ["1a_2b", "1a_2c"]
    assert await db.filter_unsent("session", ["1a_2b", "1a_2c"]) == [
        "1a_2b", "1a_2c",
    ]


@pytest.mark.asyncio
async def test_collect_pages_preserves_later_duplicate_as_evidence():
    first = _post(
        summary="",
        fields=set(POST_FIELDS) - {"summary"},
    )
    duplicate = _post(summary="Known summary")
    later = _post(
        "1a_2c", publish_time="2026-01-02 00:00:00"
    )
    pages = iter([
        _page([first], cursor="next", exhausted=False),
        _page([duplicate, later]),
    ])

    result = await collect_pages(AsyncMock(side_effect=lambda cursor: next(pages)))

    assert [post.post_id for post in result.items] == ["1a_2b", "1a_2c"]
    assert result.evidence_items == (duplicate,)


@pytest.mark.asyncio
async def test_duplicate_evidence_conflicts_after_detail_enrichment():
    first = _post(
        owner="",
        fields=set(POST_FIELDS) - {"author", "author_username"},
    )
    duplicate = _post(owner="bob")
    later = _post(
        "1a_2c", publish_time="2026-01-02 00:00:00"
    )
    source = AsyncMock()
    source.list_tag.side_effect = [
        _page([first], cursor="next", exhausted=False),
        _page([duplicate, later]),
    ]
    source.get_post.return_value = _post(owner="alice")

    with pytest.raises(SourceSchemaError) as exc_info:
        await fetch_tag_posts(["A"], source)

    assert exc_info.value.location == "post.owner"


def test_cross_target_eligible_posts_merge_compatible_fields():
    partial = _post(
        summary="",
        fields=set(POST_FIELDS) - {"summary"},
    )
    complete = _post(summary="Known summary")

    posts, sources, _ = _merge_eligible_tag_posts(
        {"A": [partial], "B": [complete]},
        {"A": ["1a_2b"], "B": ["1a_2b"]},
        False,
    )

    assert len(posts) == 1
    assert posts[0].summary == "Known summary"
    assert "summary" in posts[0].completeness
    assert sources == {"1a_2b": {"A", "B"}}


@pytest.mark.asyncio
async def test_tag_blog_conflict_precedes_first_side_effect(db):
    await db.add_subscription("session", "tag", "A")
    await db.add_subscription("session", "blog", "bob")
    await db.mark_seen_targets("session", "tag", {"A": ["warmup"]})
    await db.mark_seen_session("session", "blog", ["warmup"])
    tag_post = _post(owner="alice")
    blog_post = _post(owner="bob")
    send = AsyncMock(return_value=True)

    with (
        patch("core.scheduler.fetch_tag_posts", return_value=[tag_post]),
        patch("core.scheduler.fetch_blog_posts", return_value=[blog_post]),
    ):
        await _poll_session(
            "session",
            {
                "tag": [_sub("A", "tag")],
                "blog": [_sub("bob", "blog")],
            },
            AsyncMock(),
            db,
            send,
            AuthorBlockStorage(db),
        )

    send.assert_not_awaited()
    assert await db.filter_unseen_targets(
        "session", "tag", {"A": ["1a_2b"]}
    ) == ["1a_2b"]
    assert await db.filter_unsent("session", ["1a_2b"]) == ["1a_2b"]


@pytest.mark.asyncio
async def test_blog_author_enrichment_failure_isolates_target(db):
    await db.add_subscription("session", "blog", "alice")
    await db.add_subscription("session", "blog", "bob")
    await db.mark_seen_session("session", "blog", ["warmup"])
    await db.add_author_block("session", "name", "blocked", "Blocked")
    missing_author = set(POST_FIELDS) - {"author"}
    raw = {
        "alice": _post("1a_2b", owner="alice", fields=missing_author),
        "bob": _post("1a_2c", owner="bob", fields=missing_author),
    }
    source = AsyncMock()

    async def get_post(url):
        if "alice.lofter.com" in url:
            raise RuntimeError("ordinary detail failure")
        return _post("1a_2c", owner="bob")

    source.get_post.side_effect = get_post
    send = AsyncMock(return_value=True)

    async def fetch(sub, _source):
        return [raw[sub.target]]

    with patch("core.scheduler.fetch_blog_posts", side_effect=fetch):
        await _poll_session(
            "session",
            {
                "tag": [],
                "blog": [_sub("alice", "blog"), _sub("bob", "blog")],
            },
            source,
            db,
            send,
            AuthorBlockStorage(db),
        )

    send.assert_awaited_once()
    assert send.await_args.args[1].author_username == "bob"
    assert await db.filter_unseen_session(
        "session", "blog", ["1a_2b"]
    ) == ["1a_2b"]
