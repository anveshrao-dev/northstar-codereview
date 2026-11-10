"""Visual navigation and state consistency with generated source and fake providers."""
import re

import pytest

from .test_enterprise_ui import screen
from .test_real_source import local_intake
from .test_real_review import service, captured, Fake, ENV, permit
from .test_ui_pipeline import result


@pytest.fixture(autouse=True)
def isolated(local_intake): pass


def graphics(scope):
    return [h.proto.body for h in scope.get('html') if '<ol ' in h.proto.body]


def states(html):
    return re.findall(r'data-stage="([a-z_]+)" data-status="([a-z_]+)"', html)


def test_overview_history_and_detail_share_stage_states(result, service, monkeypatch):
    monkeypatch.setattr(service, 'run_review', lambda *a, **k: pytest.fail('inference'))
    at = screen()
    overview = graphics(at)
    assert len(overview) == 1
    assert any('Imported source sample — Portfolio Risk' in s.value for s in at.subheader)
    next(b for b in at.button if b.label == 'View existing reviews').click().run()
    assert states(graphics(at)[0]) == states(overview[0])
    next(b for b in at.button if b.label == 'Open review').click().run()
    assert [t.label for t in at.tabs] == ['Summary','Pipeline','Findings','Coverage','Source Files','Human Review','Technical Details']
    assert len(graphics(at.tabs[0])) == 1 and len(graphics(at.tabs[1])) == 0
    from .test_review_walkthrough import native_states
    assert states(graphics(at.tabs[0])[0]) == native_states(at.tabs[1]) == states(overview[0])
    assert ('human','not_started') in states(overview[0])
    assert len(at.get('download_button')) == 1
    assert not at.exception and not at.error and not at.code


def test_missing_checkpoint_and_unreadable_checkpoint_do_not_conflate(result, monkeypatch):
    from northstar_codereview import real_review_ui
    from northstar_codereview.approval_models import ApprovalError
    class Unavailable:
        def thread_for(self, report): return report.review_id
        def get(self, *args): raise ApprovalError('storage_error')
    monkeypatch.setattr(real_review_ui,'LocalReview',Unavailable)
    stages, decision = real_review_ui.saved_pipeline_state(result)
    assert stages[-1].status == 'unknown' and decision is None


def test_selected_initialization_failure_is_not_labeled_executed():
    import asyncio
    from streamlit.testing.v1 import AppTest
    from northstar_codereview.evaluation_data import load_dataset
    from northstar_codereview.evaluation_runner import run_scripted
    from northstar_codereview.ui_models import ReviewBundle
    run = asyncio.run(run_scripted(load_dataset(), 'java_backend', mode='failure'))
    bundle = ReviewBundle(scenario_id='java_backend', provenance='scripted_integration',
                          report=run.report, evaluation=run.evaluation, run_status='failed')
    def render(value):
        from northstar_codereview.streamlit_ui import _results
        _results(value)
    at = AppTest.from_function(render,args=(bundle,)).run()
    assert not at.exception
    assert any('Selected specialists:' in c.value for c in at.caption)
    assert not any('Specialist executed:' in c.value for c in at.caption)


def test_source_selection_label_and_separate_consent(captured, service, monkeypatch):
    monkeypatch.setattr(service, 'run_review', lambda *a, **k: pytest.fail('inference'))
    at = screen(); at.button(key='nav:New Review').click().run()
    choice = next(s for s in at.selectbox if s.label == 'Choose an approved source snapshot')
    assert choice.value == 'portfolio_risk'
    assert all('Imported source sample' in option for option in choice.options)
    assert any('not CSV datasets' in c.value for c in at.caption)
    next(c for c in at.checkbox if c.label.startswith('I authorize disclosure')).check().run()
    assert next(b for b in at.button if b.label == 'Start live real-code review').disabled
    assert not next(c for c in at.checkbox if c.label == 'I authorize one bounded live review now.').value


def test_failed_scripted_attempt_clears_previous_results(service, monkeypatch):
    from northstar_codereview import streamlit_ui
    at = screen(); at.button(key='nav:Test Lab').click().run()
    next(b for b in at.button if b.label == 'Run scripted review').click().run()
    assert at.tabs and not at.exception
    def fail(*args): raise ValueError('PRIVATE_PROVIDER_BODY')
    monkeypatch.setattr(streamlit_ui,'run_baseline',fail)
    next(b for b in at.button if b.label == 'Run scripted review').click().run()
    assert not at.tabs and not at.metric
    assert any('FAILED' in h.proto.body for h in at.get('html'))
    assert not any('PRIVATE_PROVIDER_BODY' in str(e.value) for e in at if hasattr(e,'value'))
    assert not at.exception
