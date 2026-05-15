from dataclasses import dataclass
from urllib.parse import urlparse

from .db import LofterDB
from .parser import Post

AuthorBlockKey = tuple[str, str, str]


@dataclass
class AuthorBlock:
    session_id: str
    kind: str
    value: str
    display: str
    created_at: int = 0


def _norm(value: str) -> str:
    return value.strip().lower()


def _extract_lofter_username(raw: str) -> str:
    text = raw.strip()
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = parsed.netloc
    if host.lower().endswith(".lofter.com"):
        return host[: -len(".lofter.com")]
    return ""


def normalize_author_query(raw: str) -> list[AuthorBlockKey]:
    display = raw.strip()
    if not display:
        return []
    username = _extract_lofter_username(display)
    if username:
        return [("username", username.lower(), username)]
    value = _norm(display)
    return [("name", value, display), ("username", value, display)]


def is_author_blocked(post: Post, blocks: list[AuthorBlock]) -> bool:
    author = _norm(post.author)
    username = _norm(post.author_username)
    for block in blocks:
        if block.kind == "name" and author and author == block.value:
            return True
        if block.kind == "username" and username and username == block.value:
            return True
    return False


def filter_blocked_posts(posts: list[Post], blocks: list[AuthorBlock]) -> tuple[list[Post], list[Post]]:
    visible: list[Post] = []
    blocked: list[Post] = []
    for post in posts:
        if is_author_blocked(post, blocks):
            blocked.append(post)
        else:
            visible.append(post)
    return visible, blocked


class AuthorBlockStorage:
    def __init__(self, db: LofterDB):
        self._db = db

    async def add(self, session_id: str, raw: str) -> bool:
        added = False
        for kind, value, display in normalize_author_query(raw):
            ok = await self._db.add_author_block(session_id, kind, value, display)
            added = added or ok
        return added

    async def remove(self, session_id: str, raw: str) -> bool:
        removed = False
        for kind, value, _ in normalize_author_query(raw):
            ok = await self._db.remove_author_block(session_id, kind, value)
            removed = removed or ok
        return removed

    async def list_by_session(self, session_id: str) -> list[AuthorBlock]:
        rows = await self._db.list_author_blocks(session_id)
        return [AuthorBlock(session_id=r[0], kind=r[1], value=r[2], display=r[3], created_at=r[4]) for r in rows]
