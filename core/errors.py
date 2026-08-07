from __future__ import annotations

from typing import Iterable


IDENTITY_SCHEMA_LOCATIONS = frozenset({
    "blogInfo.blogId",
    "blogInfo.blogName",
    "dwr.post.id",
    "embedded.post.id",
    "embedded.post.owner",
    "post.evidence",
    "post.id",
    "post.owner",
    "post.url",
    "postData.postCount.blogId",
    "postData.postView.blogId",
    "post_id",
})


class SourceError(RuntimeError):
    """Base class for content-source failures with payload-free messages."""

    def __init__(self, message: str = "内容源操作失败", *, _safe: bool = False) -> None:
        super().__init__(message if _safe else "内容源操作失败")


class SourceHTTPError(SourceError):
    def __init__(self, status: int | None = None, message: str | None = None) -> None:
        self.status = status if _is_int(status) else None
        suffix = f"（HTTP {self.status}）" if self.status is not None else ""
        super().__init__(f"内容源 HTTP 请求失败{suffix}", _safe=True)


class SourceBusinessError(SourceError):
    def __init__(self, code: int | None = None) -> None:
        self.code = code if _is_int(code) else None
        suffix = f"（业务码 {self.code}）" if self.code is not None else ""
        super().__init__(f"内容源返回业务失败{suffix}", _safe=True)


class SourceSchemaError(SourceError):
    def __init__(self, location: str = "response") -> None:
        self.location = _safe_location(location)
        super().__init__(f"内容源响应结构无效（{self.location}）", _safe=True)


class PostEvidenceError(SourceSchemaError):
    def __init__(self, reason: str, field: str, origin: str) -> None:
        self.reason = _safe_post_evidence_reason(reason)
        self.field = _safe_post_evidence_field(field)
        self.origin = _safe_post_evidence_origin(origin)
        self.diagnostic = f"{self.reason}:{self.field}:{self.origin}"
        super().__init__("post.evidence")


class DWREvidenceError(SourceSchemaError):
    def __init__(self, reason: str, *fields: str) -> None:
        self.location = "post.evidence"
        safe_reason = _safe_dwr_evidence_reason(reason)
        safe_fields = _safe_dwr_evidence_fields(fields)
        if safe_reason == "evidence_conflict" or not safe_fields:
            self.reason = "evidence_conflict"
            self.fields = ()
            self.fingerprint = "evidence_conflict:unknown"
        else:
            self.reason = safe_reason
            self.fields = safe_fields
            self.fingerprint = f"{safe_reason}:{'+'.join(safe_fields)}"
        self.diagnostic = self.fingerprint
        SourceError.__init__(
            self,
            f"DWR 帖子证据冲突（post.evidence；{self.fingerprint}）",
            _safe=True,
        )


class DWRIdentityError(SourceSchemaError):
    def __init__(
        self,
        reason: str,
        *fields: str,
        value_shape: str | None = None,
    ) -> None:
        self.location = "dwr.post.id"
        safe_reason = _safe_dwr_identity_reason(reason)
        safe_fields = _safe_dwr_identity_fields(fields)
        if safe_reason == "identity_conflict" or not safe_fields:
            self.reason = "identity_conflict"
            self.fields = ()
            self.fingerprint = "identity_conflict:unknown"
        else:
            self.reason = safe_reason
            self.fields = safe_fields
            self.fingerprint = f"{safe_reason}:{'+'.join(safe_fields)}"
        self.value_shape = (
            _safe_dwr_identity_shape(value_shape)
            if self.reason == "invalid_post_url" and value_shape is not None
            else None
        )
        self.diagnostic = self.fingerprint
        if self.value_shape is not None:
            self.diagnostic = f"{self.diagnostic};shape={self.value_shape}"
        SourceError.__init__(
            self,
            f"DWR 帖子身份无效（dwr.post.id；{self.fingerprint}）",
            _safe=True,
        )


class SourceLimitError(SourceError):
    def __init__(
        self, resource: str = "resource", limit: int | None = None, message: str | None = None
    ) -> None:
        self.resource = _safe_resource(resource)
        self.limit = limit if _is_int(limit) else None
        suffix = f"，上限 {self.limit}" if self.limit is not None else ""
        super().__init__(f"内容源响应超过资源限制（{self.resource}{suffix}）", _safe=True)


class SourcePartialError(SourceError):
    def __init__(
        self,
        mapped_count: int = 0,
        dropped_count: int = 0,
        *,
        reason: str = "source_partial",
        source: str = "unknown",
        restarted: bool | None = None,
        page_count: int = 0,
        unique_count: int = 0,
    ) -> None:
        self.mapped_count = _safe_count(mapped_count)
        self.dropped_count = _safe_count(dropped_count)
        self.reason = _safe_partial_reason(reason)
        self.source = _safe_partial_source(source)
        self.restarted = restarted if isinstance(restarted, bool) else None
        self.page_count = _safe_count(page_count)
        self.unique_count = _safe_count(unique_count)
        message = f"内容源结果不完整（映射 {self.mapped_count}，丢弃 {self.dropped_count}）"
        super().__init__(message, _safe=True)

    @property
    def evidence_count(self) -> int:
        return len(tuple(getattr(self, "evidence_items", ())))


class SourceClosingError(SourceError):
    def __init__(self, message: str | None = None) -> None:
        super().__init__("内容源正在关闭，拒绝新操作", _safe=True)


class SourceTimeoutError(SourceError):
    def __init__(self, message: str | None = None) -> None:
        super().__init__("内容源请求超时", _safe=True)


class SourceRetryExhaustedError(SourceError):
    def __init__(self, attempts: int | str | None = None, message: str | None = None) -> None:
        self.attempts = _safe_count(attempts) if _is_int(attempts) else None
        suffix = f"（尝试 {self.attempts} 次）" if self.attempts is not None else ""
        super().__init__(f"内容源重试次数已耗尽{suffix}", _safe=True)


class SourceChallengeError(SourceBusinessError):
    def __init__(self) -> None:
        SourceError.__init__(self, "内容源返回登录或挑战页面", _safe=True)
        self.code = None


class DWRExecutionError(SourceError):
    def __init__(self) -> None:
        super().__init__("DWR 解析进程执行失败", _safe=True)


def attach_source_evidence(
    error: Exception, evidence_items: Iterable[object]
) -> None:
    incoming = tuple(evidence_items)
    if not incoming:
        return
    existing = tuple(getattr(error, "evidence_items", ()))
    error.evidence_items = (*existing, *incoming)


def prepend_source_evidence(
    error: Exception, evidence_items: Iterable[object]
) -> None:
    incoming = tuple(evidence_items)
    if not incoming:
        return
    existing = tuple(getattr(error, "evidence_items", ()))
    error.evidence_items = (*incoming, *existing)


def mark_limit_identity_complete(error: SourceLimitError) -> None:
    error.identity_prefix_complete = True


def limit_identity_complete(error: SourceLimitError) -> bool:
    return getattr(error, "identity_prefix_complete", False) is True


def _safe_post_evidence_reason(value: str) -> str:
    known = {
        "field_conflict",
        "canonical_url_conflict",
        "invalid_url_evidence",
        "identity_conflict",
        "alias_presence_conflict",
        "alias_value_conflict",
    }
    return value if value in known else "unknown"


def _safe_post_evidence_field(value: str) -> str:
    known = {
        "title",
        "summary",
        "content",
        "author",
        "author_username",
        "images",
        "tags",
        "publish_time",
        "url",
        "post_id",
        "owner",
    }
    return value if value in known else "unknown"


def _safe_post_evidence_origin(value: str) -> str:
    known = {
        "post_ledger",
        "scan_ledger",
        "html_metadata",
        "html_request",
        "embedded_url_aliases",
        "embedded_text_aliases",
        "embedded_image_aliases",
    }
    return value if value in known else "unknown"


def _safe_partial_reason(value: str) -> str:
    known = {
        "source_partial",
        "deadline_before_fetch",
        "fetch_timeout",
        "deadline_after_fetch",
        "page_incomplete",
        "evidence_shortfall",
        "empty_nonterminal_page",
        "source_changed",
        "sort_changed",
        "cursor_stalled",
        "cursor_repeated",
        "page_repeated",
        "no_unique_progress",
        "publish_time_missing",
        "order_regressed_within_page",
        "order_regressed_across_pages",
        "next_cursor_missing",
        "restart_repeated",
        "restart_without_prior_page",
        "restart_without_cursor",
        "restart_same_source",
        "source_schema_after_progress",
        "source_error_after_progress",
    }
    return value if value in known else "source_partial"


def _safe_partial_source(value: str) -> str:
    known = {"mobile_tag", "dwr", "mobile_blog", "html_blog"}
    return value if value in known else "unknown"


def _safe_dwr_evidence_reason(value: str) -> str:
    return value if value == "content_alias_conflict" else "evidence_conflict"


def _safe_dwr_evidence_fields(values: Iterable[str]) -> tuple[str, ...]:
    known = {
        "dirContent",
        "content",
        "dirContent.content",
        "dirContent.text",
        "content.content",
        "content.text",
    }
    return tuple(dict.fromkeys(value for value in values if value in known))


def _safe_dwr_identity_reason(value: str) -> str:
    known = {
        "invalid_identity_type",
        "invalid_post_url",
        "post_url_conflict",
        "owner_conflict",
        "blog_id_conflict",
        "post_id_conflict",
    }
    return value if value in known else "identity_conflict"


def _safe_dwr_identity_fields(values: Iterable[str]) -> tuple[str, ...]:
    known = {
        "blogPageUrl",
        "postUrl",
        "permalink",
        "blogId",
        "blogInfo.blogId",
        "postId",
        "blogInfo.blogName",
    }
    return tuple(sorted({value for value in values if value in known}))


def _safe_dwr_identity_shape(value: str) -> str:
    known = {
        "empty",
        "blank",
        "surrounding_whitespace",
        "slug",
        "relative_post_path",
        "protocol_relative",
        "http_url",
        "first_party_non_post_url",
        "first_party_post_url_with_query",
        "first_party_post_url_with_fragment",
        "first_party_post_url_invalid_authority",
        "external_https_url",
        "other_url",
        "malformed_url",
        "text",
        "unknown",
    }
    return value if value in known else "unknown"


def _safe_location(value: str) -> str:
    known = {
        "archives", "author", "blog", "blogId", "blogInfo", "blogInfo.blogId",
        "blogInfo.blogName", "blogName", "blogNickName", "code", "content", "cursor",
        "data", "dwr.body", "dwr.callback", "dwr.input", "dwr.items",
        "dwr.post.id",
        "embedded.assignment", "embedded.blogInfo", "embedded.content",
        "embedded.description", "embedded.digest", "embedded.dirContent",
        "embedded.firstImageUrl", "embedded.images", "embedded.images[]",
        "embedded.json", "embedded.permalink", "embedded.photoLinks",
        "embedded.post", "embedded.post.content", "embedded.post.id",
        "embedded.post.owner",
        "embedded.postContent", "embedded.postUrl", "embedded.root",
        "embedded.tags", "embedded.tags[]", "embedded.title", "embedded.url",
        "firstPost", "firstImageUrl", "homePageUrl", "html", "id", "isMember", "items[]", "json",
        "list", "meta", "meta.msg", "minTimeStamp", "msg", "offset", "permalink",
        "photoCaptions", "photoLinks", "post", "post.content", "post.evidence",
        "post.id", "post.images", "post.meta.description", "post.meta.keywords", "post.url",
        "postData", "postData.postCount.blogId", "postData.postView.blogId",
        "post.owner",
        "postData.postView.photoCount", "postCount", "post_id", "postView",
        "posts", "publishTime", "response", "response.firstPost", "response.isMember",
        "response.posts", "sort", "status", "tag", "tags", "title", "url",
    }
    return value if value in known else "response"


def _safe_resource(value: str) -> str:
    known = {"body", "items", "title", "url", "content", "images", "tags"}
    return value if value in known else "resource"


def _safe_count(value: int) -> int:
    return value if _is_int(value) and value >= 0 else 0


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
