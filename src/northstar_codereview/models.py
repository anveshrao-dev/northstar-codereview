"""Review contracts; source validation does not prove a claimed defect.

Model-produced JSON belongs in FindingProposal. ValidatedFinding is produced
only by validate_finding, using a trusted snapshot and supplied source text.
These models perform no file reads, network calls, or code execution.
"""

from collections.abc import Mapping
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Any, Generic, Literal, Self, TypeVar
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    model_validator,
)


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
PositiveInteger = Annotated[int, Field(strict=True, gt=0)]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Revision = Annotated[str, StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]


def _relative_path(value: str, *, allow_root: bool = False) -> str:
    """Check lexical scope only; a future reader must also reject symlink escape."""
    if value == "." and allow_root:
        return value
    if not value or value.startswith(("/", "~")) or "\\" in value or ":" in value:
        raise ValueError("Use a canonical project-relative path")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Paths cannot contain control characters")
    parts = value.split("/")
    if any(part in {"", ".", ".."} or part != part.strip() for part in parts):
        raise ValueError("Paths cannot contain empty, relative, or padded segments")
    for part in parts:
        lowered = part.casefold()
        if lowered in {".git", ".ssh", ".aws", ".venv", ".cache", ".runtime"}:
            raise ValueError("Path targets excluded metadata or private runtime data")
        if lowered == ".env" or (lowered.startswith(".env.") and lowered != ".env.example"):
            raise ValueError("Environment files are outside the review scope")
    return value


def _local_target(value: str) -> str:
    return _relative_path(value, allow_root=True)


FilePath = Annotated[str, AfterValidator(_relative_path)]
LocalTarget = Annotated[str, AfterValidator(_local_target)]
Repository = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}$"),
]


class Contract(BaseModel):
    """Reject unexpected fields and prevent ordinary in-place record changes."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True, hide_input_in_errors=True)


class ReviewMode(StrEnum):
    LOCAL = "local"
    GITHUB = "github"


class SourceSide(StrEnum):
    BASE = "base"
    HEAD = "head"


class AgentName(StrEnum):
    CODE_QUALITY = "code_quality"
    SECURITY = "security"
    TEST_COVERAGE = "test_coverage"
    GITOPS = "gitops"
    CICD_GROOVY = "cicd_groovy"


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Category(StrEnum):
    CORRECTNESS = "correctness"
    SECURITY = "security"
    TESTS = "tests"
    GITOPS = "gitops"
    CICD = "cicd"
    MAINTAINABILITY = "maintainability"


class ToolStatus(StrEnum):
    OK = "ok"
    NO_MATCH = "no_match"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"
    ERROR = "error"


class SpecialistStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class HumanDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    CLARIFY = "clarify"


class ReviewRequest(Contract):
    """Exactly one local target or GitHub PR, without fetching either."""

    review_id: UUID = Field(default_factory=uuid4, description="Application review identity.")
    mode: ReviewMode = Field(description="Local source or GitHub pull-request intake.")
    local_path: LocalTarget | None = Field(default=None, description="Project-relative file/directory, or '.'.")
    repository: Repository | None = Field(default=None, description="GitHub owner/repository slug, never a URL.")
    pr_number: PositiveInteger | None = Field(default=None, description="Positive GitHub pull-request number.")

    @model_validator(mode="after")
    def check_target(self) -> Self:
        if self.mode == ReviewMode.LOCAL:
            if self.local_path is None or self.repository is not None or self.pr_number is not None:
                raise ValueError("Local review requires only a local_path target")
        elif self.local_path is not None or self.repository is None or self.pr_number is None:
            raise ValueError("GitHub review requires only repository and pr_number targets")
        if self.repository is not None and self.repository.split("/")[1] in {".", ".."}:
            raise ValueError("Repository name cannot be a relative path segment")
        return self


class SourceFile(Contract):
    """A trusted manifest entry; content hashes apply to exact UTF-8 text."""

    file_path: FilePath = Field(description="Canonical path inside the source snapshot.")
    side: SourceSide = Field(default=SourceSide.HEAD, description="Base or head version of the file.")
    sha256: Digest = Field(description="SHA-256 of exact UTF-8 source bytes, preserving line endings.")


class SourceSnapshot(Contract):
    """Caller-supplied source identity; no live branch names or mutable manifests."""

    snapshot_id: UUID = Field(description="Identity assigned by the trusted snapshot collector.")
    revision: Revision = Field(description="Pinned commit hash or local manifest digest.")
    files: tuple[SourceFile, ...] = Field(min_length=1, description="Unique path/side entries with content hashes.")

    @model_validator(mode="after")
    def check_unique_files(self) -> Self:
        identities = [(item.side, item.file_path) for item in self.files]
        if len(set(identities)) != len(identities):
            raise ValueError("Snapshot contains duplicate path/side identities")
        return self


class EvidenceReference(Contract):
    """An unverified source citation; snippets retain whitespace and newlines."""

    snapshot_id: UUID = Field(description="Snapshot that must contain this evidence.")
    file_path: FilePath = Field(description="Canonical path from the snapshot manifest.")
    side: SourceSide = Field(default=SourceSide.HEAD, description="Source version used by the citation.")
    start_line: PositiveInteger = Field(description="One-based first line, inclusive.")
    end_line: PositiveInteger = Field(description="One-based last line, inclusive.")
    snippet: str = Field(min_length=1, max_length=16000, description="Exact full-line source slice, including its line endings.")

    @model_validator(mode="after")
    def check_line_order(self) -> Self:
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


Payload = TypeVar("Payload")


ToolFailureCategory = Literal["invalid_arguments", "unknown_file", "source_side_unavailable", "snapshot_mismatch",
                              "invalid_line_range", "line_count_limit", "output_size_limit",
                              "tool_wrapper_validation_failure", "internal_error", "empty_file"]


class ToolDiagnostic(Contract):
    """Closed failure vocabulary; no source, input values, paths or exception text."""

    category: ToolFailureCategory
    recovery: Literal["check_input_schema", "list_snapshot_files", "get_source_metadata",
                      "use_captured_line_bounds", "reduce_request", "stop"]
    line_count: int | None = Field(default=None, strict=True, ge=0,
                                   description="Trusted captured-file line count, never a requested argument value.")


class ToolRecovery(Contract):
    """Provenance of one bounded correction; no arguments or source content."""
    original_call_id: UUID
    original_category: Literal["invalid_line_range"] = "invalid_line_range"
    original_guidance: Literal["use_captured_line_bounds"] = "use_captured_line_bounds"
    line_count: PositiveInteger
    action: Literal["clamp_end_line"] = "clamp_end_line"
    outcome: Literal["recovered", "unresolved"]


class InspectionFailureCount(Contract):
    category: ToolFailureCategory
    recovered: int = Field(strict=True, ge=0)
    unresolved: int = Field(strict=True, ge=0)

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if self.recovered + self.unresolved == 0:
            raise ValueError("Empty inspection failure count")
        return self


class InspectionSummary(Contract):
    successful_attempts: int = Field(default=0, strict=True, ge=0)
    recovery_attempts: int = Field(default=0, strict=True, ge=0)
    recovered_errors: int = Field(default=0, strict=True, ge=0)
    unresolved_errors: int = Field(default=0, strict=True, ge=0)
    failures: tuple[InspectionFailureCount, ...] | None = Field(default=None, max_length=10,
        description="Closed categories and physical failure counts; null means category telemetry unavailable.")

    @model_validator(mode="after")
    def consistent_counts(self) -> Self:
        if self.recovered_errors > min(self.recovery_attempts, self.successful_attempts):
            raise ValueError("Inconsistent inspection recovery counts")
        if self.failures is not None:
            categories = [f.category for f in self.failures]
            if (categories != sorted(set(categories))
                    or sum(f.recovered for f in self.failures) != self.recovered_errors
                    or sum(f.unresolved for f in self.failures) != self.unresolved_errors):
                raise ValueError("Inconsistent inspection category counts")
        return self


class ToolResult(Contract, Generic[Payload]):
    """A typed observation; unsuccessful calls cannot masquerade as success."""

    call_id: UUID = Field(description="Runtime identity of this tool attempt.")
    snapshot_id: UUID = Field(description="Snapshot inspected by this tool attempt.")
    tool_name: Text = Field(max_length=100, description="Name of the allowlisted inspection tool.")
    status: ToolStatus = Field(description="Execution outcome, distinct from a review conclusion.")
    data: Payload | None = Field(default=None, description="Successful payload of the declared generic type.")
    error: Text | None = Field(default=None, description="Sanitized failure reason; never credentials or raw exceptions.")
    diagnostic: ToolDiagnostic | None = Field(default=None, description="Safe failure classification and bounded recovery guidance.")
    recovery: ToolRecovery | None = Field(default=None, description="Explicit original error and outcome of a separate physical correction attempt.")
    evidence: tuple[EvidenceReference, ...] = Field(default=(), description="Source references returned by the tool.")

    @model_validator(mode="after")
    def check_outcome(self) -> Self:
        if self.recovery is not None and ((self.recovery.outcome == "recovered") != (self.status == ToolStatus.OK)
                                          or self.recovery.original_call_id == self.call_id):
            raise ValueError("Inconsistent recovery outcome or attempt identity")
        if self.status == ToolStatus.OK:
            if self.data is None or self.error is not None or self.diagnostic is not None:
                raise ValueError("Successful tools require data and cannot contain an error or failure diagnostic")
        elif self.status == ToolStatus.NO_MATCH:
            if self.data is not None or self.error is not None:
                raise ValueError("no_match is a completed search with neither data nor an error")
        elif self.data is not None or self.error is None:
            raise ValueError("Unsuccessful tools require an error and cannot contain data")
        if any(item.snapshot_id != self.snapshot_id for item in self.evidence):
            raise ValueError("Tool evidence must belong to the tool's snapshot")
        return self


class FindingProposal(Contract):
    """Untrusted claims: no authoritative IDs, agent attribution, or validation flags."""

    title: Text = Field(max_length=200, description="Concise statement of the potential defect.")
    category: Category = Field(description="Review domain of the claim.")
    severity: Severity = Field(description="Proposed impact rating, not a probability.")
    confidence: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False, description="Uncalibrated model estimate; not evidence of correctness.")
    explanation: Text = Field(description="Trigger, claimed behavior, and impact.")
    suggested_remediation: Text = Field(description="Actionable correction or investigation.")
    assumptions: tuple[Text, ...] = Field(default=(), description="Preconditions that have not been established.")
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1, description="Source citations requiring deterministic validation.")


_VALIDATION_AUTHORITY = object()


class ValidatedFinding(Contract):
    """Evidence-checked record, created through validate_finding, never LLM JSON.

    This is an application boundary, not protection against hostile Python code.
    Do not use Pydantic's validation-bypassing construction/copy APIs on input.
    """

    finding_id: UUID = Field(description="Stable identifier assigned by application validation.")
    snapshot_id: UUID = Field(description="Trusted source identity checked by the application.")
    agent_name: AgentName = Field(description="Origin assigned by the caller, not the model.")
    proposal: FindingProposal = Field(description="Original claim, retained without upgrading its certainty.")
    evidence_status: Literal["validated"] = Field(default="validated", description="Source anchors and content passed deterministic checks.")
    claim_status: Literal["unverified"] = Field(default="unverified", description="Source validation does not prove the claimed defect.")

    @model_validator(mode="before")
    @classmethod
    def require_validation_boundary(cls, data: Any, info: ValidationInfo) -> Any:
        if not isinstance(info.context, dict) or info.context.get("authority") is not _VALIDATION_AUTHORITY:
            raise ValueError("ValidatedFinding must be produced by validate_finding")
        return data


class EvidenceValidationError(ValueError):
    """Value-free rejection label; remains compatible with ValueError callers."""

    def __init__(self, category: Literal['source_identity_mismatch', 'unsupported_evidence',
                                       'exact_source_mismatch'], message: str):
        self.category = category
        super().__init__(message)


def validate_finding(
    proposal: FindingProposal,
    snapshot: SourceSnapshot,
    sources: Mapping[tuple[SourceSide | str, str], str],
    *,
    agent_name: AgentName | str,
) -> ValidatedFinding:
    """Check citations against trusted text, without reading or executing files.

    Caller-supplied snapshots and source text must come from trusted application
    intake, not model output. Sources use (side, path) keys. LF defines physical
    lines; CRLF bytes and all other characters are preserved in exact snippets.
    """
    manifest = {(item.side, item.file_path): item.sha256 for item in snapshot.files}
    for evidence in proposal.evidence:
        if evidence.snapshot_id != snapshot.snapshot_id:
            raise EvidenceValidationError('source_identity_mismatch', "Evidence belongs to a different snapshot")
        key = (evidence.side, evidence.file_path)
        if key not in manifest or key not in sources:
            raise EvidenceValidationError('unsupported_evidence', "Evidence file is absent from the manifest or supplied sources")
        source = sources[key]
        if sha256(source.encode("utf-8")).hexdigest() != manifest[key]:
            raise EvidenceValidationError('source_identity_mismatch', "Source content differs from the snapshot digest")
        segments = source.split("\n")
        lines = [segment + "\n" for segment in segments[:-1]]
        if segments[-1]:
            lines.append(segments[-1])
        if evidence.end_line > len(lines):
            raise EvidenceValidationError('unsupported_evidence', "Evidence line range exceeds the source file")
        if "".join(lines[evidence.start_line - 1:evidence.end_line]) != evidence.snippet:
            raise EvidenceValidationError('exact_source_mismatch', "Evidence snippet does not exactly match the source slice")
    origin = AgentName(agent_name)
    identity = f"northstar_codereview:{snapshot.snapshot_id}:{snapshot.revision}:{origin.value}:{proposal.model_dump_json()}"
    return ValidatedFinding.model_validate(
        {
            "finding_id": uuid5(NAMESPACE_URL, identity),
            "snapshot_id": snapshot.snapshot_id,
            "agent_name": origin,
            "proposal": proposal,
        },
        context={"authority": _VALIDATION_AUTHORITY},
    )


SynthesisStatus = Literal["not_started", "succeeded", "insufficient_evidence", "budget_denied",
                          "malformed_output", "tool_call_rejected", "request_failed"]


class FinalResponseDiagnostics(Contract):
    """Allowlisted labels only; no provider strings, arbitrary keys or values."""

    finish_reason: Literal['stop', 'length', 'tool_calls', 'content_filter', 'function_call',
                           'unavailable', 'unrecognized']
    truncated: bool | None = Field(strict=True)
    known_top_level_keys: tuple[Literal['findings', 'outcome'], ...] | None
    unexpected_keys_present: bool | None = Field(strict=True)
    findings_present: bool | None = Field(strict=True)
    failure_stage: Literal['none', 'final_envelope']


class ContextSize(Contract):
    """Numeric projection using the unchanged serialized-byte reservation rule."""
    projected_units: int = Field(strict=True, ge=0)
    message_count: int = Field(strict=True, ge=0)
    system_units: int = Field(strict=True, ge=0)
    human_units: int = Field(strict=True, ge=0)
    assistant_units: int = Field(strict=True, ge=0)
    tool_units: int = Field(strict=True, ge=0)
    other_units: int = Field(strict=True, ge=0)
    tool_schema_units: int = Field(strict=True, ge=0)
    framing_units: int = Field(strict=True, ge=0)
    duplicate_source_payloads: int = Field(strict=True, ge=0)
    repeated_evidence_references: int = Field(strict=True, ge=0)


class SynthesisContextDiagnostics(Contract):
    before: ContextSize
    after: ContextSize
    protected_allowance: int = Field(strict=True, ge=0)
    compaction_attempted: bool = Field(strict=True)
    admission: Literal['not_attempted', 'admitted', 'denied'] = 'not_attempted'


class PreparationDiagnostics(Contract):
    mode: Literal['prepared'] = 'prepared'
    selected_files: int = Field(strict=True,ge=0)
    read_attempts: int = Field(strict=True,ge=0)
    context_units: int = Field(strict=True,ge=0,le=12000)
    proposal_count: int | None = Field(default=None,ge=0)
    rejected_proposals: int = Field(default=0,ge=0)


class AnalysisRange(Contract):
    """Snapshot-bound range telemetry. No source text or provider values."""
    snapshot_id: UUID
    file_path: FilePath
    side: SourceSide
    sha256: Digest
    start_line: int = Field(strict=True, ge=0)
    end_line: int = Field(strict=True, ge=0)
    prepared: bool = Field(default=False, strict=True)
    outcome: Literal['planned', 'analyzed', 'skipped', 'failed'] = 'planned'
    reason: Literal['oversized_line', 'unsupported_file', 'file_limit', 'capacity',
                    'inspection_failed', 'request_failed', 'invalid_output', 'invalid_evidence',
                    'insufficient_evidence', 'interrupted'] | None = None

    @model_validator(mode='after')
    def valid_range(self):
        if self.end_line < self.start_line or (self.start_line == 0) != (self.end_line == 0):
            raise ValueError('Invalid analysis range')
        if self.outcome == 'analyzed' and (not self.prepared or self.reason is not None):
            raise ValueError('Analysis requires validated preparation')
        return self

    @property
    def line_count(self):
        return self.end_line-self.start_line+1 if self.start_line else 0


class AnalysisUnitDiagnostics(Contract):
    """Closed-vocabulary response metadata, never model prose or source content."""
    unit_id: int = Field(strict=True, ge=1)
    range_indices: tuple[int, ...]
    classification: Literal['findings', 'insufficient_evidence', 'invalid_output', 'invalid_evidence',
                            'request_failed', 'rejected_before_parse']
    schema_status: Literal['valid', 'invalid', 'unavailable']
    response: FinalResponseDiagnostics | None = None
    reported_input_tokens: int | None = Field(default=None, strict=True, ge=0)
    reported_output_tokens: int | None = Field(default=None, strict=True, ge=0)
    # Current output schema has no safe reason code; never infer one from prose.
    insufficient_evidence_reason: Literal['unavailable'] = 'unavailable'


class AnalysisDiagnostics(Contract):
    version: Literal[1] = 1
    admission: Literal['admitted', 'denied']
    denial_reasons: tuple[Literal['scope', 'tool_attempts', 'model_attempts', 'input_reservation', 'protected_input', 'output_reservation'], ...] = ()
    available_input_units: int = Field(default=80000, strict=True, ge=0)
    available_output_units: int = Field(default=4000, strict=True, ge=0)
    required_tool_attempts: int = Field(strict=True, ge=0)
    available_tool_attempts: int = Field(strict=True, ge=0)
    required_model_attempts: int = Field(strict=True, ge=0)
    available_model_attempts: int = Field(strict=True, ge=0)
    projected_input_units: tuple[int, ...]
    protected_input_units: int = Field(strict=True, ge=0)
    ranges: tuple[AnalysisRange, ...]
    units: tuple[AnalysisUnitDiagnostics, ...] | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode='after')
    def disjoint(self):
        last = {}
        for item in self.ranges:
            key = (item.snapshot_id, item.side, item.file_path)
            if key in last and item.start_line <= last[key]:
                raise ValueError('Overlapping analysis ranges')
            last[key] = item.end_line
        if any(type(n) is not int or n < 0 for n in self.projected_input_units):
            raise ValueError('Invalid reservation projection')
        seen_units, seen_ranges = set(), set()
        for unit in self.units or ():
            if unit.unit_id in seen_units or unit.unit_id > self.required_model_attempts:
                raise ValueError('Invalid analysis unit identity')
            seen_units.add(unit.unit_id)
            if not unit.range_indices:
                raise ValueError('Missing unit ranges')
            for index in unit.range_indices:
                if type(index) is not int or not 0 <= index < len(self.ranges) or index in seen_ranges:
                    raise ValueError('Invalid analysis unit range')
                seen_ranges.add(index)
        return self

    @property
    def planned_lines(self):
        return sum(r.line_count for r in self.ranges)

    @property
    def prepared_lines(self):
        return sum(r.line_count for r in self.ranges if r.prepared)

    @property
    def analyzed_lines(self):
        return sum(r.line_count for r in self.ranges if r.outcome == 'analyzed')

    @property
    def failed_lines(self):
        return sum(r.line_count for r in self.ranges if r.outcome == 'failed')

    @property
    def skipped_lines(self):
        return sum(r.line_count for r in self.ranges if r.outcome == 'skipped')


class SpecialistDiagnostics(Contract):
    """Runtime telemetry, never part of the model's final envelope or evidence."""

    analysis: AnalysisDiagnostics | None = Field(default=None, exclude_if=lambda value: value is None)
    tool_categories: tuple[Literal['discovery', 'metadata', 'source_read', 'evidence', 'unknown'], ...]
    source_read_occurred: bool = Field(strict=True)
    files_with_observed_source: int = Field(strict=True, ge=0)
    fully_inspected_files: int = Field(strict=True, ge=0)
    total_files: int = Field(strict=True, ge=0)
    observed_lines: int = Field(strict=True, ge=0)
    total_lines: int = Field(strict=True, ge=0)
    final_response: FinalResponseDiagnostics | None = None
    synthesis_context: SynthesisContextDiagnostics | None = Field(default=None, exclude_if=lambda value: value is None)
    preparation: PreparationDiagnostics | None = Field(default=None, exclude_if=lambda value: value is None)


class SpecialistResult(Contract):
    """Runtime envelope for a specialist's proposals and execution limitations."""

    job_id: UUID = Field(description="Identity of the dispatched specialist task.")
    review_id: UUID = Field(description="Owning review identity.")
    snapshot_id: UUID = Field(description="Snapshot assigned by the orchestrator.")
    agent_name: AgentName = Field(description="Specialist identity assigned by the runtime.")
    synthesis: SynthesisStatus = "not_started"
    status: SpecialistStatus = Field(description="Execution coverage, not a safety verdict.")
    reviewed_files: tuple[FilePath, ...] = Field(default=(), description="Files actually inspected.")
    findings: tuple[FindingProposal, ...] = Field(default=(), description="Unverified candidate findings.")
    limitations: tuple[Text, ...] = Field(default=(), description="Reasons for missing or incomplete analysis.")
    inspection: InspectionSummary | None = Field(default=None, description="Physical inspection outcomes, or unavailable; never inferred from missing usage.")
    # Omit unavailable telemetry rather than changing historical report digests.
    diagnostics: SpecialistDiagnostics | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def check_completion(self) -> Self:
        if self.status == SpecialistStatus.COMPLETED and self.inspection is not None and (self.inspection.recovered_errors or self.inspection.unresolved_errors):
            raise ValueError("Inspection errors require partial or failed status, even after correction")
        if self.status != SpecialistStatus.COMPLETED and not self.limitations:
            raise ValueError("Incomplete or skipped work requires an explicit limitation")
        if self.status in {SpecialistStatus.FAILED, SpecialistStatus.SKIPPED} and self.findings:
            raise ValueError("Use partial status when an incomplete task retains findings")
        if any(ref.snapshot_id != self.snapshot_id for finding in self.findings for ref in finding.evidence):
            raise ValueError("Specialist findings must refer to the assigned snapshot")
        return self


class HumanReviewDecision(Contract):
    """Review disposition only; this contract never authorizes external posting."""

    review_id: UUID = Field(description="Review being adjudicated.")
    report_revision: PositiveInteger = Field(description="Exact report revision seen by the reviewer.")
    decision: HumanDecision = Field(description="Approve, reject, or request clarification.")
    comment: Text | None = Field(default=None, description="Required reason for rejection or clarification.")

    @model_validator(mode="after")
    def check_comment(self) -> Self:
        if self.decision in {HumanDecision.REJECT, HumanDecision.CLARIFY} and self.comment is None:
            raise ValueError("Rejection and clarification require a meaningful comment")
        return self
