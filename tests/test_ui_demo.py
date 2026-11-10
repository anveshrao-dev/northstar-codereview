"""Demo labels derive from report-bound evaluation metadata, never model prose."""
import pytest
from northstar_codereview.ui_service import run_baseline
from northstar_codereview.ui_models import present_review, ReviewBundle
from northstar_codereview.evaluation import evaluate
from northstar_codereview.evaluation_data import load_dataset


@pytest.fixture(scope='module')
def baseline():
    return run_baseline('java_backend')


def test_scripted_false_positive_is_labeled_without_changing_findings(baseline):
    original = baseline.report.model_dump_json()
    view = present_review(baseline)
    assert len(view.findings) == len(baseline.report.findings) == 2
    expected = {f.finding_id for f in baseline.evaluation.findings
                if f.disposition == 'false_positive' and f.judgment_origin == 'scripted_fixture'}
    assert {f.finding_id for f in view.findings if f.scripted_case == 'false_positive'} == expected
    assert len(expected) == 1
    assert all(f.specialist and f.claim_status == 'unverified' for f in view.findings)
    assert [f.finding_id for f in view.findings] == [f.finding_id for f in baseline.report.findings]
    assert baseline.report.model_dump_json() == original
    assert any(c.status == 'violated' for c in view.controls)


@pytest.mark.parametrize('origin', [None, 'human'])
def test_false_positive_without_fixture_origin_is_not_deliberate_test_case(baseline, origin):
    data = baseline.model_dump(mode='json')
    for f in data['evaluation']['findings']: f['judgment_origin'] = origin
    view = present_review(ReviewBundle.model_validate(data))
    assert all(f.scripted_case is None for f in view.findings)


def test_live_or_missing_evaluation_cannot_inherit_scripted_false_positive(baseline):
    evaluation = evaluate(load_dataset(), 'java_backend', baseline.report,
                          configuration_id='0'*64, run_kind='external_review')
    live = ReviewBundle(scenario_id='java_backend', provenance='recorded_live',
                        report=baseline.report, evaluation=evaluation)
    assert all(f.scripted_case is None for f in present_review(live).findings)
    no_evaluation = ReviewBundle(scenario_id='java_backend', provenance='scripted_integration', report=baseline.report)
    assert all(f.scripted_case is None for f in present_review(no_evaluation).findings)


def test_judgment_for_different_finding_cannot_label_a_card(baseline):
    from uuid import uuid4
    data = baseline.model_dump(mode='json')
    data['evaluation']['findings'][0]['finding_id'] = str(uuid4())
    with pytest.raises(ValueError): ReviewBundle.model_validate(data)


@pytest.mark.parametrize('field,value', [('category', 'security'), ('severity', 'critical')])
def test_mismatched_assessment_attributes_are_rejected(baseline, field, value):
    data = baseline.model_dump(mode='json')
    data['evaluation']['findings'][0][field] = value
    with pytest.raises(ValueError): ReviewBundle.model_validate(data)


def test_duplicate_assessment_cannot_duplicate_or_relabel_a_finding(baseline):
    data = baseline.model_dump(mode='json')
    data['evaluation']['findings'].append(data['evaluation']['findings'][0])
    with pytest.raises(ValueError): ReviewBundle.model_validate(data)


def test_demo_page_explains_false_positive_and_retains_both_findings(baseline, monkeypatch):
    from .test_streamlit_ui import app, click
    from northstar_codereview import streamlit_ui as ui
    monkeypatch.setattr(ui, 'run_baseline', lambda _: baseline)
    at = app()
    click(at, 'Run scripted review')
    assert not at.exception and not at.error
    assert 'SCRIPTED INTEGRATION' in at.info[0].value
    assert sum('SCRIPTED FALSE-POSITIVE TEST CASE' in w.value for w in at.warning) == 1
    assert any('SCRIPTED NEGATIVE-CONTROL VIOLATION' in w.value for w in at.warning)
    for finding in baseline.report.findings:
        assert sum(t.value == finding.title for t in at.text) == 1
        assert sum(t.value == finding.suggested_remediation for t in at.text) >= 1
    assert sum('Claim: unverified' in t.value for t in at.text) == 2
    assert len([e for e in at.expander if e.label.startswith('Inspection details')]) == 3
    assert any(b.label == 'Start human review' for b in at.button)
    assert any('Reject report declines' in m.value and 'Request follow-up' in m.value for m in at.markdown)


def test_missing_judgments_never_generate_demo_warning(baseline, monkeypatch):
    from .test_streamlit_ui import app, click
    from northstar_codereview import streamlit_ui as ui
    missing = ReviewBundle(scenario_id='java_backend', provenance='scripted_integration', report=baseline.report)
    monkeypatch.setattr(ui, 'run_baseline', lambda _: missing)
    at = app()
    click(at, 'Run scripted review')
    assert not at.exception and not at.error
    assert not any('FALSE-POSITIVE TEST CASE' in w.value for w in at.warning)
    assert sum('Claim: unverified' in t.value for t in at.text) == 2


def test_unavailable_github_controls_are_collapsed():
    from .test_streamlit_ui import app
    at = app()
    at.button(key='nav:Settings').click().run()
    expander = next(e for e in at.expander if e.label == 'GitHub integration · unavailable')
    assert not expander.proto.expanded
    assert all(w.disabled for w in at.text_input if w.label in ('GitHub owner', 'Repository'))


def test_model_markup_is_plain_text_not_a_source_of_judgments(baseline, monkeypatch):
    from .test_streamlit_ui import app, click
    from northstar_codereview import streamlit_ui as ui
    from northstar_codereview.reporting import _digest
    data = baseline.report.model_dump(mode='json')
    title = '<script>deliberate false positive</script>'
    data['findings'][0]['title'] = title
    data['report_id'] = _digest({k:v for k,v in data.items() if k != 'report_id'})
    bundle = ReviewBundle(scenario_id='java_backend', provenance='scripted_integration', report=data)
    monkeypatch.setattr(ui, 'run_baseline', lambda _: bundle)
    at = app()
    click(at, 'Run scripted review')
    assert not at.exception and not at.error
    assert any(t.value == title for t in at.text)
    assert not any(title in m.value for m in at.markdown)
    assert not any('FALSE-POSITIVE TEST CASE' in w.value for w in at.warning)


@pytest.fixture(autouse=True)
def isolate_real_history(tmp_path,monkeypatch):
    from northstar_codereview import real_source
    monkeypatch.setattr(real_source,'_STORE',tmp_path/'private-store')
