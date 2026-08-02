import asyncio

import pytest

from core.session_gate import SessionGateRegistry


@pytest.mark.asyncio
async def test_same_session_is_serialized():
    gates = SessionGateRegistry()
    entered = asyncio.Event()
    release = asyncio.Event()
    order = []

    async def first():
        async with gates.hold("session"):
            order.append("first-enter")
            entered.set()
            await release.wait()
            order.append("first-exit")

    async def second():
        await entered.wait()
        async with gates.hold("session"):
            order.append("second-enter")

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await entered.wait()
    await asyncio.sleep(0)
    assert order == ["first-enter"]

    release.set()
    await asyncio.gather(first_task, second_task)
    assert order == ["first-enter", "first-exit", "second-enter"]


@pytest.mark.asyncio
async def test_different_sessions_can_enter_concurrently():
    gates = SessionGateRegistry()
    entered = set()
    both_entered = asyncio.Event()

    async def worker(session_id):
        async with gates.hold(session_id):
            entered.add(session_id)
            if len(entered) == 2:
                both_entered.set()
            await both_entered.wait()

    await asyncio.wait_for(
        asyncio.gather(worker("one"), worker("two")),
        timeout=1,
    )
    assert entered == {"one", "two"}


def test_lock_for_returns_shared_session_lock():
    gates = SessionGateRegistry()
    assert gates.lock_for("one") is gates.lock_for("one")
    assert gates.lock_for("one") is not gates.lock_for("two")
