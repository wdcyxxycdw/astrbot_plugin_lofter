import pytest
import pytest_asyncio

from core.author_block import AuthorBlock, filter_blocked_posts, normalize_author_query
from core.db import LofterDB
from core.parser import Post


def make_post(**kwargs):
    defaults = dict(post_id="p1", title="t", summary="", url="https://user.lofter.com/post/p1")
    return Post(**{**defaults, **kwargs})


def test_normalize_plain_name_and_username():
    keys = normalize_author_query("TestUser")
    assert ("name", "testuser", "TestUser") in keys
    assert ("username", "testuser", "TestUser") in keys


def test_normalize_lofter_url_as_username():
    keys = normalize_author_query("https://SomeUser.lofter.com")
    assert keys == [("username", "someuser", "SomeUser")]


def test_filter_by_author_name_case_insensitive():
    post = make_post(author="作者A")
    visible, blocked = filter_blocked_posts([post], [AuthorBlock("sess", "name", "作者a", "作者A")])
    assert visible == []
    assert blocked == [post]


def test_filter_by_author_username_case_insensitive():
    post = make_post(author="作者A", author_username="SomeUser")
    visible, blocked = filter_blocked_posts([post], [AuthorBlock("sess", "username", "someuser", "SomeUser")])
    assert visible == []
    assert blocked == [post]


def test_missing_author_does_not_match():
    post = make_post(author="", author_username="")
    visible, blocked = filter_blocked_posts([post], [AuthorBlock("sess", "name", "作者A", "作者A")])
    assert visible == [post]
    assert blocked == []


@pytest_asyncio.fixture
async def db(tmp_path):
    d = LofterDB(str(tmp_path / "test.db"))
    await d.initialize()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_storage_add_remove_list(db):
    from core.author_block import AuthorBlockStorage

    storage = AuthorBlockStorage(db)
    assert await storage.add("sess1", "SomeUser") is True
    assert await storage.add("sess1", "SomeUser") is False
    rows = await storage.list_by_session("sess1")
    assert {r.kind for r in rows} == {"name", "username"}
    assert await storage.remove("sess1", "SomeUser") is True
    assert await storage.list_by_session("sess1") == []
