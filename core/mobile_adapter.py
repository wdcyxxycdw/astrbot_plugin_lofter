from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .client import JSON_HEADERS, LofterClient
from .errors import SourceSchemaError
from .mobile_parser import (
    MobilePage,
    parse_mobile_blog_page,
    parse_mobile_post_detail,
    parse_mobile_tag_page,
)
from .parser import Post
from .post_identity import decimal_post_id

TAG_POSTS_URL = "https://api.lofter.com/newapi/tagPosts.json"
POST_DETAIL_URL = (
    "https://api.lofter.com/oldapi/post/detail.api"
    "?product=lofter-android-8.2.23"
)
BLOG_HOME_URL = "https://api.lofter.com/v2.0/blogHomePage.api"
MOBILE_HEADERS = {
    **JSON_HEADERS,
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
}


@dataclass(frozen=True)
class MobileRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    form: Mapping[str, str]


def build_tag_request(tag: str, offset: str | None) -> MobileRequest:
    return MobileRequest(
        method="POST",
        url=TAG_POSTS_URL,
        headers=MOBILE_HEADERS,
        form={
            "tag": tag,
            "offset": offset or "0",
            "type": "0",
            "recentDay": "0",
            "range": "0",
            "protectedFlag": "0",
            "postTypes": "0",
            "postYm": "",
            "firstpermalink": "",
            "style": "0",
        },
    )


def build_detail_request(blog_id: str, post_id: str) -> MobileRequest:
    return MobileRequest(
        method="POST",
        url=POST_DETAIL_URL,
        headers=MOBILE_HEADERS,
        form={"targetblogid": blog_id, "postid": post_id},
    )


def build_blog_request(
    username: str, offset: str | None, limit: int
) -> MobileRequest:
    return MobileRequest(
        method="POST",
        url=BLOG_HOME_URL,
        headers=MOBILE_HEADERS,
        form={
            "blogdomain": username,
            "offset": offset or "0",
            "limit": str(limit),
            "method": "0",
            "supportposttypes": "1,2,3,4,5,6",
            "postdigestnew": "1",
            "returnData": "1",
            "checkpwd": "1",
            "needgetpoststat": "1",
        },
    )


class MobileAdapter:
    def __init__(self, client: LofterClient):
        self._client = client

    async def get_post(self, blog_id: str, post_id: str) -> Post:
        try:
            expected = decimal_post_id(blog_id, post_id)
        except ValueError:
            raise SourceSchemaError("post_id") from None
        request = build_detail_request(blog_id, post_id)
        payload = await self._send(request)
        try:
            post = parse_mobile_post_detail(payload)
        except SourceSchemaError as exc:
            _validate_detail_evidence(exc, expected)
            raise
        if post.post_id != expected:
            raise SourceSchemaError("post_id")
        return post

    async def list_blog(
        self, username: str, offset: str | None, limit: int
    ) -> MobilePage:
        request = build_blog_request(username, offset, limit)
        payload = await self._send(request)
        return parse_mobile_blog_page(payload)

    async def list_tag(self, tag: str, offset: str | None) -> MobilePage:
        request = build_tag_request(tag, offset)
        payload = await self._send(request)
        return parse_mobile_tag_page(payload)

    async def _send(self, request: MobileRequest) -> object:
        return await self._client.request_json(
            request.method,
            request.url,
            data=request.form,
            headers=request.headers,
            credentialed=False,
        )


def _validate_detail_evidence(error: SourceSchemaError, expected: str) -> None:
    for post in getattr(error, "evidence_items", ()):
        if not isinstance(post, Post) or post.post_id != expected:
            raise SourceSchemaError("post_id") from None
