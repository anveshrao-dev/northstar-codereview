"""Explicit local operator assertions, not credentials or publication grants.

Receipt creation requires a trusted human-facing caller. These types are not a
cryptographic authority against code running as the same operating-system user.
"""

import json
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, AwareDatetime, BeforeValidator, AfterValidator, model_validator

from .models import AgentName, Contract, Digest, SourceFile, SourceSide, SourceSnapshot


def _scope_sequence(value):
    if (type(value) not in (tuple, list) or not 1 <= len(value) <= len(AgentName)
            or any(type(item) not in (str, AgentName) for item in value)):
        raise ValueError("specialist_scope")
    return value


def _unique_scope(value):
    if len(set(value)) != len(value):
        raise ValueError("specialist_scope")
    return value


SpecialistScope = Annotated[tuple[AgentName, ...], BeforeValidator(_scope_sequence), AfterValidator(_unique_scope)]


class SampleId(StrEnum):
    PORTFOLIO_RISK = "portfolio_risk"
    CSV_IMPORT = "csv_import"
    BM25_RETRIEVAL = "bm25_retrieval"


class SourceProject(StrEnum):
    PORTFOLIO = "stock_portfolio_analyzer"
    ATLAS = "northstar_atlas"


SAMPLES = MappingProxyType({
    SampleId.PORTFOLIO_RISK: (SourceProject.PORTFOLIO, (
        "src/stock_portfolio_analyzer/risk.py", "tests/test_risk.py")),
    SampleId.CSV_IMPORT: (SourceProject.PORTFOLIO, (
        "src/stock_portfolio_analyzer/csv_import.py", "src/stock_portfolio_analyzer/models.py",
        "src/stock_portfolio_analyzer/validation.py", "tests/test_imports.py")),
    SampleId.BM25_RETRIEVAL: (SourceProject.ATLAS, (
        "src/northstar_atlas/vector_store/bm25.py", "src/northstar_atlas/vector_store/catalog.py",
        "src/northstar_atlas/vector_store/contracts.py", "src/northstar_atlas/vector_store/records.py",
        "tests/test_bm25.py")),
})

MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"


def snapshot_from_hashes(sample: SampleId, hashes: dict[str, str]) -> SourceSnapshot:
    """No filesystem access; match the existing immutable snapshot identity."""
    sample = SampleId(sample)
    if set(hashes) != set(SAMPLES[sample][1]):
        raise ValueError("sample_allowlist_mismatch")
    entries = tuple(SourceFile(file_path=p, sha256=hashes[p]) for p in sorted(hashes))
    canonical = json.dumps([f.model_dump(mode="json") for f in entries], sort_keys=True, separators=(",", ":"))
    revision = sha256(b"northstar-source-manifest-v1\n" + canonical.encode()).hexdigest()
    return SourceSnapshot(snapshot_id=uuid5(NAMESPACE_URL, "northstar-source-manifest-v1:" + revision),
                          revision=revision, files=entries)


class ApprovalState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    REVOKED = "revoked"


class IntakeManifest(Contract):
    version: Literal[1] = 1
    sample: SampleId
    project: SourceProject
    provenance: Literal["user_application"] = "user_application"
    purpose: Literal["code_review_evaluation"] = "code_review_evaluation"
    retention: Literal["local_until_expiry_then_manual_delete"] = "local_until_expiry_then_manual_delete"
    retain_until: AwareDatetime
    snapshot: SourceSnapshot

    @model_validator(mode="after")
    def exact_scope(self):
        if self.project != SAMPLES[self.sample][0] or any(f.side != SourceSide.HEAD for f in self.snapshot.files):
            raise ValueError("sample_scope_mismatch")
        expected = snapshot_from_hashes(self.sample, {f.file_path: f.sha256 for f in self.snapshot.files})
        if self.snapshot != expected:
            raise ValueError("snapshot_identity_mismatch")
        return self

    @property
    def digest(self) -> str:
        return sha256(self.model_dump_json().encode()).hexdigest()


class ImportApproval(Contract):
    approval_id: UUID
    manifest: IntakeManifest
    state: ApprovalState
    approved_at: AwareDatetime
    expires_at: AwareDatetime
    privacy_reviewed: Literal[True]

    @model_validator(mode="after")
    def interval(self):
        if not self.approved_at < self.expires_at <= self.manifest.retain_until:
            raise ValueError("approval_interval")
        return self


class RealSourceContext(Contract):
    kind: Literal["real_application"] = "real_application"
    import_approval: ImportApproval


from .execution_profiles import ExecutionProfile


class TransmissionApproval(Contract):
    execution_profile: ExecutionProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    approval_id: UUID
    import_approval_id: UUID
    manifest_digest: Digest
    snapshot: SourceSnapshot
    provider: Literal["nebius"]
    model: Literal["Qwen/Qwen3-30B-A3B-Instruct-2507"]
    specialists: SpecialistScope
    purpose: Literal["code_review_evaluation"] = "code_review_evaluation"
    state: ApprovalState
    approved_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def interval(self):
        if self.execution_profile is not None and self.execution_profile.name!='standard' and self.specialists!=(AgentName.CODE_QUALITY,):
            raise ValueError('Extended profiles require Code Quality only')
        if not self.approved_at < self.expires_at:
            raise ValueError("approval_interval")
        return self


class PublicationApproval(Contract):
    """Reserved independent assertion; no importer/tool/provider consumes it."""
    approval_id: UUID
    manifest_digest: Digest
    state: ApprovalState
    rights_confirmed: Literal[True]


class ImportResult(Contract):
    status: Literal["committed"] = "committed"
    approval_id: UUID
    manifest_digest: Digest
    snapshot: SourceSnapshot


class FailureCategory(StrEnum):
    AUTHORIZATION = "authorization"
    TRANSMISSION_CONSUMED = "transmission_consumed"
    SCOPE = "sample_scope"
    INTEGRITY = "source_integrity"
    UNSAFE_FILE = "unsafe_file"
    BINARY = "binary_content"
    SIZE = "size_limit"
    CREDENTIAL = "credential_indicator"
    PRIVATE_KEY = "private_key_indicator"
    PERSONAL = "personal_data_indicator"
    WORKSTATION = "workstation_path_indicator"
    URL = "external_url_indicator"
    STORAGE = "storage_failure"
    CONFLICT = "conflicting_import"


class IntakeRejected(ValueError):
    def __init__(self, category: FailureCategory):
        self.category = FailureCategory(category)
        super().__init__(f"Real-source operation rejected ({self.category.value}).")


def checked_import(approval: ImportApproval | None, *, importing: bool = False) -> ImportApproval:
    """Revalidate even constructed/copied Pydantic instances at trust boundaries."""
    try:
        value = ImportApproval.model_validate(approval.model_dump(warnings=False))
        now = datetime.now(timezone.utc)
        if (value.state != ApprovalState.APPROVED or now < value.approved_at
                or now >= value.manifest.retain_until or (importing and now >= value.expires_at)):
            raise ValueError("inactive")
        return value
    except (ValueError, TypeError, AttributeError):
        raise IntakeRejected(FailureCategory.AUTHORIZATION) from None
