"""Closed presentation states and CI-style visuals; no provider or browser requests."""
from dataclasses import replace

import pytest
from streamlit.testing.v1 import AppTest

from .test_real_source import local_intake
from .test_real_review import service, captured, Fake, ENV, permit
from .test_ui_pipeline import result


def render_jobs(bundle):
    from northstar_codereview.ui_style import render_test_jobs
    render_test_jobs(bundle)


@pytest.mark.parametrize('status,label', [('completed','Completed'), ('partial','Partial'),
    ('failed','Failed'), ('unknown','Unavailable'), ('skipped','Not run'),
    ('not_started','Not started'), ('pending','Pending'), ('running','Running')])
def test_graphical_states_have_labels_icons_and_only_closed_html(status, label):
    from northstar_codereview.ui_pipeline import pipeline_stages
    from northstar_codereview.ui_style import pipeline_html
    rows = list(pipeline_stages(None))
    rows[0] = replace(rows[0], status=status, title='<script>PRIVATE</script>', detail='PRIVATE_SOURCE')
    for compact in (False, True):
        html = pipeline_html(rows, compact=compact)
        assert html.count('<li ') == 7
        assert label in html and 'Source verified' in html
        assert 'aria-hidden="true"' in html and 'aria-label=' in html
        assert 'PRIVATE' not in html and '<script' not in html
        assert f'data-stage="source" data-status="{status}"' in html


def test_unknown_and_skipped_remain_distinct(result):
    from northstar_codereview.ui_pipeline import pipeline_stages
    row = result.report.specialists[0]
    unknown = row.model_copy(update={'synthesis':'not_started', 'diagnostics':None})
    skipped = unknown.model_copy(update={'status':'skipped', 'selected':True})
    def analysis(item):
        value = result.model_copy(update={'report':result.report.model_copy(update={'specialists':(item,)})})
        return next(s.status for s in pipeline_stages(value) if s.key == 'analysis')
    assert analysis(unknown) == 'unknown'
    assert analysis(skipped) == 'skipped'


def test_confirmed_missing_human_checkpoint_is_not_started(result):
    from northstar_codereview.ui_pipeline import pipeline_stages
    assert pipeline_stages(result)[-1].status == 'unknown'
    assert pipeline_stages(result, human_review_missing=True)[-1].status == 'not_started'


@pytest.mark.parametrize('reason', ['invalid_proposal_structure', 'wrong_specialist_category'])
def test_proposal_rejection_is_not_evidence_failure(result, reason):
    from northstar_codereview.models import PreparationDiagnostics
    from northstar_codereview.ui_pipeline import pipeline_stages
    row = result.report.specialists[0]
    prep = PreparationDiagnostics(selected_files=2, read_attempts=2, context_units=100,
                                  proposal_count=1, rejected_proposals=1)
    row = row.model_copy(update={'diagnostics':row.diagnostics.model_copy(update={
        'analysis':None, 'preparation':prep}), 'limitations':(f'Proposal 1 rejected: {reason}.',), 'finding_count':0})
    output = result.model_copy(update={'report':result.report.model_copy(update={'findings':(), 'specialists':(row,)})})
    evidence = next(s for s in pipeline_stages(output) if s.key == 'evidence')
    assert evidence.status == 'unknown'
    assert 'not establish an evidence-check failure' in evidence.detail


def test_explicit_evidence_failure_stays_red(result):
    from northstar_codereview.ui_pipeline import pipeline_stages
    row = result.report.specialists[0]
    analysis = row.diagnostics.analysis
    ranges = tuple(r.model_copy(update={'outcome':'failed', 'reason':'invalid_evidence'}) for r in analysis.ranges)
    row = row.model_copy(update={'diagnostics':row.diagnostics.model_copy(update={
        'analysis':analysis.model_copy(update={'ranges':ranges})}), 'finding_count':0})
    output = result.model_copy(update={'report':result.report.model_copy(update={'findings':(), 'specialists':(row,)})})
    assert next(s for s in pipeline_stages(output) if s.key == 'evidence').status == 'failed'


def test_pipeline_css_uses_requested_palette_and_responsive_connection_rules():
    from northstar_codereview.ui_style import _PIPELINE_STYLE
    for status in ('success','error','partial','running','pending'):
        assert 'var(--ns-'+status+')' in _PIPELINE_STYLE
    assert '@media' in _PIPELINE_STYLE and 'prefers-reduced-motion' in _PIPELINE_STYLE
    assert '::after' in _PIPELINE_STYLE and 'overflow-wrap' in _PIPELINE_STYLE
    assert 'https:' not in _PIPELINE_STYLE and 'script' not in _PIPELINE_STYLE.lower()


def test_scripted_jobs_separate_harness_success_from_asserted_false_positive():
    from northstar_codereview.ui_service import run_baseline
    from northstar_codereview.ui_style import render_test_jobs
    bundle = run_baseline('java_backend')
    assert bundle.evaluation.quality.false_positives > 0
    at = AppTest.from_function(render_jobs, args=(bundle,)).run()
    assert not at.exception
    assert any('Harness execution' in t.value for t in at.markdown)
    assert any('PASSED' in x.proto.body for x in at.get('html'))
    assert any('false-positive' in t.value for t in at.caption)
    assert any('not a test-runner pass count' in t.value for t in at.caption)


def test_critical_finding_does_not_fail_specialist_job():
    from northstar_codereview.ui_service import run_baseline
    from northstar_codereview.ui_style import render_test_jobs
    from northstar_codereview.reporting import ReviewReport, _counts, _priority, _digest
    from northstar_codereview.models import Severity
    bundle = run_baseline('java_backend')
    report = bundle.report
    findings = tuple(sorted((f.model_copy(update={'severity':Severity.CRITICAL}) for f in report.findings), key=_priority))
    body = report.model_copy(update={'findings':findings, 'severity_counts':_counts(findings)}).model_dump(mode='json', exclude={'report_id'})
    report = ReviewReport.model_validate({**body, 'report_id':_digest(body)})
    bundle = bundle.model_copy(update={'report':report, 'evaluation':None})
    at = AppTest.from_function(render_jobs, args=(bundle,)).run()
    assert not at.exception
    assert any('Critical findings: 2' in c.value for c in at.caption)
    assert not any('FAILED' in x.proto.body for x in at.get('html'))
    assert any('PASSED' in x.proto.body for x in at.get('html'))


@pytest.mark.parametrize('status,label', [('completed','PASSED'), ('failed','FAILED'),
                                        ('partial','PARTIAL'), ('skipped','NOT RUN'), ('unknown','UNAVAILABLE')])
def test_job_badges_are_explicit_not_severity_based(status, label):
    from northstar_codereview.ui_style import job_status_html
    html = job_status_html(status)
    assert label in html and 'aria-hidden' in html
