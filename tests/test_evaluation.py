"""Semantic adjudication is explicit; correct citations alone earn no credit."""

from uuid import uuid4

import pytest

from northstar_codereview.agents.code_quality import Usage
from northstar_codereview.evaluation import EvaluationError, evaluate, fingerprint, render_evaluation
from northstar_codereview.evaluation_data import load_dataset, load_scenario
from northstar_codereview.evaluation_models import EvidenceTelemetry, FindingJudgment
from northstar_codereview.models import EvidenceReference, FindingProposal, InspectionSummary, SpecialistResult, validate_finding
from northstar_codereview.orchestrator import OrchestrationResult, SpecialistExecution
from northstar_codereview.reporting import build_report


CONFIG = '1' * 64


@pytest.fixture
def case():
    dataset = load_dataset()
    scenario = dataset.scenarios[0]
    return dataset, scenario, load_scenario(scenario)


def prediction(case, label=None, **updates):
    _, scenario, snapshot = case
    label = label or scenario.expected[0]
    data = dict(title='Predeclared synthetic claim', category=label.category, severity='medium', confidence=0.8,
                explanation='Synthetic assessment with semantic adjudication required.',
                suggested_remediation='Review the contract with a focused boundary assertion.',
                evidence=[EvidenceReference(snapshot_id=snapshot.manifest.snapshot_id, **loc.model_dump(),
                    snippet=''.join(snapshot.sources[loc.side, loc.file_path].splitlines(keepends=True)[loc.start_line-1:loc.end_line]))
                          for loc in label.locations])
    name = updates.pop('agent_name', label.agent_name)
    data.update(updates)
    return validate_finding(FindingProposal(**data), snapshot.manifest, snapshot.sources, agent_name=name)


def report_for(case, findings=(), *, status='completed', usage_known=True, inspection=True):
    _, scenario, snapshot = case
    review = uuid4()
    usage = Usage(model_calls=2, tool_rounds=1, tool_attempts=3, retries=0, repairs=0,
                  input_tokens_reserved=200, output_tokens_reserved=200, reported_input_tokens=100,
                  reported_output_tokens=50, usage_unknown_calls=0 if usage_known else 2)
    notes = () if status == 'completed' else ('Incomplete scripted review.',)
    executions = []
    for name in scenario.specialists:
        assigned = tuple(f for f in findings if f.agent_name == name)
        result = SpecialistResult(job_id=uuid4(), review_id=review, snapshot_id=snapshot.manifest.snapshot_id,
            agent_name=name, status=status, reviewed_files=tuple(f.file_path for f in snapshot.manifest.files),
            findings=tuple(f.proposal for f in assigned), limitations=notes,
            inspection=InspectionSummary(successful_attempts=3) if inspection else None)
        executions.append(SpecialistExecution(result=result, validated_findings=assigned, usage=usage))
    outcome = OrchestrationResult(review_id=review, snapshot_id=snapshot.manifest.snapshot_id,
        selected_specialists=scenario.specialists, specialist_results=tuple(executions),
        validated_findings=tuple(findings), limitations=notes, failures=(), status=status)
    return build_report(outcome, snapshot=snapshot.manifest)


def judge(case, report, finding, **kwargs):
    dataset, scenario, _ = case
    defaults = dict(verdict='match', expected_id=scenario.expected[0].id)
    defaults.update(kwargs)
    return FindingJudgment(dataset_id=fingerprint(dataset), scenario_id=scenario.id, report_id=report.report_id,
                           finding_id=finding.finding_id, origin='human', adjudicator='test_reviewer', **defaults)


def score(case, report, judgments=(), **kwargs):
    dataset, scenario, _ = case
    return evaluate(dataset, scenario.id, report, judgments=judgments, configuration_id=CONFIG, **kwargs)


def test_overlapping_evidence_is_only_a_candidate(case):
    report = report_for(case, [prediction(case)])
    result = score(case, report)
    assert result.findings[0].candidate_ids == ('full_refund',)
    assert result.quality.true_positives == 0 and result.quality.pending == 1
    assert result.quality.false_negatives is None and result.quality.precision is None
    assert result.controls[0].status == 'pending'


def test_semantic_matching_false_positive_false_negative_and_rates(case):
    correct, wrong = prediction(case), prediction(case, case[1].controls[0], title='Deliberately incorrect control claim')
    report = report_for(case, [correct, wrong])
    judgments = [judge(case, report, correct), judge(case, report, wrong, verdict='false_positive', expected_id=None,
                                                  control_id=case[1].controls[0].id)]
    result = score(case, report, judgments)
    q = result.quality
    assert (q.true_positives, q.false_positives, q.false_negatives) == (1, 1, 0)
    assert q.precision == .5 and q.recall == 1 and q.f1 == pytest.approx(2/3)
    assert result.controls[0].status == 'violated'
    # A semantically incorrect claim on the expected lines remains a false positive.
    report = report_for(case, [correct])
    q = score(case, report, [judge(case, report, correct, verdict='false_positive', expected_id=None)]).quality
    assert (q.true_positives, q.false_positives, q.false_negatives) == (0, 1, 1)
    assert q.precision == q.recall == q.f1 == 0


def test_completed_zero_findings_is_not_missing(case):
    zero = score(case, report_for(case))
    missing = score(case, None)
    assert zero.quality.false_negatives == 1 and zero.quality.recall == zero.quality.f1 == 0
    assert zero.quality.precision is None and zero.controls[0].status == 'passed'
    assert missing.review_status == 'missing' and missing.quality.false_negatives is None
    assert missing.controls[0].status == 'not_evaluated' and missing.workflow.model_attempts is None


def test_no_labels_no_predictions_has_undefined_ratios(case):
    dataset, scenario, snapshot = case
    empty = scenario.model_copy(update={'expected': ()})
    data = dataset.model_copy(update={'scenarios': (empty,)})
    output = score((data, empty, snapshot), report_for(case))
    assert output.quality.false_negatives == 0
    assert output.quality.precision is output.quality.recall is output.quality.f1 is None


@pytest.mark.parametrize('status', ['partial', 'failed', 'skipped'])
def test_incomplete_never_passes_controls_or_invents_accuracy(case, status):
    result = score(case, report_for(case, status=status))
    assert not result.quality.accuracy_scored and result.quality.false_negatives is None
    assert result.controls[0].status == 'not_evaluated'
    assert result.workflow.specialist_completion_rate == 0


@pytest.mark.parametrize('field,value', [('report_id', '0'*64), ('dataset_id', '0'*64),
    ('scenario_id', 'wrong'), ('finding_id', uuid4()), ('expected_id', 'unknown')])
def test_stale_or_unknown_judgments_rejected(case, field, value):
    finding = prediction(case)
    report = report_for(case, [finding])
    judgment = judge(case, report, finding).model_copy(update={field: value})
    with pytest.raises(EvaluationError):
        score(case, report, [judgment])


def test_cross_agent_and_missing_location_cannot_match(case):
    for finding in (prediction(case, agent_name='security', category='security'),
                    prediction(case, evidence=prediction(case).proposal.evidence[:1])):
        report = report_for(case, [finding])
        assert score(case, report).findings[0].candidate_ids == ()
        with pytest.raises(EvaluationError, match='invalid_match'):
            score(case, report, [judge(case, report, finding)])


def test_duplicate_matches_do_not_inflate_recall_and_severity_is_separate(case):
    first, second = prediction(case), prediction(case, title='Another wording', severity='high')
    report = report_for(case, [first, second])
    output = score(case, report, [judge(case, report, f) for f in (first, second)])
    assert output.quality.true_positives == 1 and output.quality.duplicate_matches == 1
    assert sorted(f.severity_matches_guidance for f in output.findings) == [False, True]
    # Report synthesis already collapses exact duplicates.
    report = report_for(case, [first, first])
    assert len(report.findings) == 1
    assert score(case, report, [judge(case, report, first)]).quality.true_positives == 1


def test_out_of_scope_and_judgment_order(case):
    first, second = prediction(case), prediction(case, title='Separate possible concern')
    report = report_for(case, [first, second])
    judgments = [judge(case, report, first), judge(case, report, second, verdict='out_of_scope', expected_id=None)]
    a, b = [score(case, report, j, elapsed_seconds=1.25) for j in (judgments, list(reversed(judgments)))]
    assert a == b and render_evaluation(a) == render_evaluation(b)
    assert a.quality.out_of_scope == 1 and a.quality.precision == 1
    with pytest.raises(EvaluationError, match='duplicate_judgment'):
        score(case, report, [judgments[0], judgments[0]])


def test_evidence_rate_needs_trusted_denominator_and_unknown_usage_stays_unknown(case):
    report = report_for(case, [prediction(case)], usage_known=False, inspection=False)
    out = score(case, report)
    assert out.workflow.evidence_validation_rate is None
    assert out.workflow.reported_input_tokens is None and out.workflow.unknown_usage_attempts == 6
    assert out.workflow.tool_failures is None
    telemetry = EvidenceTelemetry(report_id=report.report_id, proposed=2, validated=1)
    out = score(case, report, evidence=telemetry)
    assert out.workflow.evidence_validation_rate == .5 and out.workflow.tool_attempts == 9
    with pytest.raises(EvaluationError, match='invalid_telemetry'):
        score(case, report, evidence=telemetry.model_copy(update={'report_id': '0'*64}))


def test_known_usage_and_latency(case):
    out = score(case, report_for(case), elapsed_seconds=2.5)
    assert out.workflow.reported_input_tokens == 300 and out.workflow.reported_output_tokens == 150
    assert out.workflow.model_attempts == 6 and out.workflow.tool_failures == 0
    assert out.workflow.elapsed_seconds == 2.5 and out.workflow.specialist_completion_rate == 1


def test_withheld_findings_are_incomplete_and_safe(case):
    secret = 'sk-' + 'FictionalNeverARealKey1234567890'
    report = report_for(case, [prediction(case, explanation=secret)])
    result = score(case, report)
    assert not result.quality.accuracy_scored and result.controls[0].status == 'not_evaluated'
    text = render_evaluation(result)
    assert secret not in text and 'snippet' not in text and 'suggested_remediation' not in text
    assert 'report_content_withheld' in text


def test_snapshot_report_and_result_tampering_fail_closed(case):
    report = report_for(case)
    with pytest.raises(EvaluationError):
        score(case, report.model_copy(update={'snapshot_revision': 'bad'}))
    result = score(case, report)
    with pytest.raises(EvaluationError):
        render_evaluation(result.model_copy(update={'scenario_id': '<script>alert(1)</script>'}))
    with pytest.raises(EvaluationError):
        render_evaluation(result.model_copy(update={'snapshot_revision': 'UNTRUSTED_RAW_RESPONSE'}))


def test_partial_observed_false_positive_remains_visible(case):
    finding = prediction(case, case[1].controls[0])
    report = report_for(case, [finding], status='partial')
    judgment = judge(case, report, finding, verdict='false_positive', expected_id=None,
                     control_id=case[1].controls[0].id)
    result = score(case, report, [judgment])
    assert result.quality.false_positives == 1 and result.quality.precision is None
    assert result.controls[0].status == 'violated'


def test_scripted_assertions_cannot_masquerade_as_human_accuracy(case):
    finding = prediction(case)
    report = report_for(case, [finding])
    scripted = judge(case, report, finding).model_copy(update={'origin': 'scripted_fixture'})
    with pytest.raises(EvaluationError, match='scripted_judgment_requires_scripted_run'):
        score(case, report, [scripted])


def test_invalid_control_attribution_and_telemetry_rejected(case):
    finding = prediction(case)
    report = report_for(case, [finding])
    judgment = judge(case, report, finding, verdict='false_positive', expected_id=None,
                     control_id=case[1].controls[0].id)
    with pytest.raises(EvaluationError, match='invalid_control_match'):
        score(case, report, [judgment])
    with pytest.raises(EvaluationError, match='invalid_telemetry'):
        score(case, report, evidence=EvidenceTelemetry(report_id=report.report_id, proposed=2, validated=2))


def test_unattributed_control_false_positive_cannot_produce_clean_control(case):
    finding = prediction(case, case[1].controls[0])
    report = report_for(case, [finding])
    judgment = judge(case, report, finding, verdict='false_positive', expected_id=None)
    result = score(case, report, [judgment])
    assert result.quality.false_positives == 1
    assert result.controls[0].status == 'pending'
    assert 'control_attribution_pending' in result.limitations
