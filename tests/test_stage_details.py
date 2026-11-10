"""Sanitized terminal-report projections; no source or model message diagnostics."""
import pytest
from .test_real_source import local_intake
from .test_real_review import service, captured, Fake, ENV, permit
from .test_ui_pipeline import result


def test_all_stage_details_have_provenance_and_no_event_log(result):
    from northstar_codereview.ui_pipeline import recorded_stage_details, STAGES
    details = recorded_stage_details(result)
    assert set(details) == {key for key, _ in STAGES}
    assert all(d.evidence_source and d.unavailable for d in details.values())
    assert dict(details['analysis'].counts)['Analyzed lines'] == 2
    assert dict(details['report'].counts)['Retained findings'] == 1
    assert 'claim consumption' in ' '.join(details['authorization'].unavailable)
    text = repr(details)
    assert str(result.transmission_id) not in text and str(result.run_id) not in text
    assert 'value = 1' not in text and 'Live logs' not in text


def test_stage_details_ignore_free_text_and_legacy_missing_telemetry(result):
    from northstar_codereview.ui_pipeline import recorded_stage_details
    row = result.report.specialists[0].model_copy(update={'diagnostics':None,
        'limitations':('PRIVATE_SOURCE_OR_PROMPT',)})
    output = result.model_copy(update={'report':result.report.model_copy(update={'specialists':(row,),
        'limitations':('PRIVATE_SOURCE_OR_PROMPT',)})})
    details = recorded_stage_details(output)
    assert dict(details['analysis'].counts)['Analyzed lines'] is None
    assert 'PRIVATE_SOURCE_OR_PROMPT' not in repr(details)
    assert recorded_stage_details(None)['analysis'].counts == ()


def test_unit_details_are_bounded_and_closed(result):
    from northstar_codereview.ui_pipeline import recorded_stage_details
    details = recorded_stage_details(result)['analysis']
    assert len(details.diagnostics) <= 16
    assert any('schema=valid' in d for d in details.diagnostics)


def test_large_unit_metadata_panel_is_capped(result):
    from northstar_codereview.ui_pipeline import recorded_stage_details
    row=result.report.specialists[0];a=row.diagnostics.analysis
    units=tuple(a.units[0].model_copy(update={'unit_id':i}) for i in range(1,31))
    row=row.model_copy(update={'diagnostics':row.diagnostics.model_copy(update={'analysis':a.model_copy(update={'units':units})})})
    output=result.model_copy(update={'report':result.report.model_copy(update={'specialists':(row,)})})
    details=recorded_stage_details(output)['analysis']
    assert len(details.diagnostics)==12 and dict(details.counts)['Recorded units']==30


def test_preparation_counts_refer_to_recorded_ranges_not_inferred_success(result):
    from northstar_codereview.ui_pipeline import recorded_stage_details
    counts=dict(recorded_stage_details(result)['preparation'].counts)
    assert counts['Prepared files']==2 and counts['Prepared ranges']==2
    row=result.report.specialists[0]
    row=row.model_copy(update={'diagnostics':None})
    legacy=result.model_copy(update={'report':result.report.model_copy(update={'specialists':(row,)})})
    counts=dict(recorded_stage_details(legacy)['preparation'].counts)
    assert counts['Prepared files'] is None and counts['Prepared ranges'] is None
