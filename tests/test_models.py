"""Synthetic contract tests: no files are fetched and no APIs are called."""

from hashlib import sha256
from uuid import UUID

import pytest
from pydantic import ValidationError

from northstar_codereview import models


REVIEW_ID = UUID("10000000-0000-0000-0000-000000000001")
SNAPSHOT_ID = UUID("20000000-0000-0000-0000-000000000002")
OTHER_ID = UUID("30000000-0000-0000-0000-000000000003")
PATH = "src/refund.py"
SOURCE = "def refund(amount):\n    return amount - 1\n"


def evidence_data(**changes):
    data = {
        "snapshot_id": SNAPSHOT_ID,
        "file_path": PATH,
        "side": "head",
        "start_line": 2,
        "end_line": 2,
        "snippet": "    return amount - 1\n",
    }
    return data | changes


def proposal_data(**changes):
    data = {
        "title": "Refund reduces the requested amount",
        "category": "correctness",
        "severity": "medium",
        "confidence": 0.8,
        "explanation": "The returned amount is one less than the requested amount.",
        "suggested_remediation": "Confirm the intended refund calculation and test its boundary.",
        "evidence": [evidence_data()],
    }
    return data | changes


def snapshot():
    return models.SourceSnapshot(
        snapshot_id=SNAPSHOT_ID,
        revision="a" * 64,
        files=[{
            "file_path": PATH,
            "side": "head",
            "sha256": sha256(SOURCE.encode("utf-8")).hexdigest(),
        }],
    )


@pytest.mark.parametrize("target", [".", "samples/backend", "src/refund.py"])
def test_local_request_accepts_project_relative_target(target):
    request = models.ReviewRequest(mode="local", local_path=target)
    assert request.local_path == target
    assert request.repository is None


def test_github_request_requires_no_network_to_validate():
    request = models.ReviewRequest(mode="github", repository="northstar/demo", pr_number=7)
    assert request.repository == "northstar/demo"
    assert request.pr_number == 7


@pytest.mark.parametrize("payload", [
    {"mode": "local"},
    {"mode": "github", "repository": "northstar/demo"},
    {"mode": "github", "pr_number": 7},
    {"mode": "local", "local_path": ".", "pr_number": 7},
    {"mode": "local", "local_path": ".", "repository": "northstar/demo"},
    {"mode": "github", "repository": "northstar/demo", "pr_number": 7, "local_path": "."},
    {"mode": "github", "repository": "https://example.test/a/b", "pr_number": 7},
    {"mode": "github", "repository": "northstar/..", "pr_number": 7},
    {"mode": "github", "repository": "northstar/demo", "pr_number": 0},
    {"mode": "github", "repository": "northstar/demo", "pr_number": True},
    {"mode": "github", "repository": "northstar/demo", "pr_number": "7"},
    {"mode": "unknown", "local_path": "."},
])
def test_review_request_rejects_invalid_or_contradictory_targets(payload):
    with pytest.raises(ValidationError):
        models.ReviewRequest(**payload)


@pytest.mark.parametrize("path", [
    "", "/etc/passwd", "../outside.py", "src/../../outside.py", "src/./a.py",
    "src//a.py", "src/", "C:/source.py", "C:\\source.py", "~/source.py",
    " src/a.py", "src/a.py ", "src/../a.py", "src/\x00a.py", "src/\na.py",
    ".git/config", ".env", ".env.production",
])
def test_requests_and_evidence_reject_unsafe_or_ambiguous_paths(path):
    with pytest.raises(ValidationError):
        models.ReviewRequest(mode="local", local_path=path)
    with pytest.raises(ValidationError):
        models.EvidenceReference(**evidence_data(file_path=path))


def test_evidence_requires_a_file_instead_of_the_project_root():
    with pytest.raises(ValidationError):
        models.EvidenceReference(**evidence_data(file_path="."))


@pytest.mark.parametrize("start,end", [(0, 1), (2, 1), (-1, 1), (True, 2), (1, "2")])
def test_evidence_rejects_invalid_line_ranges(start, end):
    with pytest.raises(ValidationError):
        models.EvidenceReference(**evidence_data(start_line=start, end_line=end))


def test_evidence_preserves_exact_whitespace():
    evidence = models.EvidenceReference(**evidence_data())
    assert evidence.snippet == "    return amount - 1\n"


@pytest.mark.parametrize("severity", ["urgent", "HIGH", "", 1])
def test_proposal_rejects_unknown_severity(severity):
    with pytest.raises(ValidationError):
        models.FindingProposal(**proposal_data(severity=severity))


@pytest.mark.parametrize("confidence", [-0.1, 1.1, float("nan"), float("inf"), True])
def test_proposal_requires_finite_bounded_confidence(confidence):
    with pytest.raises(ValidationError):
        models.FindingProposal(**proposal_data(confidence=confidence))


def test_proposal_requires_evidence():
    with pytest.raises(ValidationError):
        models.FindingProposal(**proposal_data(evidence=[]))
    payload = proposal_data()
    del payload["evidence"]
    with pytest.raises(ValidationError):
        models.FindingProposal(**payload)


@pytest.mark.parametrize("field,value", [
    ("evidence_status", "validated"), ("claim_status", "confirmed"),
    ("finding_id", str(OTHER_ID)), ("agent_name", "security"),
])
def test_model_proposals_cannot_assign_authoritative_fields(field, value):
    with pytest.raises(ValidationError):
        models.FindingProposal(**proposal_data(**{field: value}))


def test_deterministic_validation_creates_stable_evidence_checked_finding():
    proposal = models.FindingProposal(**proposal_data())
    args = (proposal, snapshot(), {("head", PATH): SOURCE})
    first = models.validate_finding(*args, agent_name="code_quality")
    second = models.validate_finding(*args, agent_name="code_quality")
    assert first.finding_id == second.finding_id
    assert first.evidence_status == "validated"
    assert first.claim_status == "unverified"
    assert first.proposal.evidence[0].snippet == "    return amount - 1\n"
    assert first.agent_name == "code_quality"


def test_untrusted_json_cannot_create_validated_finding():
    checked = models.validate_finding(
        models.FindingProposal(**proposal_data()), snapshot(),
        {("head", PATH): SOURCE}, agent_name="code_quality",
    )
    with pytest.raises(ValidationError):
        models.ValidatedFinding.model_validate_json(checked.model_dump_json())
    with pytest.raises(ValidationError):
        models.ValidatedFinding(**checked.model_dump())


@pytest.mark.parametrize("change", [
    {"snapshot_id": OTHER_ID},
    {"file_path": "src/nonexistent.py"},
    {"side": "base"},
    {"start_line": 3, "end_line": 3, "snippet": "invented"},
    {"snippet": "return amount - 1"},
])
def test_evidence_validation_rejects_fabricated_or_mismatched_anchors(change):
    proposal = models.FindingProposal(**proposal_data(evidence=[evidence_data(**change)]))
    with pytest.raises(ValueError):
        models.validate_finding(proposal, snapshot(), {("head", PATH): SOURCE}, agent_name="code_quality")


def test_evidence_validation_rejects_changed_source_even_if_snippet_still_matches():
    proposal = models.FindingProposal(**proposal_data())
    changed_source = SOURCE + "# changed after snapshot\n"
    with pytest.raises(ValueError):
        models.validate_finding(proposal, snapshot(), {("head", PATH): changed_source}, agent_name="code_quality")


def test_evidence_validation_requires_available_source():
    with pytest.raises(ValueError):
        models.validate_finding(models.FindingProposal(**proposal_data()), snapshot(), {}, agent_name="code_quality")


def test_snapshot_rejects_duplicate_file_identity():
    payload = snapshot().model_dump()
    payload["files"] = [payload["files"][0], payload["files"][0]]
    with pytest.raises(ValidationError):
        models.SourceSnapshot(**payload)


@pytest.mark.parametrize("changes", [{"revision": "mutable-branch"}, {"files": []}])
def test_snapshot_requires_revision_identity_and_manifest(changes):
    with pytest.raises(ValidationError):
        models.SourceSnapshot(**(snapshot().model_dump() | changes))


@pytest.mark.parametrize("status,data,error", [
    ("ok", {"matches": 2}, None),
    ("no_match", None, None),
    ("unavailable", None, "Analyzer not installed"),
    ("blocked", None, "Capability is disabled"),
    ("timeout", None, "Read deadline exceeded"),
    ("error", None, "Input could not be parsed"),
])
def test_tool_result_accepts_consistent_status_payloads(status, data, error):
    result = models.ToolResult[dict[str, int]](
        call_id=OTHER_ID, snapshot_id=SNAPSHOT_ID, tool_name="inspect_source",
        status=status, data=data, error=error,
    )
    assert result.status == status
    assert result.data == data


@pytest.mark.parametrize("status,data,error", [
    ("invented", None, None), ("ok", None, None),
    ("ok", {"matches": 2}, "Failed"), ("error", {"matches": 2}, "Failed"),
    ("timeout", None, None), ("no_match", {"matches": 2}, None),
    ("no_match", None, "Failed"), ("blocked", None, " "),
])
def test_tool_result_rejects_contradictory_or_unknown_statuses(status, data, error):
    with pytest.raises(ValidationError):
        models.ToolResult[dict[str, int]](
            call_id=OTHER_ID, snapshot_id=SNAPSHOT_ID, tool_name="inspect_source",
            status=status, data=data, error=error,
        )


def test_tool_result_rejects_evidence_from_another_snapshot():
    with pytest.raises(ValidationError):
        models.ToolResult[str](
            call_id=OTHER_ID, snapshot_id=SNAPSHOT_ID, tool_name="read_source",
            status="ok", data="synthetic", evidence=[evidence_data(snapshot_id=OTHER_ID)],
        )


def test_specialist_result_preserves_execution_limitations():
    result = models.SpecialistResult(
        job_id=OTHER_ID, review_id=REVIEW_ID, snapshot_id=SNAPSHOT_ID,
        agent_name="code_quality", status="partial", reviewed_files=[PATH],
        findings=[proposal_data()], limitations=["Caller analysis unavailable"],
    )
    assert result.status == "partial"
    assert result.limitations == ("Caller analysis unavailable",)


@pytest.mark.parametrize("changes", [
    {"status": "partial", "limitations": []},
    {"status": "failed", "limitations": ["Failed"], "findings": [proposal_data()]},
    {"status": "skipped", "limitations": []},
    {"snapshot_id": OTHER_ID, "findings": [proposal_data()]},
    {"agent_name": "unbounded_supervisor"},
])
def test_specialist_result_rejects_inconsistent_outcomes(changes):
    payload = dict(
        job_id=OTHER_ID, review_id=REVIEW_ID, snapshot_id=SNAPSHOT_ID,
        agent_name="code_quality", status="completed", reviewed_files=[PATH],
    ) | changes
    with pytest.raises(ValidationError):
        models.SpecialistResult(**payload)


@pytest.mark.parametrize("decision,comment", [
    ("approve", None), ("reject", "Evidence does not establish the impact"),
    ("clarify", "Which caller passes a negative amount?"),
])
def test_human_decision_is_bound_to_review_revision(decision, comment):
    result = models.HumanReviewDecision(
        review_id=REVIEW_ID, report_revision=2, decision=decision, comment=comment,
    )
    assert result.review_id == REVIEW_ID
    assert result.report_revision == 2
    assert result.decision == decision


@pytest.mark.parametrize("changes", [
    {"decision": "post"}, {"decision": "merge"},
    {"decision": "clarify", "comment": None}, {"decision": "clarify", "comment": " "},
    {"decision": "reject", "comment": None}, {"report_revision": 0},
    {"report_revision": True}, {"approved_for_posting": True},
])
def test_human_decision_rejects_invalid_actions_or_missing_context(changes):
    payload = dict(review_id=REVIEW_ID, report_revision=1, decision="approve") | changes
    with pytest.raises(ValidationError):
        models.HumanReviewDecision(**payload)
