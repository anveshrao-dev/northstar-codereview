"""Native product controls over generated, validated review records only."""
import asyncio

import pytest

from .test_product_ui import screen
from .test_real_source import local_intake
from .test_real_review import service, captured, Fake, ENV, permit


@pytest.fixture(autouse=True)
def isolated(local_intake):
    pass


def saved(service, captured, provider=Fake):
    approval = permit(service, captured)
    return asyncio.run(service.run_review(
        'portfolio_risk', captured.approval_id,
        transmission_id=approval.approval_id, allow_live=True,
        environ=ENV, provider_factory=lambda *a: provider()))


def forbid_execution(service, monkeypatch):
    monkeypatch.setattr(service, 'run_review', lambda *a, **k: pytest.fail('inference started'))
    monkeypatch.setattr(service, 'authorize_transmission', lambda *a, **k: pytest.fail('approval created'))


def test_native_sidebar_routes_with_visible_active_label(service, monkeypatch):
    forbid_execution(service, monkeypatch)
    at = screen()
    destinations = ['Overview', 'New Review', 'Review History', 'Test Lab', 'Settings']
    assert [b.label for b in at.sidebar.button] == destinations
    assert not any(r.label == 'Navigation' for r in at.radio)
    for page in destinations:
        at.button(key='nav:' + page).click().run()
        assert not at.exception and not at.error
        assert at.session_state['navigation'] == page
        assert at.button(key='nav:' + page).proto.type == 'primary'
        assert any(c.value == 'Current page: ' + page for c in at.sidebar.caption)
        at.run()
        assert not at.exception


def test_overview_empty_metrics_are_zero_with_bounded_scope(service, monkeypatch):
    forbid_execution(service, monkeypatch)
    at = screen()
    assert {m.label: m.value for m in at.metric} == {
        'Total reviews': '0', 'Completed': '0', 'Need attention': '0'}
    assert any('available validated records' in c.value for c in at.caption)
    assert any('No available retained reviews' in i.value for i in at.info)
    assert not any(b.label == 'Open review' for b in at.button)


def test_overview_counts_overall_run_status_and_opens_saved_report(service, captured, monkeypatch):
    class CleanupFailure(Fake):
        async def __aexit__(self, *args):
            raise RuntimeError('PRIVATE_CLEANUP')
    saved(service, captured)
    partial = saved(service, captured, CleanupFailure)
    assert partial.status == 'partial' and partial.report.status == 'completed'
    forbid_execution(service, monkeypatch)
    at = screen()
    assert {m.label: m.value for m in at.metric} == {
        'Total reviews': '2', 'Completed': '1', 'Need attention': '1'}
    key = f'open:{partial.import_id}:{partial.run_id}'
    at.button(key=key).click().run()
    assert not at.exception
    assert at.session_state['navigation'] == 'Review History'
    assert any(t.value == 'Run ID: ' + str(partial.run_id) for t in at.text)
    assert next(m for m in at.metric if m.label == 'Review status').value == 'Partial'
    assert any('Incomplete review' in w.value for w in at.warning)
    assert at.get('download_button') and not at.code
    assert 'PRIVATE_CLEANUP' not in str(at)
    at.run()
    assert not at.exception


def test_each_history_entry_opens_exact_report_and_back_restores_list(service, captured, monkeypatch):
    first, second = saved(service, captured), saved(service, captured)
    forbid_execution(service, monkeypatch)
    at = screen()
    at.button(key='nav:Review History').click().run()
    assert len([b for b in at.button if b.label == 'Open review']) == 2
    assert not any(s.label == 'Retained review' for s in at.selectbox)
    assert any('filesystem metadata' in c.value for c in at.caption)
    assert any('Coverage:' in c.value for c in at.caption)
    for review in (first, second):
        at.button(key=f'open:{review.import_id}:{review.run_id}').click().run()
        assert not at.exception and not at.error
        assert any(t.value == 'Run ID: ' + str(review.run_id) for t in at.text)
        assert at.get('download_button')
        technical = [e for e in at.expander if e.label.startswith('Technical details')]
        assert technical and all(not e.proto.expanded for e in technical)
        next(b for b in at.button if b.label == 'Back to reviews').click().run()
        assert not at.get('download_button')
        assert len([b for b in at.button if b.label == 'Open review']) == 2


def test_identical_saved_review_metadata_has_distinct_visible_list_labels(service, captured):
    import os
    from northstar_codereview import real_source
    for _ in range(2):
        result = saved(service, captured)
        path = real_source._STORE / 'imports' / str(result.import_id) / 'review_artifacts' / f'{result.run_id}.json'
        os.utime(path, (1794052800, 1794052800))  # Generated test report only.
    at = screen()
    labels = [c.value for c in at.caption if c.value.startswith('Saved review ')]
    assert len(labels) == len(set(labels)) == 2
    assert labels[0].startswith('Saved review 1 ·')
    assert labels[1].startswith('Saved review 2 ·')
    buttons = [b for b in at.button if b.label == 'Open review']
    assert 'saved review 1' in buttons[0].proto.help
    assert 'saved review 2' in buttons[1].proto.help
    assert not at.exception and not at.error
