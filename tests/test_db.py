import pytest
import pytest_asyncio

from core.db import LofterDB


@pytest_asyncio.fixture
async def db(tmp_path):
    d = LofterDB(str(tmp_path / "test.db"))
    await d.initialize()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_config_get_set(db):
    assert await db.get_config("foo") is None
    await db.set_config("foo", "bar")
    assert await db.get_config("foo") == "bar"
    await db.set_config("foo", "baz")
    assert await db.get_config("foo") == "baz"


@pytest.mark.asyncio
async def test_add_remove_subscription(db):
    ok = await db.add_subscription("sess1", "tag", "原创")
    assert ok is True

    ok2 = await db.add_subscription("sess1", "tag", "原创")
    assert ok2 is False

    sub_id = await db.get_subscription_id("sess1", "tag", "原创")
    assert sub_id is not None

    rows = await db.list_subscriptions("sess1")
    assert len(rows) == 1

    removed = await db.remove_subscription("sess1", "tag", "原创")
    assert removed is True

    assert await db.get_subscription_id("sess1", "tag", "原创") is None


@pytest.mark.asyncio
async def test_filter_unseen_marks_seen(db):
    await db.add_subscription("sess1", "tag", "test")
    sub_id = await db.get_subscription_id("sess1", "tag", "test")

    ids = ["1", "2", "3"]
    unseen = await db.filter_unseen(sub_id, ids)
    assert set(unseen) == {"1", "2", "3"}

    await db.mark_seen(sub_id, ["1", "2"])
    unseen2 = await db.filter_unseen(sub_id, ids)
    assert unseen2 == ["3"]

    await db.mark_seen(sub_id, ["3"])
    unseen3 = await db.filter_unseen(sub_id, ids)
    assert unseen3 == []


@pytest.mark.asyncio
async def test_cold_start_no_push(db):
    """首次轮询时所有 ID 均未见过，应全部标记但不推送"""
    await db.add_subscription("sess1", "tag", "test")
    sub_id = await db.get_subscription_id("sess1", "tag", "test")

    all_ids = ["10", "20", "30"]
    unseen = await db.filter_unseen(sub_id, all_ids)
    is_cold_start = len(unseen) == len(all_ids)
    assert is_cold_start is True

    await db.mark_seen(sub_id, unseen)

    unseen_after = await db.filter_unseen(sub_id, all_ids)
    assert unseen_after == []


@pytest.mark.asyncio
async def test_filter_unsent_marks_sent(db):
    """同一 session 内跨订阅去重：mark_sent 后 filter_unsent 应过滤掉已发送的帖子"""
    await db.mark_sent("sess1", ["a", "b"])
    result = await db.filter_unsent("sess1", ["a", "b", "c"])
    assert result == ["c"]

    await db.mark_sent("sess1", ["c"])
    result2 = await db.filter_unsent("sess1", ["a", "b", "c"])
    assert result2 == []


@pytest.mark.asyncio
async def test_sent_posts_session_isolated(db):
    """不同 session 的 sent_posts 互不干扰"""
    await db.mark_sent("sess1", ["x"])
    result = await db.filter_unsent("sess2", ["x"])
    assert result == ["x"]


@pytest.mark.asyncio
async def test_cascade_delete(db):
    """删除订阅时 seen_posts 应联级删除"""
    await db.add_subscription("sess1", "tag", "test")
    sub_id = await db.get_subscription_id("sess1", "tag", "test")
    await db.mark_seen(sub_id, ["1", "2"])

    await db.remove_subscription("sess1", "tag", "test")
    new_sub = await db.get_subscription_id("sess1", "tag", "test")
    assert new_sub is None
