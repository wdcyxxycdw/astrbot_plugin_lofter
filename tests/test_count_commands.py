import pytest

from core.count_commands import LofterCountCommandsMixin, _format_count_list


class CountCommandEvent:
    def __init__(self, message_str: str, is_admin=True):
        self.message_str = message_str
        self._is_admin = is_admin

    def is_admin(self):
        return self._is_admin

    def plain_result(self, text: str):
        return text


class CountCommandDB:
    def __init__(self):
        self.rows = [("米哈游", "原神"), ("方舟", "明日方舟")]

    async def delete_count_condition(self, name: str) -> bool:
        for row in list(self.rows):
            if row[0] == name:
                self.rows.remove(row)
                return True
        return False

    async def list_count_conditions(self):
        return list(self.rows)


class CountCommandRunner(LofterCountCommandsMixin):
    def __init__(self, db):
        self._db = db

    @staticmethod
    def _cmd_arg(message_str: str) -> str:
        parts = message_str.strip().split(None, 2)
        return parts[2] if len(parts) > 2 else ""


def test_count_list_includes_delete_hint():
    text = _format_count_list([("米哈游", "原神"), ("方舟", "明日方舟")])

    assert "1. 米哈游 = 原神" in text
    assert "用 /lofter count-del <名称或编号> 删除统计条件" in text


@pytest.mark.asyncio
async def test_handle_count_list_is_public():
    db = CountCommandDB()
    runner = CountCommandRunner(db)

    results = [
        item async for item in runner.handle_count_list(
            CountCommandEvent("/lofter count-list", is_admin=False)
        )
    ]

    assert results == [_format_count_list(db.rows)]


@pytest.mark.asyncio
async def test_handle_count_del_deletes_by_exact_name_first():
    db = CountCommandDB()
    db.rows = [("1", "数字名称"), ("米哈游", "原神")]
    runner = CountCommandRunner(db)

    results = [item async for item in runner.handle_count_del(CountCommandEvent("/lofter count-del 1"))]

    assert results == ["已删除统计条件「1」"]
    assert db.rows == [("米哈游", "原神")]


@pytest.mark.asyncio
async def test_handle_count_del_deletes_by_list_index_when_name_missing():
    db = CountCommandDB()
    runner = CountCommandRunner(db)

    results = [item async for item in runner.handle_count_del(CountCommandEvent("/lofter count-del 2"))]

    assert results == ["已删除第 2 条统计条件「方舟」"]
    assert db.rows == [("米哈游", "原神")]


@pytest.mark.asyncio
async def test_handle_count_del_reports_index_out_of_range():
    db = CountCommandDB()
    runner = CountCommandRunner(db)

    results = [item async for item in runner.handle_count_del(CountCommandEvent("/lofter count-del 3"))]

    assert results == ["编号 3 超出范围（当前共 2 条统计条件）"]
    assert db.rows == [("米哈游", "原神"), ("方舟", "明日方舟")]
