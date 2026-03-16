import json
import os
from dataclasses import asdict, dataclass
from typing import Literal, Optional


@dataclass
class Subscription:
    session_id: str
    type: Literal["tag", "blog"]
    target: str
    last_post_id: str = ""


class SubscriptionStorage:
    def __init__(self, data_dir: str):
        self._path = os.path.join(data_dir, "subscriptions.json")
        self._subs: list[Subscription] = []
        self._load()

    def _load(self):
        if not os.path.exists(self._path):
            return
        with open(self._path, encoding="utf-8") as f:
            data = json.load(f)
        self._subs = [Subscription(**s) for s in data.get("subscriptions", [])]

    def _save(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump({"subscriptions": [asdict(s) for s in self._subs]}, f, ensure_ascii=False, indent=2)

    def add(self, session_id: str, sub_type: Literal["tag", "blog"], target: str) -> bool:
        if self.get(session_id, sub_type, target):
            return False
        self._subs.append(Subscription(session_id=session_id, type=sub_type, target=target))
        self._save()
        return True

    def remove(self, session_id: str, sub_type: Literal["tag", "blog"], target: str) -> bool:
        sub = self.get(session_id, sub_type, target)
        if not sub:
            return False
        self._subs.remove(sub)
        self._save()
        return True

    def get(self, session_id: str, sub_type: str, target: str) -> Optional[Subscription]:
        for s in self._subs:
            if s.session_id == session_id and s.type == sub_type and s.target == target:
                return s
        return None

    def list_by_session(self, session_id: str) -> list[Subscription]:
        return [s for s in self._subs if s.session_id == session_id]

    def all(self) -> list[Subscription]:
        return list(self._subs)

    def update_last_post_id(self, session_id: str, sub_type: str, target: str, post_id: str):
        sub = self.get(session_id, sub_type, target)
        if sub:
            sub.last_post_id = post_id
            self._save()
