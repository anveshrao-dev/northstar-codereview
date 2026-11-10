"""Real Streamlit smoke tests without a server, browser, credentials or network."""
from uuid import uuid4

import pytest
from streamlit.testing.v1 import AppTest

from northstar_codereview.local_source import PROJECT_ROOT
from northstar_codereview import streamlit_ui as ui
from northstar_codereview.ui_service import LocalReview
from northstar_codereview.ui_models import ReviewBundle


def app():
    at = AppTest.from_file(PROJECT_ROOT / 'streamlit_app.py', default_timeout=20).run()
    return at.button(key='nav:Test Lab').click().run()


def click(at, label):
    return next(b for b in at.button if b.label == label).click().run()


def test_initial_local_screen_has_no_execution_or_unsupported_actions():
    at = app()
    assert not at.exception and not at.error
    assert at.title[0].value == 'Test Lab'
    assert [b.label for b in at.main.button] == ['Run scripted review']
    assert all(w.disabled for w in at.text_input if w.label in ('GitHub owner', 'Repository'))
    assert not at.metric


@pytest.mark.parametrize('scenario', ['java_backend', 'angular_frontend', 'jenkins_aks'])
def test_actual_app_renders_all_scripted_scenarios(scenario):
    at = app()
    at.selectbox[0].set_value(scenario).run()
    click(at, 'Run scripted review')
    assert not at.exception and not at.error
    assert [tab.label for tab in at.tabs] == ['Summary', 'Pipeline', 'Findings', 'Coverage', 'Source Files', 'Human Review', 'Technical Details']
    assert any('SCRIPTED INTEGRATION' in info.value for info in at.info)
    assert any('Claim: unverified' in text.value for text in at.text)
    assert not any('Publish' in b.label or 'Run live' in b.label for b in at.button)
    assert any('Code Quality' in m.value or 'GitOps' in m.value for m in at.markdown)


def test_scenario_switch_does_not_display_previous_review():
    at = app()
    click(at, 'Run scripted review')
    at.selectbox[0].set_value('angular_frontend').run()
    assert not at.metric and not at.exception


def test_saved_live_empty_catalog_has_no_execution_button(monkeypatch):
    monkeypatch.setattr(ui, 'saved_live', lambda _: ())
    at = app()
    next(r for r in at.radio if r.label=='Evaluation mode').set_value('Saved live results').run()
    assert not at.main.button and not at.exception
    assert any('No compatible validated live artifact' in item.value for item in at.info)


def test_local_rejection_uses_checkpoint_without_rerunning_graph(monkeypatch):
    database = PROJECT_ROOT / '.runtime' / f'ui-smoke-{uuid4()}.sqlite3'
    monkeypatch.setattr(ui, 'LocalReview', lambda: LocalReview(database=database))
    runs = []
    original = ui.run_baseline
    def counted(scenario):
        runs.append(scenario)
        return original(scenario)
    monkeypatch.setattr(ui, 'run_baseline', counted)
    try:
        at = app()
        click(at, 'Run scripted review')
        click(at, 'Start human review')
        assert not at.error
        next(s for s in at.selectbox if s.label=='Local report decision').set_value('reject').run()
        click(at, 'Record manual decision')  # Explicit local choice, never automatic.
        assert not at.exception and not at.error
        assert any('Manual decision: reject' in item.value for item in at.text)
        assert runs == ['java_backend']
    finally:
        for suffix in ('', '-wal', '-shm', '-journal'):
            path = database.with_name(database.name + suffix)
            if path.exists(): path.unlink()


def test_raw_internal_errors_never_reach_page(monkeypatch):
    def fail(_): raise RuntimeError('PRIVATE_SECRET_RESPONSE')
    monkeypatch.setattr(ui, 'run_baseline', fail)
    at = app()
    click(at, 'Run scripted review')
    assert not at.exception and len(at.error) == 1
    assert 'PRIVATE_SECRET_RESPONSE' not in at.error[0].value


def test_recorded_live_result_keeps_provenance_and_partial_warning(monkeypatch):
    import asyncio
    from northstar_codereview.evaluation_runner import run_scripted
    from northstar_codereview.evaluation_data import load_dataset
    from northstar_codereview.evaluation import evaluate
    dataset = load_dataset()
    run = asyncio.run(run_scripted(dataset, 'java_backend', mode='recovery'))
    evaluation = evaluate(dataset, 'java_backend', run.report, configuration_id='0'*64, run_kind='external_review')
    bundle = ReviewBundle(scenario_id='java_backend', provenance='recorded_live',
        report=run.report, evaluation=evaluation, artifact_name='0'*64+'.json', run_status='partial')
    monkeypatch.setattr(ui, 'saved_live', lambda _: (bundle,))
    at = app()
    next(r for r in at.radio if r.label=='Evaluation mode').set_value('Saved live results').run()
    click(at, 'Load saved live result')
    assert not at.exception and not at.error
    assert any('RECORDED NEBIUS LIVE RESULT' in item.value for item in at.info)
    assert any('Incomplete review' in item.value for item in at.warning)
    assert not any('SCRIPTED FALSE-POSITIVE' in item.value for item in at.warning)
    assert not any('Run scripted review' == b.label for b in at.button)


def test_partial_approval_requires_acknowledgment(monkeypatch):
    import asyncio
    from northstar_codereview.evaluation_runner import run_scripted
    from northstar_codereview.evaluation_data import load_dataset
    run = asyncio.run(run_scripted(load_dataset(), 'java_backend', mode='recovery'))
    bundle = ReviewBundle(scenario_id='java_backend', provenance='scripted_integration',
                          report=run.report, evaluation=run.evaluation)
    monkeypatch.setattr(ui, 'run_baseline', lambda _: bundle)
    database = PROJECT_ROOT / '.runtime' / f'ui-partial-{uuid4()}.sqlite3'
    monkeypatch.setattr(ui, 'LocalReview', lambda: LocalReview(database=database))
    try:
        at = app()
        click(at, 'Run scripted review')
        click(at, 'Start human review')
        button = next(b for b in at.button if b.label == 'Record manual decision')
        assert button.disabled and not at.checkbox[0].value
        at.checkbox[0].check().run()
        assert next(b for b in at.button if b.label=='Record manual decision').disabled
        next(s for s in at.selectbox if s.label=='Local report decision').set_value('reject').run()
        assert not next(b for b in at.button if b.label == 'Record manual decision').disabled
        assert not at.exception and not at.error
    finally:
        for suffix in ('', '-wal', '-shm', '-journal'):
            path = database.with_name(database.name + suffix)
            if path.exists(): path.unlink()


@pytest.fixture(autouse=True)
def isolate_real_history(tmp_path,monkeypatch):
    from northstar_codereview import real_source
    monkeypatch.setattr(real_source,'_STORE',tmp_path/'private-store')
