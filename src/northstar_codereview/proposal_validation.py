"""Shared specialist proposal gates with closed, value-free rejection labels."""
from typing import Literal

from pydantic import ValidationError

from .models import EvidenceValidationError, FindingProposal, ValidatedFinding, validate_finding


ProposalDiagnostic = Literal['invalid_proposal_structure', 'wrong_specialist_category',
    'unobserved_evidence_range', 'source_identity_mismatch', 'exact_source_mismatch',
    'unsupported_evidence', 'unknown_validation_failure']


def validate_observed_proposal(data, snapshot, observed, *, categories, agent_name
                               ) -> tuple[ValidatedFinding | None, ProposalDiagnostic | None]:
    """Preserve structure, domain, observed-range and source validation gates.

    No exception strings, error inputs, locations, field values, or source text
    are returned. A label describes the first failed gate, never defect truth.
    """
    try:
        proposal = FindingProposal.model_validate(data)
    except ValidationError:
        return None, 'invalid_proposal_structure'
    try:
        if proposal.category not in categories:
            return None, 'wrong_specialist_category'
        identities = {(item.side, item.file_path) for item in snapshot.manifest.files}
        for ref in proposal.evidence:
            if ref.snapshot_id != snapshot.manifest.snapshot_id:
                return None, 'source_identity_mismatch'
            if (ref.side, ref.file_path) not in identities or (ref.side, ref.file_path) not in snapshot.sources:
                return None, 'unsupported_evidence'
            if not any(ref.snapshot_id == seen.snapshot_id and ref.file_path == seen.file_path and ref.side == seen.side
                       and seen.start_line <= ref.start_line <= ref.end_line <= seen.end_line for seen in observed):
                return None, 'unobserved_evidence_range'
        return validate_finding(proposal, snapshot.manifest, snapshot.sources, agent_name=agent_name), None
    except EvidenceValidationError as error:
        if error.category in ('source_identity_mismatch', 'unsupported_evidence', 'exact_source_mismatch'):
            return None, error.category
        return None, 'unknown_validation_failure'
    except (ValueError, ValidationError):
        return None, 'unknown_validation_failure'
