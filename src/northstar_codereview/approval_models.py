"""Local report-review contracts; reviewer IDs are assertions, not authentication."""

import json
from hashlib import sha256
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from .models import Contract, Digest, HumanReviewDecision, PositiveInteger, Text
from .output_safety import contains_sensitive
from .reporting import ReviewReport


ReviewerId = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}$")]
Decision = Literal["approve", "reject", "request_changes"]


class ApprovalError(ValueError):
    """Only fixed diagnostic codes cross the application boundary."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(f"Local approval operation rejected ({code}).")


def content_hash(value: dict) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                             allow_nan=False).encode()).hexdigest()


class ApprovalDecision(HumanReviewDecision):
    """Explicit submission; no publication field or model-supplied authority."""
    decision: Decision
    report_hash: Digest
    reviewer_id: ReviewerId
    acknowledge_partial: bool = Field(default=False, strict=True)
    comment: Text | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def check_comment(self) -> Self:
        # Override legacy clarification/rejection comment requirement: this
        # workflow accepts optional comments for each of its three dispositions.
        if contains_sensitive(self.model_dump(mode="json")):
            raise ValueError("Sensitive decision content is not accepted")
        return self


class _DecisionBody(ApprovalDecision):
    schema_version: Literal[1] = 1
    thread_id: UUID
    recorded_at: AwareDatetime
    scope: Literal["local_report_review"] = "local_report_review"


class DecisionRecord(_DecisionBody):
    record_id: Digest

    @model_validator(mode="after")
    def check_digest(self) -> Self:
        if self.record_id != content_hash(self.model_dump(mode="json", exclude={"record_id"})):
            raise ValueError("Decision record integrity mismatch")
        return self

    @classmethod
    def create(cls, **values) -> Self:
        body = _DecisionBody(**values).model_dump(mode="json")
        return cls.model_validate({**body, "record_id": content_hash(body)})


class ApprovalAnchor(Contract):
    thread_id: UUID
    report: ReviewReport
    reviewers: tuple[ReviewerId, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def check_reviewers(self) -> Self:
        if tuple(sorted(set(self.reviewers))) != self.reviewers or contains_sensitive(self.reviewers):
            raise ValueError("Invalid reviewer policy")
        return self


class ApprovalPrompt(Contract):
    thread_id: UUID
    review_id: UUID
    report_revision: PositiveInteger
    report_hash: Digest
    report_status: Literal["completed", "partial"]
    requires_partial_acknowledgment: bool
    decisions: tuple[Decision, ...] = ("approve", "reject", "request_changes")
    scope: Literal["local_report_review"] = "local_report_review"


class ApprovalView(Contract):
    thread_id: UUID
    status: Literal["pending", "finalized", "superseded"]
    report: ReviewReport
    prompt: ApprovalPrompt | None = None
    outcome: DecisionRecord | None = None
