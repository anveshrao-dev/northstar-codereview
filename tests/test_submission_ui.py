"""Submission-facing clarity using generated records and offline providers only."""
import asyncio

import pytest

from .test_enterprise_ui import screen
from .test_real_source import local_intake
from .test_real_review import service, captured, Fake, ENV, permit


@pytest.fixture(autouse=True)
def isolated(local_intake):
    pass


def test_dashboard_purpose_and_saved_review_actions(service, monkeypatch):
    monkeypatch.setattr(service, 'run_review', lambda *a, **k: pytest.fail('inference'))
    monkeypatch.setattr(service, 'authorize_transmission', lambda *a, **k: pytest.fail('authorization'))
    at = screen()
    assert at.title[0].value == 'Northstar CodeReview'
    assert any('AI-Powered Multi-Agent Code Review' in c.value for c in at.caption)
    assert any('approved imported source-code snapshots' in w.value for w in at.markdown)
    guide = next(e for e in at.expander if e.label == 'Explore the review pipeline')
    assert not guide.proto.expanded
    assert len(guide.markdown) == 7
    assert any('process guide' in c.value for c in guide.caption)
    next(b for b in at.button if b.label == 'View existing reviews').click().run()
    assert at.session_state['navigation'] == 'Review History'
    assert not at.exception and not at.error


def test_new_review_plain_steps_and_advanced_limits(captured, service, monkeypatch):
    monkeypatch.setattr(service, 'run_review', lambda *a, **k: pytest.fail('inference'))
    at = screen()
    at.button(key='nav:New Review').click().run()
    headings = [s.value for s in at.subheader]
    for label in ('1. Choose code', '2. Select reviewer', '3. Approve code sharing', '4. Run review'):
        assert label in headings
    advanced = next(e for e in at.expander if e.label == 'Advanced · execution profile and limits')
    assert not advanced.proto.expanded
    assert next(s for s in advanced.selectbox if s.label == 'Execution profile').value == 'standard'
    assert not any(c.value for c in at.checkbox)
    assert next(b for b in at.button if b.label == 'Start live real-code review').disabled
    assert next(b for b in at.button if b.label == 'Authorize model transmission').disabled
    assert any('separate' in c.value.lower() for c in at.caption)
    assert not at.exception


def test_test_lab_distinguishes_design_testing_and_live_scope(service, monkeypatch):
    monkeypatch.setattr(service, 'run_review', lambda *a, **k: pytest.fail('inference'))
    at = screen()
    at.button(key='nav:Test Lab').click().run()
    capability = next(e for e in at.expander if e.label == 'Five specialist capabilities')
    assert sum('Implemented · Offline tested' in t.value for t in capability.text) == 5
    assert sum('Live real-source workflow: supported' == c.value for c in capability.caption) == 1
    assert sum('Live real-source workflow: not available' == c.value for c in capability.caption) == 4
    assert not any(b.label == 'Start live real-code review' for b in at.button)
    assert not at.exception


def test_dashboard_history_action_returns_to_list_after_opening_detail(service, captured, monkeypatch):
    approval = permit(service, captured)
    asyncio.run(service.run_review('portfolio_risk', captured.approval_id,
        transmission_id=approval.approval_id, allow_live=True, environ=ENV,
        provider_factory=lambda *a: Fake()))
    monkeypatch.setattr(service, 'run_review', lambda *a, **k: pytest.fail('inference'))
    at = screen()
    next(b for b in at.button if b.label == 'Open review').click().run()
    assert any(b.label == 'Back to reviews' for b in at.button)
    at.button(key='nav:Overview').click().run()
    next(b for b in at.button if b.label == 'View existing reviews').click().run()
    assert any(b.label == 'Open review' for b in at.button)
    assert not any(b.label == 'Back to reviews' for b in at.button)
    assert not at.exception


@pytest.mark.parametrize('response, expected', [('{"findings":[]}', 'Completed'), ('{}', 'Partial')])
def test_saved_zero_finding_status_and_summary_order(service, captured, monkeypatch, response, expected):
    from langchain_core.messages import AIMessage

    class Output(Fake):
        async def ainvoke(self, messages, **kwargs):
            return AIMessage(content=response, response_metadata={'finish_reason': 'stop'})

    approval = permit(service, captured)
    result = asyncio.run(service.run_review('portfolio_risk', captured.approval_id,
        transmission_id=approval.approval_id, allow_live=True, environ=ENV,
        provider_factory=lambda *a: Output()))
    monkeypatch.setattr(service, 'run_review', lambda *a, **k: pytest.fail('inference'))
    at = screen()
    at.button(key='nav:Review History').click().run()
    assert any('Imported demonstration source code' in c.value for c in at.caption)
    assert any(expected + ' · Zero findings' in c.value for c in at.caption)
    assert any('not proof of defect-free code' in c.value for c in at.caption)
    next(b for b in at.button if b.label == 'Open review').click().run()
    assert next(m for m in at.metric if m.label == 'Review status').value == expected
    assert next(m for m in at.metric if m.label == 'Evidence-validated findings').value == '0'
    assert len(at.get('download_button')) == 1
    assert not at.code and not at.exception
    # Summary precedes the detailed seven-stage display; no event or source text fabricated.
    rendered = list(at.main)
    metric_index = next(i for i, e in enumerate(rendered) if e.type == 'metric' and e.label == 'Review status')
    pipeline_index = next(i for i, e in enumerate(rendered) if e.type == 'subheader' and e.value == 'Review pipeline')
    assert metric_index < pipeline_index
    advanced = next(e for e in at.expander if e.label == 'Technical details · run')
    assert not advanced.proto.expanded
    assert any('Execution profile:' in t.value for t in advanced.text)
    assert any('Run ID: ' + str(result.run_id) == t.value for t in advanced.text)
