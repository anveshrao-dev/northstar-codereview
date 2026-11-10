"""Evaluation labels are trusted test data, never specialist instructions."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from .models import (AgentName, Category, Contract, Digest, FilePath, PositiveInteger, Revision,
                     Severity, SourceSide, SourceSnapshot, Text)
from .models import InspectionFailureCount


Identifier = Annotated[str, Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')]
Count = Annotated[int, Field(strict=True, ge=0)]
Rate = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class LabelLocation(Contract):
    file_path: FilePath
    side: SourceSide = SourceSide.HEAD
    start_line: PositiveInteger
    end_line: PositiveInteger

    @model_validator(mode='after')
    def ordered(self) -> Self:
        if self.end_line < self.start_line:
            raise ValueError('Invalid label range')
        return self


class NegativeControl(Contract):
    id: Identifier
    agent_name: AgentName
    category: Category
    behavior: Text
    locations: tuple[LabelLocation, ...] = Field(min_length=1)


class ExpectedFinding(NegativeControl):
    description: Text
    severity: Severity


class EvaluationScenario(Contract):
    id: Identifier
    source_directory: FilePath
    snapshot: SourceSnapshot
    specialists: tuple[AgentName, ...] = Field(min_length=1)
    expected: tuple[ExpectedFinding, ...]
    controls: tuple[NegativeControl, ...]
    assumptions: tuple[Text, ...] = Field(min_length=1)
    completeness_boundary: Text
    provenance: Text

    @model_validator(mode='after')
    def consistent(self) -> Self:
        # Import only the static registry; no models/providers are constructed.
        from .orchestrator import SPECIALIST_SPECS
        domains = {spec.name: spec.categories for spec in SPECIALIST_SPECS}
        labels = (*self.expected, *self.controls)
        if len({label.id for label in labels}) != len(labels) or len(set(self.specialists)) != len(self.specialists):
            raise ValueError('Duplicate evaluation identity')
        files = {(f.side, f.file_path) for f in self.snapshot.files}
        for label in labels:
            if label.agent_name not in self.specialists or label.category not in domains[label.agent_name]:
                raise ValueError('Invalid label attribution')
            if any((loc.side, loc.file_path) not in files for loc in label.locations):
                raise ValueError('Label outside pinned manifest')
        return self


class EvaluationDataset(Contract):
    schema_version: Literal[1] = 1
    id: Identifier
    scenarios: tuple[EvaluationScenario, ...] = Field(min_length=1, max_length=50)

    @model_validator(mode='after')
    def unique(self) -> Self:
        if len({s.id for s in self.scenarios}) != len(self.scenarios):
            raise ValueError('Duplicate scenario identity')
        return self


class FindingJudgment(Contract):
    """Caller-supplied semantic assessment, bound to one immutable report.

    Human identity here is provenance, not authentication. Scripted assertions
    verify plumbing only. No LLM or keyword similarity adjudicates correctness.
    """
    dataset_id: Digest
    scenario_id: Identifier
    report_id: Digest
    finding_id: UUID
    verdict: Literal['match', 'false_positive', 'out_of_scope']
    expected_id: Identifier | None = None
    control_id: Identifier | None = None
    origin: Literal['human', 'scripted_fixture']
    adjudicator: Identifier

    @model_validator(mode='after')
    def valid_verdict(self) -> Self:
        if (self.verdict == 'match') != (self.expected_id is not None):
            raise ValueError('Match requires exactly one expected label')
        if self.control_id is not None and self.verdict != 'false_positive':
            raise ValueError('Control violation requires false positive')
        return self


class EvidenceTelemetry(Contract):
    """Trusted instrumentation; unique proposals scoped by specialist.

    The numerator includes only proposals accepted through schema, scope and
    exact-source validation. Counts alone cannot authenticate imported runs.
    """
    report_id: Digest
    proposed: Count
    validated: Count

    @model_validator(mode='after')
    def bounded(self) -> Self:
        if self.validated > self.proposed:
            raise ValueError('Invalid evidence counts')
        return self


class FindingAssessment(Contract):
    finding_id: UUID
    agent_name: AgentName
    category: Category
    severity: Severity
    candidate_ids: tuple[Identifier, ...]
    disposition: Literal['match', 'false_positive', 'out_of_scope', 'pending_human_review']
    expected_id: Identifier | None = None
    severity_matches_guidance: bool | None = None
    judgment_origin: Literal['human', 'scripted_fixture'] | None = None


class ControlAssessment(Contract):
    control_id: Identifier
    status: Literal['passed', 'violated', 'pending', 'not_evaluated']


class QualityMetrics(Contract):
    accuracy_scored: bool
    true_positives: Count
    false_positives: Count
    false_negatives: Count | None
    pending: Count
    out_of_scope: Count
    duplicate_matches: Count
    precision: Rate | None
    recall: Rate | None
    f1: Rate | None


class WorkflowMetrics(Contract):
    inspection_failures: tuple[InspectionFailureCount, ...] | None = Field(default=None, max_length=10)
    network_attempts: Count | None = None
    specialist_completion_rate: Rate | None
    model_attempts: Count | None
    tool_attempts: Count | None
    retries: Count | None
    repairs: Count | None
    tool_failures: Count | None
    recovered_failures: Count | None
    unresolved_failures: Count | None
    recovery_attempts: Count | None
    reported_input_tokens: Count | None
    reported_output_tokens: Count | None
    unknown_usage_attempts: Count | None
    elapsed_seconds: float | None = Field(ge=0, allow_inf_nan=False)
    evidence_proposed: Count | None
    evidence_validated: Count | None
    evidence_validation_rate: Rate | None


class EvaluationResult(Contract):
    schema_version: Literal[2] = 2
    scoring_version: Literal['semantic-adjudication-v1'] = 'semantic-adjudication-v1'
    run_kind: Literal['scripted_integration', 'external_review']
    independent_model_accuracy_established: Literal[False] = False
    dataset_id: Digest
    configuration_id: Digest
    scenario_id: Identifier
    snapshot_id: UUID
    snapshot_revision: Revision
    report_id: Digest | None
    review_status: Literal['completed', 'partial', 'failed', 'skipped', 'missing']
    quality: QualityMetrics
    workflow: WorkflowMetrics
    findings: tuple[FindingAssessment, ...]
    controls: tuple[ControlAssessment, ...]
    limitations: tuple[Literal['scripted_not_accuracy', 'semantic_review_pending', 'incomplete_review',
                               'missing_result', 'unknown_usage', 'evidence_denominator_unavailable',
                               'scoped_ground_truth', 'report_content_withheld', 'control_attribution_pending'], ...]
