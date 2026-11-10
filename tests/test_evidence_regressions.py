"""Previously identified exact-evidence edge cases, without changing contracts."""

from hashlib import sha256
from uuid import UUID

import pytest

from northstar_codereview.models import EvidenceReference, FindingProposal, SourceSnapshot, validate_finding


IDENTITY = UUID("20000000-0000-0000-0000-000000000002")


def check_source(text, snippet, start, end, side="head", extra=()):
    snapshot = SourceSnapshot(snapshot_id=IDENTITY, revision="a" * 64, files=[{
        "file_path": "sample.py", "side": side, "sha256": sha256(text.encode()).hexdigest(),
    }])
    ref = EvidenceReference(snapshot_id=IDENTITY, file_path="sample.py", side=side, start_line=start, end_line=end, snippet=snippet)
    proposal = FindingProposal(title="Synthetic claim", category="correctness", severity="low", confidence=0.5,
        explanation="Synthetic explanation", suggested_remediation="Inspect the behavior", evidence=(ref, *extra))
    return validate_finding(proposal, snapshot, {(side, "sample.py"): text}, agent_name="code_quality")


@pytest.mark.parametrize("text,snippet,start,end", [
    ("first\nsecond\nthird\n", "first\nsecond\n", 1, 2),
    ("first\r\nsecond\r\n", "first\r\nsecond\r\n", 1, 2),
    ("first\nlast", "last", 2, 2),
    ("first\n\nlast\n", "\n", 2, 2),
    ("one\u2028two\nlast\n", "one\u2028two\n", 1, 1),
])
def test_exact_newline_and_multiline_evidence(text, snippet, start, end):
    assert check_source(text, snippet, start, end).evidence_status == "validated"


def test_successful_base_side_evidence():
    assert check_source("old\n", "old\n", 1, 1, side="base").proposal.evidence[0].side == "base"


def test_one_invalid_reference_rejects_the_whole_finding():
    invalid = EvidenceReference(snapshot_id=IDENTITY, file_path="sample.py", start_line=1, end_line=1, snippet="invented\n")
    with pytest.raises(ValueError):
        check_source("actual\n", "actual\n", 1, 1, extra=(invalid,))


def test_terminal_newline_does_not_create_a_phantom_line():
    with pytest.raises(ValueError):
        check_source("first\n", "\n", 2, 2)
