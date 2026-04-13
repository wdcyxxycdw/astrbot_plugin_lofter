from dataclasses import dataclass
from typing import Literal, Optional

from .db import LofterDB


@dataclass
class Subscription:
    session_id: str
    type: Literal["tag", "blog"]
    target: str
    last_post_id: str = ""
    filter_rule: str = ""


class SubscriptionStorage:
    def __init__(self, db: LofterDB):
        self._db = db

    async def add(self, session_id: str, sub_type: Literal["tag", "blog"], target: str, filter_rule: str | None = None) -> bool:
        return await self._db.add_subscription(session_id, sub_type, target, filter_rule)

    async def remove(self, session_id: str, sub_type: Literal["tag", "blog"], target: str) -> bool:
        return await self._db.remove_subscription(session_id, sub_type, target)

    async def list_by_session(self, session_id: str) -> list[Subscription]:
        rows = await self._db.list_subscriptions(session_id)
        return [Subscription(session_id=r[1], type=r[2], target=r[3], filter_rule=r[4] or "") for r in rows]

    async def all(self) -> list[Subscription]:
        rows = await self._db.list_subscriptions()
        return [Subscription(session_id=r[1], type=r[2], target=r[3], filter_rule=r[4] or "") for r in rows]

    async def get(self, session_id: str, sub_type: str, target: str) -> Optional[Subscription]:
        rows = await self._db.list_subscriptions(session_id)
        for r in rows:
            if r[2] == sub_type and r[3] == target:
                return Subscription(session_id=r[1], type=r[2], target=r[3], filter_rule=r[4] or "")
        return None

    async def update_filter(self, session_id: str, sub_type: str, target: str, filter_rule: str | None):
        sub_id = await self._db.get_subscription_id(session_id, sub_type, target)
        if sub_id is None:
            return
        await self._db.update_filter_rule(sub_id, filter_rule)
