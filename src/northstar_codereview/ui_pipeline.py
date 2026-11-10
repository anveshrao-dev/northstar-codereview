"""Read-only presentation projections. Never executes, authorizes or persists a review."""
from dataclasses import dataclass, replace
from typing import Literal

StageStatus = Literal['pending', 'running', 'completed', 'partial', 'failed', 'blocked', 'unknown', 'skipped', 'not_started']
STATUS_LABELS = {'pending': 'Pending', 'running': 'Running', 'completed': 'Completed',
                 'partial': 'Partial', 'failed': 'Failed', 'blocked': 'Blocked / awaiting approval',
                 'unknown': 'Unavailable', 'skipped': 'Not run', 'not_started': 'Not started'}
STAGES = (('source', 'Source verified'), ('authorization', 'Sharing approved'),
          ('preparation', 'Source prepared'), ('analysis', 'Code analyzed'),
          ('evidence', 'Evidence checked'), ('report', 'Report saved'),
          ('human', 'Human review'))


@dataclass(frozen=True)
class PipelineStage:
    key: str
    title: str
    status: StageStatus
    detail: str


@dataclass(frozen=True)
class StageDetails:
    evidence_source: str
    counts: tuple[tuple[str, int | None], ...] = ()
    diagnostics: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ('Chronological stage events and stage durations were not persisted.',)
    subjects: tuple[str, ...] = ()


def recorded_stage_details(output):
    """Bounded projection of counts/closed diagnostics only, never free-text payloads."""
    missing = StageDetails('Unavailable: no saved terminal record.', unavailable=('Stage telemetry unavailable.',))
    details = {key: missing for key, _ in STAGES}
    if output is None:
        return details
    report = output.report
    selected = [s for s in report.specialists if s.selected] if report else []
    row = selected[0] if len(selected) == 1 else None
    d = row.diagnostics if row else None
    a = d.analysis if d else None
    inspection = row.inspection if row else None
    absent_events = ('No chronological event log or per-stage timing is available.',)
    details['source'] = StageDetails('Locally revalidated saved report/import binding; terminal source counters.',
        (('Snapshot files', d.total_files if d else None), ('Snapshot lines', d.total_lines if d else None)),
        unavailable=absent_events + ('No original project directory is consulted. Source counters may be unavailable in legacy records.',))
    details['authorization'] = StageDetails('Saved transmission lineage and terminal model-attempt counters; reconstructed fact.',
        (('Recorded model attempts', output.usage.model_attempts),),
        ('Transmission lineage recorded.' if output.transmission_id else 'Transmission lineage unavailable.',),
        absent_events + ('Durable claim consumption is not independently inspected by this panel; no new permission is granted.',))
    prep_codes = []
    if inspection and inspection.failures is not None:
        prep_codes = [f'{f.category}: recovered={f.recovered}, unresolved={f.unresolved}' for f in inspection.failures[:10]]
    capacity = output.capacity or a
    if capacity:
        prep_codes += ['Admission: '+capacity.admission] + ['Capacity: '+reason for reason in capacity.denial_reasons]
    details['preparation'] = StageDetails('Saved inspection summary, preparation and range diagnostics.',
        (('Prepared lines', a.prepared_lines if a else None), ('Planned lines', a.planned_lines if a else None),
         ('Prepared files', len({(r.side,r.file_path) for r in a.ranges if r.prepared}) if a else None),
         ('Prepared ranges', sum(r.prepared for r in a.ranges) if a else None),
         ('Tool attempts', output.usage.tool_attempts), ('Successful tools', inspection.successful_attempts if inspection else None),
         ('Recovered failures', inspection.recovered_errors if inspection else None),
         ('Unresolved failures', inspection.unresolved_errors if inspection else None)), tuple(prep_codes[:16]),
        absent_events + ('Missing range or failure-category telemetry is unavailable, not a zero count.',))
    unit_codes = []
    if a and a.units is not None:
        for unit in a.units[:12]:
            response = unit.response
            unit_codes.append(f'Unit {unit.unit_id}: {unit.classification}; schema={unit.schema_status}; '
                f'finish={response.finish_reason if response else "unavailable"}; '
                f'truncated={response.truncated if response and response.truncated is not None else "unavailable"}')
    elif row:
        unit_codes.append('Recorded synthesis: '+row.synthesis)
        if d and d.final_response:
            f = d.final_response
            unit_codes.append(f'Final response: finish={f.finish_reason}; truncated={f.truncated if f.truncated is not None else "unavailable"}; stage={f.failure_stage}')
    details['analysis'] = StageDetails('Saved unit classifications/schema metadata, or legacy final-response diagnostics.',
        (('Analyzed lines', a.analyzed_lines if a else None), ('Failed lines', a.failed_lines if a else None),
         ('Skipped lines', a.skipped_lines if a else None), ('Recorded units', len(a.units) if a and a.units is not None else None)),
        tuple(unit_codes), absent_events + ('At most 12 units are shown. Missing unit/schema/finish metadata remains unavailable; no provider text is retained here.',))
    details['evidence'] = StageDetails('Retained evidence-validated report findings and saved validation-gate counters.',
        (('Retained findings', len(report.findings) if report else None),
         ('Retained citations', sum(len(f.evidence) for f in report.findings) if report else None),
         ('Rejected proposals', d.preparation.rejected_proposals if d and d.preparation else None),
         ('Invalid-evidence ranges', sum(r.reason == 'invalid_evidence' for r in a.ranges) if a else None)),
        unavailable=absent_events + ('Generic proposal rejection does not identify the failed evidence check. The invalid_evidence bucket can include specialist-category rejection. Citation integrity is not defect correctness.',))
    details['report'] = StageDetails('Reconstructed from the validated saved artifact and terminal cleanup status.',
        (('Retained findings', len(report.findings) if report else None),),
        ('Report persisted: '+('yes' if output.persisted else 'no'), 'Provider cleanup: '+output.cleanup) +
        (('Terminal category: '+output.category,) if output.category else ()), absent_events)
    details['human'] = StageDetails('Current report-bound local checkpoint lookup; separate from the saved model result.',
        unavailable=('No individual-finding verdict or publication approval is inferred. Checkpoint identities and decision payloads are not displayed here.',))
    if row:
        from .ui_models import AGENT_NAMES
        subjects=('Recorded specialist: '+AGENT_NAMES[row.agent_name.value],)
        details['analysis']=replace(details['analysis'],subjects=subjects)
    if a:
        for stage,paths in (
            ('preparation', sorted({r.file_path for r in a.ranges if r.prepared})),
            ('analysis', sorted({r.file_path for r in a.ranges if r.outcome=='analyzed'}))):
            details[stage]=replace(details[stage],subjects=details[stage].subjects+
                tuple('Recorded file: '+path for path in paths[:50]))
    return details


def stage_label(stage):
    if stage.key == 'human' and stage.status == 'blocked':
        return 'Awaiting human review'
    return STATUS_LABELS.get(stage.status, 'Unavailable')


def pipeline_stages(output, *, human_review=None, human_review_missing=False):
    """Project a validated saved run; no status is inferred from overall completion alone.

    Callers use the existing retained-result loader before presentation. Human review
    must match the exact report; absence of a checkpoint read is not a pending decision.
    """
    state = {k: ('unknown', 'This record does not establish this stage.') for k, _ in STAGES}
    if output is None:
        return tuple(PipelineStage(k, title, *state[k]) for k, title in STAGES)
    report = output.report
    selected = [s for s in report.specialists if s.selected] if report else []
    # Real-source UI supports one specialist. Do not invent aggregation for other scopes.
    summary = selected[0] if len(selected) == 1 else None
    diagnostics = summary.diagnostics if summary else None
    analysis = diagnostics.analysis if diagnostics else None
    if output.persisted and report and output.manifest_digest:
        state['source'] = ('completed', 'A snapshot-bound report and manifest identity were retained. This does not mean every source line was analyzed.')
    if output.category in ('authorization', 'transmission_consumed', 'run_already_claimed'):
        state['authorization'] = ('blocked', 'The execution authorization gate rejected this attempt. No new authority is granted by viewing it.')
    elif output.transmission_id and output.manifest_digest and output.usage.model_attempts > 0:
        state['authorization'] = ('completed', 'Transmission lineage and model attempts are recorded for this execution. This is historical evidence, not permission to run again.')
    capacity = output.capacity or analysis
    if capacity and capacity.admission == 'denied':
        state['preparation'] = ('blocked', 'The complete source plan was denied by its fixed capacity limits.')
    elif analysis:
        if analysis.ranges and all(r.prepared for r in analysis.ranges):
            state['preparation'] = ('completed', f'All planned ranges prepared: {analysis.prepared_lines}/{analysis.planned_lines} lines. Preparation is not model analysis.')
        elif any(r.prepared for r in analysis.ranges):
            state['preparation'] = ('partial', f'Prepared {analysis.prepared_lines}/{analysis.planned_lines} planned lines; some ranges were not prepared.')
        elif any(r.reason == 'inspection_failed' for r in analysis.ranges):
            state['preparation'] = ('failed', 'A planned source inspection failed; complete preparation is not established.')
    elif diagnostics:
        state['preparation'] = ('unknown', f'Legacy observed coverage: {diagnostics.observed_lines}/{diagnostics.total_lines} lines. A chunk plan was not recorded.')
    if analysis and analysis.ranges and all(r.outcome == 'analyzed' for r in analysis.ranges):
        state['analysis'] = ('completed', f'Accepted unit analysis covers {analysis.analyzed_lines}/{analysis.planned_lines} lines. Completion does not establish defect-free code.')
    elif analysis and any(r.outcome == 'analyzed' for r in analysis.ranges):
        state['analysis'] = ('partial', f'Accepted analysis covers {analysis.analyzed_lines}/{analysis.planned_lines} lines. Earlier accepted results are preserved.')
    elif summary:
        status = {'succeeded': 'completed', 'insufficient_evidence': 'partial', 'budget_denied': 'blocked',
                  'malformed_output': 'failed', 'tool_call_rejected': 'failed', 'request_failed': 'failed',
                  'not_started': 'unknown'}[summary.synthesis]
        # A successful final envelope alone cannot override explicit incomplete chunk telemetry.
        if analysis and status == 'completed':
            status = 'partial'
        elif status == 'completed':
            if summary.status != 'completed' or (diagnostics and (diagnostics.observed_lines < diagnostics.total_lines or diagnostics.fully_inspected_files < diagnostics.total_files)):
                status = 'partial'
            elif diagnostics is None:
                status = 'unknown'
        detail = ('Legacy final envelope accepted; validated analyzed-line coverage was not recorded.'
                  if status == 'completed' else 'Recorded synthesis outcome: ' + summary.synthesis.replace('_', ' ') + '. No missing analysis is assumed complete.')
        state['analysis'] = (status, detail)
        if summary.status == 'skipped':
            state['analysis'] = ('skipped', 'The specialist is explicitly recorded as skipped; analysis did not run.')
    rejected = bool(analysis and any(r.reason == 'invalid_evidence' for r in analysis.ranges))
    proposal_rejected = bool(diagnostics and diagnostics.preparation and diagnostics.preparation.rejected_proposals)
    findings = len(report.findings) if report else 0
    if rejected:
        state['evidence'] = ('partial' if findings else 'failed', f'Proposal/evidence rejection was recorded. {findings} evidence-validated findings were retained; rejected proposals are not findings.')
    elif proposal_rejected:
        state['evidence'] = ('partial' if findings else 'unknown', f'Proposal rejection was recorded; this does not establish an evidence-check failure. {findings} evidence-validated findings were retained. The rejected proposal may have failed structure or specialist-category validation first.')
    elif findings:
        state['evidence'] = ('completed', f'{findings} retained findings passed exact-source evidence validation. This does not prove defect correctness.')
    else:
        state['evidence'] = ('unknown', 'No retained citations to validate. Zero findings does not prove defect absence; finding-level evidence checks may not have been exercised.')
    if output.category == 'storage_failure':
        state['report'] = ('failed', 'Durable report persistence failed. No saved report is claimed.')
    elif output.persisted and report:
        state['report'] = ('completed', 'A ' + report.status + ' report was generated and persisted. Report generation is separate from review completion.')
    if human_review_missing and report:
        state['human'] = ('not_started', 'No checkpoint exists for this report’s selected local review thread. Start human review to create one; no decision is inferred.')
    if human_review and report and human_review.report.report_id == report.report_id and human_review.report.review_id == report.review_id:
        if human_review.status == 'pending':
            state['human'] = ('blocked', 'A local checkpoint is awaiting a manual report-level decision. Findings are not automatically approved.')
        elif human_review.outcome and human_review.status == 'finalized':
            decision = human_review.outcome
            if (decision.report_hash, decision.review_id, decision.report_revision) == (report.report_id, report.review_id, report.report_revision):
                state['human'] = ('completed', 'Recorded local report decision: ' + decision.decision.replace('_', ' ') + '. This grants neither defect correctness nor publication authority.')
    return tuple(PipelineStage(k, title, *state[k]) for k, title in STAGES)
