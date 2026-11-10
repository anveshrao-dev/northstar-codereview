"""Plain presentation contracts; no execution, storage, HTML, or authority."""
from typing import Literal, Self
from uuid import UUID

from pydantic import model_validator

from .evaluation_models import EvaluationResult, ControlAssessment
from .models import AgentName, Contract, SpecialistDiagnostics
from .output_safety import contains_sensitive
from .reporting import ReviewReport

SCENARIOS = ('java_backend', 'angular_frontend', 'jenkins_aks')
SCENARIO_NAMES = {'java_backend': 'Java backend', 'angular_frontend': 'Angular / TypeScript', 'jenkins_aks': 'Jenkins on AKS'}
AGENT_NAMES = {'code_quality': 'Code Quality', 'security': 'Security', 'test_coverage': 'Test Coverage',
               'gitops': 'GitOps', 'cicd_groovy': 'CI/CD & Groovy'}


class ReviewBundle(Contract):
    scenario_id: Literal['java_backend', 'angular_frontend', 'jenkins_aks', 'portfolio_risk', 'csv_import', 'bm25_retrieval']
    provenance: Literal['scripted_integration', 'recorded_live', 'real_source_live']
    report: ReviewReport | None = None
    evaluation: EvaluationResult | None = None
    artifact_name: str | None = None
    run_status: str = 'missing'
    input_tokens: int | None = None
    output_tokens: int | None = None
    unknown_usage: int | None = None
    elapsed_seconds: float | None = None
    model_attempts: int | None = None
    tool_attempts: int | None = None
    provider: str | None = None
    model: str | None = None

    @model_validator(mode='after')
    def consistent(self) -> Self:
        real = self.scenario_id in ('portfolio_risk', 'csv_import', 'bm25_retrieval')
        if real != (self.provenance == 'real_source_live') or (real and self.evaluation is not None):
            raise ValueError('Real-source provenance mismatch')
        if contains_sensitive(self.model_dump(mode='json')):
            raise ValueError('Unsafe presentation data')
        if self.evaluation:
            expected = 'scripted_integration' if self.provenance == 'scripted_integration' else 'external_review'
            if self.evaluation.scenario_id != self.scenario_id or self.evaluation.run_kind != expected:
                raise ValueError('Evaluation provenance mismatch')
            if self.evaluation.report_id != (self.report.report_id if self.report else None):
                raise ValueError('Report identity mismatch')
            if self.report and (self.evaluation.snapshot_id != self.report.snapshot_id
                                or self.evaluation.snapshot_revision != self.report.snapshot_revision
                                or self.evaluation.review_status != self.report.status):
                raise ValueError('Evaluation identity mismatch')
            findings = {f.finding_id: f for f in self.report.findings} if self.report else {}
            assessed = set()
            for assessment in self.evaluation.findings:
                finding = findings.get(assessment.finding_id)
                if (finding is None or assessment.finding_id in assessed
                        or (assessment.agent_name, assessment.category, assessment.severity)
                        != (finding.agent_name, finding.category, finding.severity)):
                    raise ValueError('Finding assessment mismatch')
                if self.provenance == 'recorded_live' and assessment.judgment_origin == 'scripted_fixture':
                    raise ValueError('Scripted judgment cannot describe live performance')
                assessed.add(assessment.finding_id)
        return self


class SpecialistCard(Contract):
    diagnostics: SpecialistDiagnostics | None = None
    agent: str
    name: str
    selected: bool
    status: str
    files: tuple[str, ...]
    synthesis: str
    model_attempts: int | None = None
    tool_attempts: int | None = None
    tool_successes: int | None = None
    recovered: int | None = None
    unresolved: int | None = None
    limitations: tuple[str, ...] = ()


def activity_label(row: SpecialistCard) -> str:
    if row.status == 'unavailable':
        return 'Selection and execution unavailable'
    if not row.selected:
        return 'Not selected'
    if (row.model_attempts or 0)>0 or (row.tool_attempts or 0)>0:
        return 'Selected and executed' + (' · Incomplete' if row.status!='completed' else '')
    if row.model_attempts is None or row.tool_attempts is None:
        return 'Selected · Execution metadata unavailable'
    return ('Selected but incomplete' if row.status!='completed' else 'Selected') + ' · No execution attempts recorded'


class FindingCard(Contract):
    finding_id: UUID
    title: str
    specialist: str
    scripted_case: Literal['false_positive', 'match'] | None = None
    severity: str
    category: str
    confidence_percent: float
    locations: tuple[str, ...]
    evidence_status: Literal['validated']
    claim_status: Literal['unverified']
    explanation: str
    remediation: str
    assumptions: tuple[str, ...]


class ReviewScreen(Contract):
    scenario_id: str
    provenance: str
    report_id: str | None
    status: str
    specialists: tuple[SpecialistCard, ...]
    findings: tuple[FindingCard, ...]
    controls: tuple[ControlAssessment, ...]
    limitations: tuple[str, ...]
    pending_judgments: int | None
    network_attempts: int | None
    accuracy_established: Literal[False] = False
    approval_threshold: None = None


def present_review(bundle: ReviewBundle) -> ReviewScreen:
    """Revalidate before projection; never create findings from model proposals."""
    bundle = ReviewBundle.model_validate_json(bundle.model_dump_json())
    report, evaluation = bundle.report, bundle.evaluation
    # Report identity is checked above; classify only explicit fixture judgments.
    # Never use title text, confidence, citation overlap or a control ID heuristic.
    cases = ({a.finding_id: a.disposition for a in evaluation.findings
              if a.judgment_origin == 'scripted_fixture' and a.disposition in ('false_positive', 'match')}
             if evaluation and bundle.provenance == 'scripted_integration' else {})
    summaries = {s.agent_name.value: s for s in report.specialists} if report else {}
    cards = []
    for agent, name in AGENT_NAMES.items():
        row = summaries.get(agent)
        usage = row.usage if row else None
        inspection = row.inspection if row else None
        cards.append(SpecialistCard(diagnostics=row.diagnostics if row else None, agent=agent, name=name, selected=bool(row and row.selected),
            status=row.status.value if row else ('skipped' if report else 'unavailable'),
            files=row.reviewed_files if row else (), synthesis=row.synthesis if row else 'not_started',
            model_attempts=usage.model_calls if usage else None, tool_attempts=usage.tool_attempts if usage else None,
            tool_successes=inspection.successful_attempts if inspection else None,
            recovered=inspection.recovered_errors if inspection else None,
            unresolved=inspection.unresolved_errors if inspection else None,
            limitations=row.limitations if row else ()))
    findings = tuple(FindingCard(finding_id=f.finding_id, title=f.title, severity=f.severity.value,
        specialist=AGENT_NAMES[f.agent_name.value], scripted_case=cases.get(f.finding_id),
        category=f.category.value, confidence_percent=f.confidence*100,
        locations=tuple(f'{e.file_path}:{e.start_line}–{e.end_line} ({e.side.value})' for e in f.evidence),
        evidence_status=f.evidence_status, claim_status=f.claim_status, explanation=f.explanation,
        remediation=f.suggested_remediation, assumptions=f.assumptions) for f in report.findings) if report else ()
    return ReviewScreen(scenario_id=bundle.scenario_id, provenance=bundle.provenance,
        report_id=report.report_id if report else None, status=report.status if report else 'missing',
        specialists=tuple(cards), findings=findings, controls=evaluation.controls if evaluation else (),
        limitations=report.limitations if report else ('No report was retained for this run.',),
        pending_judgments=evaluation.quality.pending if evaluation else None,
        network_attempts=evaluation.workflow.network_attempts if evaluation else None)
