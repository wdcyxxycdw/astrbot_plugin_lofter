from core.errors import (
    PostEvidenceError,
    SourcePartialError,
    SourceSchemaError,
    attach_source_evidence,
)


def test_source_partial_error_keeps_legacy_constructor_and_message():
    assert str(SourcePartialError()) == "内容源结果不完整（映射 0，丢弃 0）"
    assert str(SourcePartialError(3)) == "内容源结果不完整（映射 3，丢弃 0）"

    error = SourcePartialError(3, 4)

    assert (error.mapped_count, error.dropped_count) == (3, 4)
    assert error.args == ("内容源结果不完整（映射 3，丢弃 4）",)
    assert error.reason == "source_partial"
    assert error.source == "unknown"
    assert error.restarted is None


def test_source_partial_error_sanitizes_context_and_counts():
    sentinel = "private-post-id:https://private.lofter.com/post/secret"
    error = SourcePartialError(
        True,
        -1,
        reason=sentinel,
        source=sentinel,
        restarted=1,
        page_count=True,
        unique_count=-2,
    )

    assert error.mapped_count == 0
    assert error.dropped_count == 0
    assert error.reason == "source_partial"
    assert error.source == "unknown"
    assert error.restarted is None
    assert error.page_count == 0
    assert error.unique_count == 0
    assert sentinel not in str(error)
    assert sentinel not in repr(error.args)


def test_source_partial_error_tracks_attached_evidence_dynamically():
    error = SourcePartialError(
        20,
        0,
        reason="evidence_shortfall",
        source="dwr",
        restarted=True,
        page_count=2,
        unique_count=20,
    )

    assert error.evidence_count == 0
    attach_source_evidence(error, (object(), object()))

    assert error.evidence_count == 2
    assert error.restarted is True


def test_post_evidence_error_preserves_schema_contract():
    error = PostEvidenceError("field_conflict", "summary", "post_ledger")

    assert isinstance(error, SourceSchemaError)
    assert error.location == "post.evidence"
    assert str(error) == "内容源响应结构无效（post.evidence）"
    assert error.diagnostic == "field_conflict:summary:post_ledger"


def test_post_evidence_error_drops_unknown_tokens():
    sentinel = "private-owner/private-title/private-token"
    error = PostEvidenceError(sentinel, sentinel, sentinel)

    assert error.reason == "unknown"
    assert error.field == "unknown"
    assert error.origin == "unknown"
    assert error.diagnostic == "unknown:unknown:unknown"
    assert sentinel not in str(error)
    assert sentinel not in repr(error.args)
    assert sentinel not in error.diagnostic
