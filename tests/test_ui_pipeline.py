"""Presentation-only pipeline derived from generated, validated real results."""
import asyncio
import pytest
from .test_real_source import local_intake
from .test_real_review import service, captured, Fake, ENV, permit


def presenter():
    import importlib.util
    assert importlib.util.find_spec('northstar_codereview.ui_pipeline'), 'pipeline presentation missing'
    from northstar_codereview import ui_pipeline
    return ui_pipeline


@pytest.fixture
def result(service,captured):
    approval=permit(service,captured)
    return asyncio.run(service.run_review('portfolio_risk',captured.approval_id,
        transmission_id=approval.approval_id,allow_live=True,environ=ENV,provider_factory=lambda *a:Fake()))


def stages(result,**kwargs):
    return {s.key:s for s in presenter().pipeline_stages(result,**kwargs)}


def test_missing_telemetry_never_invents_completed_stages():
    rows=presenter().pipeline_stages(None)
    assert len(rows)==7
    assert all(s.status=='unknown' for s in rows)


def test_complete_review_uses_each_stage_evidence(result):
    rows=stages(result)
    assert [rows[k].status for k in ('source','authorization','preparation','analysis','evidence','report')]==['completed']*6
    assert rows['human'].status=='unknown'
    assert 'defect' in rows['evidence'].detail


def test_overall_completed_cannot_invent_analysis_or_evidence(result):
    summary=result.report.specialists[0]
    summary=summary.model_copy(update={'diagnostics':None,'synthesis':'not_started','finding_count':0})
    report=result.report.model_copy(update={'specialists':(summary,), 'findings':()})
    rows=stages(result.model_copy(update={'report':report}))
    assert rows['preparation'].status=='unknown'
    assert rows['analysis'].status=='unknown'
    assert rows['evidence'].status=='unknown'


@pytest.mark.parametrize('synthesis,status',[('insufficient_evidence','partial'),('malformed_output','failed'),('request_failed','failed'),('budget_denied','blocked'),('not_started','unknown')])
def test_legacy_synthesis_outcomes_remain_distinct(result,synthesis,status):
    row=result.report.specialists[0].model_copy(update={'diagnostics':None,'synthesis':synthesis})
    output=result.model_copy(update={'report':result.report.model_copy(update={'specialists':(row,)})})
    assert stages(output)['analysis'].status==status


def test_empty_findings_does_not_claim_citation_validation(result):
    row=result.report.specialists[0]
    output=result.model_copy(update={'report':result.report.model_copy(update={'findings':(), 'specialists':(row.model_copy(update={'finding_count':0}),)})})
    stage=stages(output)['evidence']
    assert stage.status=='unknown' and 'No retained citations' in stage.detail


@pytest.mark.parametrize('category',['authorization','transmission_consumed','run_already_claimed'])
def test_authority_rejection_is_blocked_not_completed(result,category):
    output=result.model_copy(update={'category':category,'status':'blocked','usage':result.usage.model_copy(update={'model_attempts':0}), 'report':None,'persisted':False})
    rows=stages(output)
    assert rows['authorization'].status=='blocked'
    assert rows['report'].status=='unknown'


def test_report_persistence_failure_never_shows_completed(result):
    assert stages(result.model_copy(update={'persisted':False,'category':'storage_failure'}))['report'].status=='failed'


def test_stage_labels_closed_and_never_embed_result_values(result):
    rows=stages(result)
    assert all(s.status in presenter().STATUS_LABELS for s in rows.values())
    assert not any(str(result.run_id) in s.detail or 'value = 1' in s.detail for s in rows.values())


def test_theme_light_native_and_static_scoped_styles():
    from pathlib import Path
    import tomllib
    from northstar_codereview.ui_style import _STYLE
    cfg=tomllib.loads(Path('.streamlit/config.toml').read_text())
    assert cfg['theme']['base']=='light'
    assert cfg['theme']['backgroundColor']=='#F5F7FB'
    assert cfg['theme']['primaryColor']=='#0F766E'
    assert 'prefers-reduced-motion' in _STYLE
    from northstar_codereview.ui_theme import style_for
    assert 'linear-gradient' not in _STYLE and ':focus-visible' in style_for('Light')
    assert 'https://' not in _STYLE and 'unsafe_allow_html' not in _STYLE


def test_human_pipeline_requires_exact_report_and_persisted_decision(result):
    from uuid import uuid4
    from datetime import datetime,timezone
    from northstar_codereview.approval_models import ApprovalView,DecisionRecord
    report=result.report;thread=uuid4()
    pending=ApprovalView(thread_id=thread,status='pending',report=report)
    assert stages(result,human_review=pending)['human'].status=='blocked'
    for choice in ('approve','reject','request_changes'):
        decision=DecisionRecord.create(thread_id=thread,recorded_at=datetime.now(timezone.utc),
            review_id=report.review_id,report_revision=report.report_revision,report_hash=report.report_id,
            reviewer_id='test-reviewer',decision=choice)
        view=pending.model_copy(update={'status':'finalized','outcome':decision})
        stage=stages(result,human_review=view)['human']
        assert stage.status=='completed' and choice.replace('_',' ') in stage.detail
        assert 'publication' in stage.detail
        wrong=view.model_copy(update={'report':report.model_copy(update={'report_id':'0'*64})})
        assert stages(result,human_review=wrong)['human'].status=='unknown'


@pytest.mark.parametrize('category',['cancelled','deadline','cleanup_failed'])
def test_partial_execution_does_not_erase_completed_recorded_stages(result,category):
    output=result.model_copy(update={'status':'partial','category':category})
    assert stages(output)['report'].status=='completed'  # Persisted report, not successful execution.
    assert stages(output)['human'].status=='unknown'


def test_capacity_blocks_preparation_without_claiming_source_inspection(result):
    summary=result.report.specialists[0];a=summary.diagnostics.analysis
    denied=a.model_copy(update={'admission':'denied','denial_reasons':('input_reservation',)})
    output=result.model_copy(update={'capacity':denied,'report':None,'persisted':False,'category':'capacity'})
    assert stages(output)['preparation'].status=='blocked'
    assert stages(output)['source'].status=='unknown'
    assert stages(output)['analysis'].status=='unknown'


def test_primary_and_secondary_text_palette_contrast():
    def luminance(color):
        values=[int(color[i:i+2],16)/255 for i in (1,3,5)]
        return sum(v*c for v,c in zip([x/12.92 if x<=.04045 else ((x+.055)/1.055)**2.4 for x in values],(.2126,.7152,.0722)))
    def contrast(a,b):
        x,y=sorted((luminance(a),luminance(b)))
        return (y+.05)/(x+.05)
    for background in ('#F5F7FB','#FFFFFF'):
        assert contrast('#0F172A',background)>=4.5
        assert contrast('#475569',background)>=4.5
    assert contrast('#FFFFFF','#0F766E')>=4.5


@pytest.mark.parametrize('telemetry,status',[('incomplete','partial'),('missing','unknown')])
def test_successful_legacy_envelope_does_not_prove_complete_analysis(result,telemetry,status):
    row=result.report.specialists[0];d=row.diagnostics.model_copy(update={'analysis':None,'observed_lines':1,'fully_inspected_files':1}) if telemetry=='incomplete' else None
    row=row.model_copy(update={'diagnostics':d,'synthesis':'succeeded','status':'partial' if telemetry=='incomplete' else 'completed'})
    output=result.model_copy(update={'report':result.report.model_copy(update={'specialists':(row,)})})
    assert stages(output)['analysis'].status==status


def test_successful_legacy_envelope_with_complete_recorded_coverage(result):
    row=result.report.specialists[0]
    row=row.model_copy(update={'diagnostics':row.diagnostics.model_copy(update={'analysis':None})})
    output=result.model_copy(update={'report':result.report.model_copy(update={'specialists':(row,)})})
    assert stages(output)['analysis'].status=='completed'
