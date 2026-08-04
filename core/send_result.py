from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PushSendResult:
    primary_outcome: str
    primary_stage: str
    primary_error_type: str | None = None
    media_outcome: str = "not_applicable"
    media_stage: str | None = None
    media_error_type: str | None = None
    primary_error_retcode: int | None = None
    media_error_retcode: int | None = None

    @property
    def accepted(self) -> bool:
        return self.primary_outcome == "accepted"

    def error(self) -> "PushSendError | None":
        if self.primary_outcome != "accepted":
            return PushSendError(
                self.primary_stage,
                self.primary_outcome,
                self.primary_error_type,
                self.primary_error_retcode,
            )
        if self.media_outcome in {"rejected", "error"}:
            return PushSendError(
                self.media_stage or "media_send",
                self.media_outcome,
                self.media_error_type,
                self.media_error_retcode,
            )
        return None


class PushSendError(Exception):
    def __init__(
        self,
        stage: str,
        outcome: str,
        error_type: str | None = None,
        retcode: int | None = None,
    ) -> None:
        self.stage = stage
        self.outcome = outcome
        self.error_type = _safe_identifier(error_type) if error_type else None
        self.retcode = retcode if type(retcode) is int else None
        args = (stage, outcome, self.error_type)
        if self.retcode is not None:
            args = (*args, self.retcode)
        super().__init__(*args)

    def __str__(self) -> str:
        parts = [self.stage, self.outcome]
        if self.error_type:
            parts.append(self.error_type)
        if self.retcode is not None:
            parts.append(f"retcode={self.retcode}")
        return ":".join(parts)


def action_failed_retcode(error: BaseException) -> int | None:
    try:
        from aiocqhttp.exceptions import ActionFailed
    except ImportError:
        return None
    if not isinstance(error, ActionFailed):
        return None
    try:
        value = error.retcode
    except (AttributeError, KeyError, TypeError):
        return None
    return value if type(value) is int else None


def exception_type(error: BaseException) -> str:
    return _safe_identifier(type(error).__name__)


def _safe_identifier(value: str) -> str:
    return value if value.isidentifier() else "Exception"
