from dataclasses import dataclass
from typing import Literal, Optional

from .db import LofterDB


@dataclass
class Subscription:
    id: int
    session_id: str
    type: Literal["tag", "blog"]
    role: Literal["subscribe", "exclude"]
    target: str
    created_at: int = 0
    state: Literal["warming", "active"] = "active"
    revision: int = 1
    initialized_at: int | None = None


class SubscriptionStorage:
    def __init__(self, db: LofterDB):
        self._db = db

    async def add(self, session_id: str, sub_type: Literal["tag", "blog"], target: str, role: str = "subscribe") -> bool:
        return await self._db.add_subscription(session_id, sub_type, target, role)

    async def remove(self, session_id: str, sub_type: Literal["tag", "blog"], target: str, role: str = "subscribe") -> bool:
        return await self._db.remove_subscription(session_id, sub_type, target, role)

    async def remove_by_id(self, sub_id: int) -> bool:
        return await self._db.remove_subscription_by_id(sub_id)

    async def list_by_session(self, session_id: str) -> list[Subscription]:
        rows = await self._db.list_subscriptions(session_id)
        return [_row_to_sub(r) for r in rows]

    async def all(self) -> list[Subscription]:
        rows = await self._db.list_subscriptions()
        return [_row_to_sub(r) for r in rows]

    async def get(self, session_id: str, sub_type: str, target: str, role: str = "subscribe") -> Optional[Subscription]:
        rows = await self._db.list_subscriptions(session_id)
        for r in rows:
            if r[2] == sub_type and r[3] == role and r[4] == target:
                return _row_to_sub(r)
        return None


def _row_to_sub(r: tuple) -> Subscription:
    return Subscription(
        id=r[0],
        session_id=r[1],
        type=r[2],
        role=r[3],
        target=r[4],
        created_at=r[5],
        state=r[6] if len(r) > 6 else "active",
        revision=r[7] if len(r) > 7 else 1,
        initialized_at=r[8] if len(r) > 8 else None,
    )
