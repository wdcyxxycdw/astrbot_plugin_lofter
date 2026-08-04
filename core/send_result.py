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

    @property
    def accepted(self) -> bool:
        return self.primary_outcome == "accepted"

    def error(self) -> "PushSendError | None":
        if self.primary_outcome != "accepted":
            return PushSendError(
                self.primary_stage,
                self.primary_outcome,
                self.primary_error_type,
            )
        if self.media_outcome in {"rejected", "error"}:
            return PushSendError(
                self.media_stage or "media_send",
                self.media_outcome,
                self.media_error_type,
            )
        return None


class PushSendError(Exception):
    def __init__(
        self,
        stage: str,
        outcome: str,
        error_type: str | None = None,
    ) -> None:
        self.stage = stage
        self.outcome = outcome
        self.error_type = _safe_identifier(error_type) if error_type else None
        super().__init__(stage, outcome, self.error_type)

    def __str__(self) -> str:
        parts = [self.stage, self.outcome]
        if self.error_type:
            parts.append(self.error_type)
        return ":".join(parts)


def exception_type(error: BaseException) -> str:
    return _safe_identifier(type(error).__name__)


def _safe_identifier(value: str) -> str:
    return value if value.isidentifier() else "Exception"
