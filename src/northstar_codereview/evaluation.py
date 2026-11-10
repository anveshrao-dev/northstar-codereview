"""Pure scoring of trusted reports against independently authored labels."""

import json
from hashlib import sha256

from .evaluation_models import (ControlAssessment, EvaluationDataset, EvaluationResult, EvidenceTelemetry,
                                FindingAssessment, FindingJudgment, QualityMetrics, WorkflowMetrics)
from .output_safety import contains_sensitive
from .reporting import ReviewReport
from .models import InspectionFailureCount


class EvaluationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(f'Evaluation rejected input ({code})')


def fingerprint(value) -> str:
    if hasattr(value, 'model_dump'):
        value = value.model_dump(mode='json')
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                             allow_nan=False).encode()).hexdigest()


def _candidate(finding, label) -> bool:
    return (finding.agent_name == label.agent_name and finding.category == label.category
            and all(any(ref.file_path == loc.file_path and ref.side == loc.side
                        and ref.start_line <= loc.start_line and ref.end_line >= loc.end_line
                        for ref in finding.evidence) for loc in label.locations))


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _workflow(report, scenario, evidence, elapsed, network_attempts):
    selected = [s for s in report.specialists if s.selected] if report else []
    complete_set = {s.agent_name for s in selected} == set(scenario.specialists)
    usages = [s.usage for s in selected]
    inspections = [s.inspection for s in selected]
    usage_known = complete_set and all(u is not None for u in usages)
    inspection_known = complete_set and all(i is not None for i in inspections)
    def consumption(field):
        return sum(getattr(u, field) for u in usages) if usage_known else None
    def inspection(field):
        return sum(getattr(i, field) for i in inspections) if inspection_known else None
    unknown = consumption('usage_unknown_calls')
    recovered, unresolved = inspection('recovered_errors'), inspection('unresolved_errors')
    categories = None
    if inspection_known and all(i.failures is not None for i in inspections):
        totals = {}
        for item in inspections:
            for failure in item.failures:
                counts = totals.setdefault(failure.category, dict(recovered=0, unresolved=0))
                counts['recovered'] += failure.recovered
                counts['unresolved'] += failure.unresolved
        categories = tuple(InspectionFailureCount(category=category, **counts) for category, counts in sorted(totals.items()))
    return WorkflowMetrics(
        inspection_failures=categories,
        network_attempts=network_attempts,
        specialist_completion_rate=(sum(s.status == 'completed' and s.agent_name in scenario.specialists
                                       for s in selected) / len(scenario.specialists)) if report else None,
        model_attempts=consumption('model_calls'), tool_attempts=consumption('tool_attempts'),
        retries=consumption('retries'), repairs=consumption('repairs'),
        tool_failures=(recovered + unresolved) if inspection_known else None,
        recovered_failures=recovered, unresolved_failures=unresolved,
        recovery_attempts=inspection('recovery_attempts'),
        reported_input_tokens=consumption('reported_input_tokens') if unknown == 0 else None,
        reported_output_tokens=consumption('reported_output_tokens') if unknown == 0 else None,
        unknown_usage_attempts=unknown, elapsed_seconds=elapsed,
        evidence_proposed=evidence.proposed if evidence else None,
        evidence_validated=evidence.validated if evidence else None,
        evidence_validation_rate=_ratio(evidence.validated, evidence.proposed) if evidence else None)


def evaluate(dataset: EvaluationDataset, scenario_id: str, report: ReviewReport | None, *,
             judgments=(), configuration_id: str, run_kind='external_review',
             evidence: EvidenceTelemetry | None = None, elapsed_seconds: float | None = None,
             network_attempts: int | None = None) -> EvaluationResult:
    """No filesystem/network/model access; inputs must come from trusted callers.

    A candidate needs the correct specialist/category and all labeled anchors.
    Only an explicit report-bound semantic judgment gives credit. Pending claims,
    incomplete/withheld results or missing specialists suppress full accuracy.
    Ratios are undefined (null) for zero denominators. Semantic duplicates earn
    one TP per label; out-of-scope judgments are disclosed and excluded.
    """
    try:
        dataset = EvaluationDataset.model_validate(dataset.model_dump(mode='json'))
        scenario = next((s for s in dataset.scenarios if s.id == scenario_id), None)
        if scenario is None:
            raise EvaluationError('unknown_scenario')
        dataset_id = fingerprint(dataset)
        if report is not None:
            report = ReviewReport.model_validate(report.model_dump(mode='json'))
            if report.snapshot_id != scenario.snapshot.snapshot_id or report.snapshot_revision != scenario.snapshot.revision:
                raise EvaluationError('snapshot_mismatch')
            files = {(f.side, f.file_path): f.sha256 for f in scenario.snapshot.files}
            if any(files.get((r.side, r.file_path)) != r.source_sha256 for f in report.findings for r in f.evidence):
                raise EvaluationError('source_hash_mismatch')
        judgments = tuple(FindingJudgment.model_validate(j.model_dump(mode='json')) for j in judgments)
        if run_kind != 'scripted_integration' and any(j.origin == 'scripted_fixture' for j in judgments):
            raise EvaluationError('scripted_judgment_requires_scripted_run')
        by_id = {j.finding_id: j for j in judgments}
        if len(by_id) != len(judgments):
            raise EvaluationError('duplicate_judgment')
        predictions = {f.finding_id: f for f in report.findings} if report else {}
        for j in judgments:
            if (report is None or j.report_id != report.report_id or j.dataset_id != dataset_id
                    or j.scenario_id != scenario.id or j.finding_id not in predictions):
                raise EvaluationError('stale_judgment')
        if evidence is not None:
            evidence = EvidenceTelemetry.model_validate(evidence.model_dump(mode='json'))
            withheld = {n.finding_id for n in report.safety if n.action == 'finding_withheld'} if report else set()
            if (report is None or evidence.report_id != report.report_id
                    or evidence.validated != len(predictions) + len(withheld)):
                raise EvaluationError('invalid_telemetry')
        expected = {label.id: label for label in scenario.expected}
        controls = {label.id: label for label in scenario.controls}
        matched, violated, ambiguous_controls, assessments = set(), set(), set(), []
        fp = pending = excluded = duplicates = 0
        for f in sorted(predictions.values(), key=lambda item: str(item.finding_id)):
            candidates = tuple(sorted(label.id for label in scenario.expected if _candidate(f, label)))
            j = by_id.get(f.finding_id)
            same_severity = None
            if j is None:
                pending += 1
            elif j.verdict == 'match':
                if j.expected_id not in candidates:
                    raise EvaluationError('invalid_match')
                duplicates += j.expected_id in matched
                matched.add(j.expected_id)
                same_severity = f.severity == expected[j.expected_id].severity
            elif j.verdict == 'false_positive':
                fp += 1
                ambiguous_controls.update(id for id, control in controls.items()
                                          if id != j.control_id and _candidate(f, control))
                if j.control_id is not None:
                    if j.control_id not in controls or not _candidate(f, controls[j.control_id]):
                        raise EvaluationError('invalid_control_match')
                    violated.add(j.control_id)
            else:
                excluded += 1
            assessments.append(FindingAssessment(finding_id=f.finding_id, agent_name=f.agent_name,
                category=f.category, severity=f.severity, candidate_ids=candidates,
                disposition=j.verdict if j else 'pending_human_review', expected_id=j.expected_id if j else None,
                severity_matches_guidance=same_severity, judgment_origin=j.origin if j else None))
        complete = (report is not None and report.status == 'completed' and not report.safety
                    and {s.agent_name for s in report.specialists if s.selected} == set(scenario.specialists)
                    and all(s.status == 'completed' for s in report.specialists if s.selected))
        eligible = complete and not pending
        tp = len(matched)
        fn = len(expected) - tp if eligible else None
        quality = QualityMetrics(accuracy_scored=eligible, true_positives=tp, false_positives=fp,
            false_negatives=fn, pending=pending, out_of_scope=excluded, duplicate_matches=duplicates,
            precision=_ratio(tp, tp + fp) if eligible else None,
            recall=_ratio(tp, tp + fn) if eligible else None,
            f1=_ratio(2 * tp, 2 * tp + fp + fn) if eligible else None)
        control_results = tuple(ControlAssessment(control_id=id, status=(
            'violated' if id in violated else 'not_evaluated' if not complete
            else 'pending' if not eligible or id in ambiguous_controls else 'passed'))
            for id in sorted(controls))
        workflow = _workflow(report, scenario, evidence, elapsed_seconds, network_attempts)
        limitations = ['scoped_ground_truth']
        if run_kind == 'scripted_integration': limitations.append('scripted_not_accuracy')
        if pending: limitations.append('semantic_review_pending')
        if not complete: limitations.append('missing_result' if report is None else 'incomplete_review')
        if workflow.unknown_usage_attempts != 0: limitations.append('unknown_usage')
        if evidence is None: limitations.append('evidence_denominator_unavailable')
        if report and report.safety: limitations.append('report_content_withheld')
        if ambiguous_controls - violated: limitations.append('control_attribution_pending')
        result = EvaluationResult(run_kind=run_kind, dataset_id=dataset_id, configuration_id=configuration_id,
            scenario_id=scenario.id, snapshot_id=scenario.snapshot.snapshot_id, snapshot_revision=scenario.snapshot.revision,
            report_id=report.report_id if report else None, review_status=report.status if report else 'missing',
            quality=quality, workflow=workflow, findings=tuple(assessments), controls=control_results,
            limitations=tuple(sorted(limitations)))
        if contains_sensitive(result.model_dump(mode='json')):
            raise EvaluationError('unsafe_output')
        return result
    except EvaluationError:
        raise
    except (ValueError, TypeError, AttributeError):
        raise EvaluationError('invalid_evaluation_input') from None


def render_evaluation(result: EvaluationResult) -> str:
    """Safe JSON projection: IDs, enums and metrics, never raw claims/source."""
    try:
        result = EvaluationResult.model_validate(result.model_dump(mode='json'))
        data = result.model_dump(mode='json')
        if contains_sensitive(data):
            raise ValueError('Unsafe output')
        return json.dumps(data, sort_keys=True, ensure_ascii=True, indent=2, allow_nan=False) + '\n'
    except (ValueError, TypeError, AttributeError):
        raise EvaluationError('unsafe_output') from None
