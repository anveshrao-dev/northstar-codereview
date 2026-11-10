"""Pure deterministic presentation of already-collected, validated findings.

No source execution, filesystem access, model call, posting, or approval. The
caller supplies the trusted matching manifest and an in-process orchestrator
result. This boundary does not authenticate imported JSON or re-run collection.
Report JSON is data, never HTML. Credential-pattern filtering is defense in depth,
not permission to publish arbitrary source or a guarantee of secret detection.
"""

import json
import re
import unicodedata
from collections import Counter
from hashlib import sha256
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from .agents.code_quality import Usage
from .models import SynthesisStatus, SpecialistDiagnostics
from .models import (AgentName, Category, Contract, Digest, EvidenceReference, FilePath, InspectionSummary,
                     PositiveInteger, Revision, Severity, SourceSide, SourceSnapshot,
                     SpecialistResult, SpecialistStatus, Text, ValidatedFinding)
from .orchestrator import OrchestrationResult, SpecialistFailure
from .output_safety import contains_sensitive, sensitive_text


_SEVERITIES = (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW)


class ReportInputError(ValueError):
    """Fixed diagnostic codes, without source, claims, or raw validation errors."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"Report input rejected ({code})")


class EvidenceLocation(Contract):
    """Location projection of validated evidence; deliberately no source snippet."""
    snapshot_id: UUID
    file_path: FilePath
    side: SourceSide
    start_line: PositiveInteger
    end_line: PositiveInteger
    source_sha256: Digest

    @model_validator(mode="after")
    def ordered_lines(self) -> Self:
        if self.end_line < self.start_line:
            raise ValueError("Invalid evidence range")
        return self


class ReportFinding(Contract):
    finding_id: UUID
    agent_name: AgentName
    title: Text = Field(max_length=200)
    category: Category
    severity: Severity
    confidence: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)
    explanation: Text
    suggested_remediation: Text
    assumptions: tuple[Text, ...] = ()
    evidence: tuple[EvidenceLocation, ...] = Field(min_length=1)
    evidence_status: Literal["validated"]
    claim_status: Literal["unverified"]


class SpecialistSummary(Contract):
    synthesis: SynthesisStatus = "not_started"
    agent_name: AgentName
    job_id: UUID
    selected: bool = Field(strict=True)
    status: SpecialistStatus
    reviewed_files: tuple[FilePath, ...]
    finding_count: int = Field(strict=True, ge=0)
    limitations: tuple[Text, ...]
    usage: Usage | None
    inspection: InspectionSummary | None = None
    diagnostics: SpecialistDiagnostics | None = Field(default=None, exclude_if=lambda value: value is None)


class ReportSafetyNotice(Contract):
    """Closed labels and trusted identities only; never the matched value/path."""
    action: Literal["finding_withheld", "text_withheld", "path_withheld"]
    field: Literal["finding_prose", "evidence_path", "reviewed_files", "specialist_limitations", "limitations"]
    agent_name: AgentName | None = None
    finding_id: UUID | None = None


class SeverityCounts(Contract):
    critical: int = Field(strict=True, ge=0)
    high: int = Field(strict=True, ge=0)
    medium: int = Field(strict=True, ge=0)
    low: int = Field(strict=True, ge=0)


class ReportNotice(Contract):
    category: Literal["overlapping_evidence"] = "overlapping_evidence"
    finding_ids: tuple[UUID, UUID]


def _anchor(ref: EvidenceLocation):
    return ref.file_path, ref.start_line, ref.end_line, ref.side.value


def _priority(finding: ReportFinding):
    return (_SEVERITIES.index(finding.severity), min(_anchor(ref) for ref in finding.evidence),
            finding.category.value, str(finding.finding_id))


def _counts(findings: tuple[ReportFinding, ...]) -> SeverityCounts:
    counts = Counter(item.severity for item in findings)
    return SeverityCounts(**{severity.value: counts[severity] for severity in _SEVERITIES})


def _notices(findings: tuple[ReportFinding, ...]) -> tuple[ReportNotice, ...]:
    overlaps = []
    for index, first in enumerate(findings):
        for second in findings[index + 1:]:
            if any(a.snapshot_id == b.snapshot_id and a.file_path == b.file_path and a.side == b.side
                   and max(a.start_line, b.start_line) <= min(a.end_line, b.end_line)
                   for a in first.evidence for b in second.evidence):
                ids = tuple(sorted((first.finding_id, second.finding_id), key=str))
                overlaps.append(ReportNotice(finding_ids=ids))
    return tuple(sorted(overlaps, key=lambda notice: tuple(map(str, notice.finding_ids))))


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


class _ReportBody(Contract):
    schema_version: Literal[4] = 4
    review_id: UUID
    snapshot_id: UUID
    snapshot_revision: Revision
    report_revision: PositiveInteger
    status: Literal["completed", "partial", "failed", "skipped"]
    specialists: tuple[SpecialistSummary, ...]
    findings: tuple[ReportFinding, ...]
    severity_counts: SeverityCounts
    limitations: tuple[Text, ...]
    failures: tuple[SpecialistFailure, ...]
    notices: tuple[ReportNotice, ...]
    safety: tuple[ReportSafetyNotice, ...] = ()

    @model_validator(mode="after")
    def consistent_report(self) -> Self:
        if contains_sensitive(self.model_dump(mode="json")):
            raise ValueError("Report contains blocked sensitive output")
        if self.safety and self.status == "completed":
            raise ValueError("Withheld report content requires incomplete report status")
        origins = {item.agent_name: item for item in self.specialists}
        if len(origins) != len(self.specialists) or len({item.job_id for item in self.specialists}) != len(self.specialists):
            raise ValueError("Duplicate specialist identity")
        if len({item.finding_id for item in self.findings}) != len(self.findings):
            raise ValueError("Duplicate finding identity")
        if self.findings != tuple(sorted(self.findings, key=_priority)) or self.severity_counts != _counts(self.findings):
            raise ValueError("Inconsistent finding order or severity counts")
        for item in self.findings:
            if item.agent_name not in origins or not origins[item.agent_name].selected:
                raise ValueError("Finding has no selected specialist")
            if any(ref.snapshot_id != self.snapshot_id for ref in item.evidence):
                raise ValueError("Finding belongs to another snapshot")
        for specialist in self.specialists:
            count = sum(item.agent_name == specialist.agent_name for item in self.findings)
            if count != specialist.finding_count:
                raise ValueError("Inconsistent specialist count")
            if specialist.status in {"failed", "skipped"} and count:
                raise ValueError("Failed/skipped specialists cannot contribute findings")
            if specialist.status != "completed" and not specialist.limitations:
                raise ValueError("Incomplete specialist requires limitations")
        for failure in self.failures:
            owner = origins.get(failure.agent_name)
            if owner is None or owner.job_id != failure.job_id or owner.status != "failed":
                raise ValueError("Failure belongs to another specialist")
        selected = [item for item in self.specialists if item.selected]
        if self.status == "completed" and (not selected or self.failures or any(item.status != "completed" for item in selected)):
            raise ValueError("Incomplete review cannot be completed")
        if self.status != "completed" and not self.limitations:
            raise ValueError("Incomplete review requires limitations")
        if self.notices != _notices(self.findings):
            raise ValueError("Inconsistent overlap notices")
        return self


class ReviewReport(_ReportBody):
    """Reproducible presentation record, not a signature or approval artifact."""
    report_id: Digest

    @model_validator(mode="after")
    def consistent_identity(self) -> Self:
        if self.report_id != _digest(self.model_dump(mode="json", exclude={"report_id"})):
            raise ValueError("Report content identity does not match")
        return self


def _finding_map(findings) -> dict[UUID, ValidatedFinding]:
    unique = {}
    for item in findings:
        if not isinstance(item, ValidatedFinding):
            raise ReportInputError("invalid_finding")
        if item.finding_id in unique and item != unique[item.finding_id]:
            raise ReportInputError("conflicting_finding_identity")
        unique[item.finding_id] = item
    return unique


def _project(item: ValidatedFinding, snapshot: SourceSnapshot) -> ReportFinding:
    if item.snapshot_id != snapshot.snapshot_id:
        raise ReportInputError("snapshot_mismatch")
    manifest = {(entry.side, entry.file_path): entry.sha256 for entry in snapshot.files}
    locations = []
    for value in item.proposal.evidence:
        ref = EvidenceReference.model_validate(value.model_dump())
        if ref.snapshot_id != snapshot.snapshot_id or (ref.side, ref.file_path) not in manifest:
            raise ReportInputError("snapshot_mismatch")
        locations.append(EvidenceLocation(**ref.model_dump(exclude={"snippet"}),
                                          source_sha256=manifest[(ref.side, ref.file_path)]))
    return ReportFinding(
        finding_id=item.finding_id, agent_name=item.agent_name,
        **item.proposal.model_dump(exclude={"evidence"}), evidence=tuple(sorted(locations, key=_anchor)),
        evidence_status=item.evidence_status, claim_status=item.claim_status,
    )


def build_report(result: OrchestrationResult, *, snapshot: SourceSnapshot, report_revision: int = 1) -> ReviewReport:
    """Project an in-process collected result; never recollect or infer new claims.

    The manifest must come from trusted intake. Its revision is supplied explicitly
    because OrchestrationResult binds snapshot ID, not manifest revision. No clock
    is read. Exact duplicate finding identities fold; identity conflicts fail
    explicitly. Distinct overlapping findings remain separate.
    """
    try:
        return _build_report(result, snapshot, report_revision)
    except ReportInputError:
        raise
    except Exception:
        raise ReportInputError("invalid_report_input") from None


def _build_report(result, snapshot, report_revision):
    if type(report_revision) is not int or report_revision < 1:
        raise ReportInputError("invalid_report_revision")
    if not isinstance(result, OrchestrationResult) or not isinstance(snapshot, SourceSnapshot):
        raise ReportInputError("invalid_report_input")
    snapshot = SourceSnapshot.model_validate(snapshot.model_dump())
    if result.snapshot_id != snapshot.snapshot_id:
        raise ReportInputError("snapshot_mismatch")
    aggregate = _finding_map(result.validated_findings)
    selected = set(result.selected_specialists)
    if len(selected) != len(result.selected_specialists):
        raise ReportInputError("specialist_identity_mismatch")
    summaries, delivered, failures = [], [], []
    manifest_paths = {entry.file_path for entry in snapshot.files}
    for execution in result.specialist_results:
        specialist = SpecialistResult.model_validate(execution.result.model_dump())
        if specialist.review_id != result.review_id or specialist.snapshot_id != snapshot.snapshot_id:
            raise ReportInputError("specialist_identity_mismatch")
        if not set(specialist.reviewed_files) <= manifest_paths:
            raise ReportInputError("snapshot_mismatch")
        unique = _finding_map(execution.validated_findings)
        if any(item.agent_name != specialist.agent_name for item in unique.values()):
            raise ReportInputError("specialist_identity_mismatch")
        proposed = {_canonical(item.model_dump(mode="json")) for item in specialist.findings}
        validated = {_canonical(item.proposal.model_dump(mode="json")) for item in unique.values()}
        if proposed != validated:
            raise ReportInputError("inconsistent_findings")
        delivered.extend(unique.values())
        if execution.failure is not None:
            failures.append(SpecialistFailure.model_validate(execution.failure.model_dump()))
        summaries.append(SpecialistSummary(
            agent_name=specialist.agent_name, job_id=specialist.job_id, selected=specialist.agent_name in selected,
            status=specialist.status, reviewed_files=tuple(sorted(set(specialist.reviewed_files))),
            finding_count=len(unique), limitations=tuple(sorted(set(specialist.limitations))),
            usage=Usage.model_validate(execution.usage.model_dump()) if execution.usage is not None else None,
            inspection=specialist.inspection,
            diagnostics=specialist.diagnostics,
            synthesis=specialist.synthesis,
        ))
    if aggregate != _finding_map(delivered):
        raise ReportInputError("inconsistent_findings")
    if not selected <= {item.agent_name for item in summaries}:
        raise ReportInputError("specialist_identity_mismatch")
    failure_key = lambda failure: (failure.agent_name.value, str(failure.job_id), failure.category)
    if sorted(failures, key=failure_key) != sorted(result.failures, key=failure_key):
        raise ReportInputError("inconsistent_failures")
    findings = tuple(sorted((_project(item, snapshot) for item in aggregate.values()), key=_priority))
    status = result.status
    limitations = set(result.limitations)
    for summary in summaries:
        limitations.update(summary.limitations)
    active = [summary for summary in summaries if summary.selected]
    if status == "completed" and (not active or failures or any(summary.status != "completed" for summary in active)):
        status = "failed" if active and all(summary.status == "failed" for summary in active) else "partial"
        limitations.add("Orchestration completion was inconsistent with specialist coverage; report completion was downgraded.")
    if status != "completed" and not limitations:
        limitations.add("Review execution or scope was incomplete; absence of findings is not a clean-review result.")

    # Validate attribution/evidence above before changing the public projection.
    # Withhold whole affected claims: rewriting individual secrets could silently
    # change a claim's meaning while retaining its evidence-validation label.
    safe_findings, safety = [], []
    for finding in findings:
        field = ("evidence_path" if any(sensitive_text(ref.file_path) for ref in finding.evidence)
                 else "finding_prose" if contains_sensitive(finding.model_dump(mode="json")) else None)
        if field:
            safety.append(ReportSafetyNotice(action="finding_withheld", field=field,
                                            agent_name=finding.agent_name, finding_id=finding.finding_id))
        else:
            safe_findings.append(finding)
    findings = tuple(safe_findings)

    def safe_texts(values, field, agent=None, *, paths=False):
        kept = []
        for value in values:
            if sensitive_text(value):
                safety.append(ReportSafetyNotice(action="path_withheld" if paths else "text_withheld", field=field, agent_name=agent))
            else:
                kept.append(value)
        if len(kept) != len(values) and not paths:
            kept.append("Sensitive text withheld by output safety policy; consult the authorized local review context.")
        return tuple(sorted(set(kept)))

    summaries = [summary.model_copy(update={
        "reviewed_files": safe_texts(summary.reviewed_files, "reviewed_files", summary.agent_name, paths=True),
        "limitations": safe_texts(summary.limitations, "specialist_limitations", summary.agent_name),
        "finding_count": sum(item.agent_name == summary.agent_name for item in findings),
    }) for summary in summaries]
    limitations = set(safe_texts(tuple(limitations), "limitations"))
    if safety:
        if status == "completed":
            status = "partial"
        limitations.add("Report content was withheld by output safety policy; visible findings and counts exclude withheld claims. This report is incomplete.")
    safety = tuple(sorted(set(safety), key=lambda note: (note.action, note.field, str(note.agent_name or ""), str(note.finding_id or ""))))
    body = _ReportBody(review_id=result.review_id, snapshot_id=snapshot.snapshot_id, snapshot_revision=snapshot.revision,
                       report_revision=report_revision, status=status,
                       specialists=tuple(sorted(summaries, key=lambda item: item.agent_name.value)), findings=findings,
                       severity_counts=_counts(findings), limitations=tuple(sorted(limitations)),
                       failures=tuple(sorted(failures, key=failure_key)), notices=_notices(findings), safety=safety)
    return ReviewReport(report_id=_digest(body.model_dump(mode="json")),
                        **{name: getattr(body, name) for name in _ReportBody.model_fields})


def _checked_report(report: ReviewReport) -> ReviewReport:
    try:
        if contains_sensitive(report.model_dump(mode="json")):
            raise ReportInputError("unsafe_report_output")
        return ReviewReport.model_validate(report.model_dump())
    except ReportInputError:
        raise
    except Exception:
        raise ReportInputError("invalid_report") from None


def render_json(report: ReviewReport) -> str:
    """Machine data only; callers must not inject it directly into HTML/script."""
    checked = _checked_report(report)
    return json.dumps(checked.model_dump(mode="json"), sort_keys=True, ensure_ascii=True, indent=2, allow_nan=False) + "\n"


def _literal(value: str) -> str:
    """Single-line CommonMark code span, immune to markup and autolinking.

    Control/format/line-separator characters are shown as escapes, preventing new
    blocks and bidi/control spoofing. A longer delimiter prevents early closing.
    These spans are never placed in tables (GFM pipes have separate rules).
    """
    text = "".join((f"\\u{ord(char):04x}" if ord(char) <= 0xffff else f"\\U{ord(char):08x}")
                   if unicodedata.category(char).startswith("C") or char in "\u2028\u2029" else char for char in value)
    delimiter = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    return f"{delimiter} {text} {delimiter}"


def render_markdown(report: ReviewReport) -> str:
    """Human projection with only static headings/tables and literal claim text."""
    report = _checked_report(report)
    lines = ["# Northstar CodeReview", "", f"Review: {report.review_id}",
             f"Snapshot: {report.snapshot_id}", f"Snapshot revision: {report.snapshot_revision}",
             f"Report revision: {report.report_revision}", f"Report identity: {report.report_id}",
             f"Overall status: {report.status}", "",
             "Validated source evidence does not prove defect correctness, vulnerability, or test adequacy.",
             "All claims remain unverified. Severity is proposed impact; confidence is an uncalibrated model estimate.", "",
             "## Severity counts", "", "| Severity | Findings |", "| --- | ---: |"]
    lines.extend(f"| {severity.value} | {getattr(report.severity_counts, severity.value)} |" for severity in _SEVERITIES)
    lines.extend(["", "## Findings", ""])
    if not report.findings:
        lines.append("No findings were produced." if report.status == "completed" else
                     "No findings were produced, but review coverage was incomplete.")
        lines.extend(["This is not a guarantee of correct, secure, or adequately tested software.", ""])
    for index, item in enumerate(report.findings, 1):
        lines.extend([f"### Finding {index}: {item.severity.value}", "", f"**Title:** {_literal(item.title)}",
                      f"Finding ID: {item.finding_id}", f"Specialist: {item.agent_name.value}; category: {item.category.value}",
                      f"Evidence: {item.evidence_status}; Claim: {item.claim_status}", f"Model confidence: {item.confidence}", "",
                      f"**Explanation:** {_literal(item.explanation)}", "",
                      f"**Recommended action:** {_literal(item.suggested_remediation)}", "", "Source locations:", ""])
        lines.extend(f"- {_literal(ref.file_path)} — {ref.side.value} lines {ref.start_line}-{ref.end_line}; source SHA-256 {ref.source_sha256}"
                     for ref in item.evidence)
        lines.extend(["", "Assumptions:", ""])
        lines.extend(f"- {_literal(assumption)}" for assumption in item.assumptions)
        if not item.assumptions:
            lines.append("- No additional assumptions were supplied; claim correctness remains unverified.")
        lines.append("")
    lines.extend(["## Specialist coverage", "", "| Specialist | Selected | Status | Findings | Model requests | Tool attempts |",
                  "| --- | --- | --- | ---: | ---: | ---: |"])
    for item in report.specialists:
        calls = str(item.usage.model_calls) if item.usage is not None else "unknown"
        tools = str(item.usage.tool_attempts) if item.usage is not None else "unknown"
        lines.append(f"| {item.agent_name.value} | {'yes' if item.selected else 'no'} | {item.status.value} | {item.finding_count} | {calls} | {tools} |")
    lines.extend(["", "Completed execution describes inspection scope, not verified claim correctness.", ""])
    if report.safety:
        lines.extend(["## Output safety", "", "Sensitive content was withheld; visible counts exclude withheld findings. Publication safety is not established.", ""])
        lines.extend(f"- {note.action}: {note.field}; specialist {note.agent_name.value if note.agent_name else 'overall'}; finding {note.finding_id or 'not applicable'}"
                     for note in report.safety)
        lines.append("")
    for item in report.specialists:
        lines.extend([f"**{item.agent_name.value}** — job {item.job_id}", "", "Inspected files:", ""])
        lines.append(f"Reserved synthesis: {item.synthesis}.")
        lines.extend(f"- {_literal(path)}" for path in item.reviewed_files)
        if not item.reviewed_files:
            lines.append("- No inspected files reported.")
        if item.inspection is not None:
            counts = item.inspection
            lines.extend(["", f"Inspection attempts: {counts.successful_attempts} successful; {counts.recovery_attempts} correction attempts; "
                          f"{counts.recovered_errors} recovered errors; {counts.unresolved_errors} unresolved errors."])
            if counts.failures is not None:
                lines.extend(f"- {item.category}: {item.recovered} recovered; {item.unresolved} unresolved."
                             for item in counts.failures)
        lines.extend(["", "Specialist limitations:", ""])
        lines.extend(f"- {_literal(note)}" for note in item.limitations)
        if not item.limitations:
            lines.append("- No additional execution limitations reported.")
        lines.append("")
    lines.extend(["## Limitations", ""])
    lines.extend(f"- {_literal(note)}" for note in report.limitations)
    if not report.limitations:
        lines.append("- No additional execution limitations reported; semantic correctness remains unverified.")
    lines.extend(["", "## Failures", ""])
    lines.extend(f"- {item.agent_name.value}: {item.category}; job {item.job_id}." for item in report.failures)
    if not report.failures:
        lines.append("- No structured specialist failures reported.")
    lines.extend(["", "## Overlap notices", ""])
    for notice in report.notices:
        lines.append(f"- Findings {notice.finding_ids[0]} and {notice.finding_ids[1]} cite overlapping source ranges. "
                     "Both were retained; semantic equivalence or conflict was not resolved.")
    if not report.notices:
        lines.append("- No overlapping source ranges among distinct findings were identified; this is not a semantic conflict check.")
    return "\n".join(lines) + "\n"
