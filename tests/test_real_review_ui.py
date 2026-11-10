"""Streamlit real-source actions require clicks; generated data only."""
import asyncio
import json

import pytest
from streamlit.testing.v1 import AppTest

from northstar_codereview.local_source import PROJECT_ROOT
from northstar_codereview import real_source as intake
from .test_real_review import service, captured, Fake, ENV
from .test_real_source import local_intake


def app():
    at = AppTest.from_file(PROJECT_ROOT / 'streamlit_app.py', default_timeout=30).run()
    assert any(b.label=='New Review' for b in at.sidebar.button), 'real-source UI workflow missing'
    return at.button(key='nav:New Review').click().run()


def button(at, label): return next(b for b in at.button if b.label == label)
def click(at, label): return button(at, label).click().run()
def check(at, label): return next(c for c in at.checkbox if c.label == label).check().run()


def test_real_page_selection_never_reads_credentials_or_starts_run(service, captured, monkeypatch):
    monkeypatch.setattr(service, 'load_nebius_settings', lambda *a: pytest.fail('credentials read'))
    monkeypatch.setattr(service, 'run_review', lambda *a, **k: pytest.fail('run started'))
    at = app()
    assert not at.exception and not at.error
    assert button(at, 'Authorize model transmission').disabled
    assert button(at, 'Start live real-code review').disabled
    assert any('Qwen/Qwen3-30B-A3B-Instruct-2507' in t.value for t in at.text)
    assert not list((intake._STORE / 'imports' / str(captured.approval_id) / 'receipts').glob('transmission-*'))


def test_explicit_consent_run_and_rerun_do_not_repeat(service, captured, monkeypatch):
    original = service.run_review
    calls = []
    async def fake_run(*a, **kw):
        calls.append(True)
        return await original(*a, **kw, environ=ENV, provider_factory=lambda *a: Fake())
    monkeypatch.setattr(service, 'run_review', fake_run)
    at = app()
    check(at, 'I authorize disclosure of this exact snapshot to Nebius for code review.')
    click(at, 'Authorize model transmission')
    assert not calls
    check(at, 'I authorize one bounded live review now.')
    click(at, 'Start live real-code review')
    assert not at.exception and not at.error and len(calls) == 1
    assert any('REAL-SOURCE NEBIUS REVIEW' in i.value for i in at.info)
    assert any('Claim: unverified' in t.value for t in at.text)
    assert any('Observed source: 2/2 files fully inspected' in t.value for t in at.text)
    assert any('Prepared context:' in t.value for t in at.text)
    assert at.get('download_button')
    at.run()
    assert len(calls) == 1
    assert button(at, 'Start live real-code review').disabled


def test_sample_switch_does_not_reuse_consent(service, captured):
    at = app()
    check(at, 'I authorize disclosure of this exact snapshot to Nebius for code review.')
    click(at, 'Authorize model transmission')
    next(s for s in at.selectbox if s.label == 'Choose an approved source snapshot').set_value('csv_import').run()
    assert not any(b.label == 'Start live real-code review' and not b.disabled for b in at.button)
    assert not at.exception


def test_missing_configuration_is_sanitized_blocked_state(service, captured, monkeypatch):
    def missing(*a): raise ValueError('PRIVATE_CREDENTIAL_VALUE')
    monkeypatch.setattr(service, 'load_nebius_settings', missing)
    at = app()
    check(at, 'I authorize disclosure of this exact snapshot to Nebius for code review.')
    click(at, 'Authorize model transmission')
    check(at, 'I authorize one bounded live review now.')
    click(at, 'Start live real-code review')
    assert not at.exception
    assert any('configuration' in t.value for t in at.text)
    assert 'PRIVATE_CREDENTIAL_VALUE' not in str(at)


def test_explicit_ui_renewal_preserves_old_bundle(service, captured):
    at = app()
    at.button(key='nav:Settings').click().run()
    assert button(at, 'Renew local retention').disabled
    check(at, 'I authorize a local retention replacement; no transmission approval is extended.')
    click(at, 'Renew local retention')
    assert not at.error and not at.exception
    assert len(service.snapshot_catalog('portfolio_risk')) == 2
    assert intake.load_import(captured.approval_id).real_source.import_approval == captured


def test_cleanup_failure_shows_partial_execution_despite_completed_report(service,captured,monkeypatch):
    original=service.run_review
    class CleanupFailure(Fake):
        async def __aexit__(self,*args):raise RuntimeError('PRIVATE_CLEANUP')
    async def fake_run(*args,**kwargs):
        return await original(*args,**kwargs,environ=ENV,provider_factory=lambda *a:CleanupFailure())
    monkeypatch.setattr(service,'run_review',fake_run)
    at=app()
    check(at,'I authorize disclosure of this exact snapshot to Nebius for code review.')
    click(at,'Authorize model transmission')
    check(at,'I authorize one bounded live review now.')
    click(at,'Start live real-code review')
    assert not at.exception
    assert any('Overall execution: partial' in t.value for t in at.text)
    assert any('Report completion does not establish successful execution cleanup' in w.value for w in at.warning)
    assert 'PRIVATE_CLEANUP' not in str(at)


def test_retention_expiry_blocks_saved_result_and_export(service,captured,monkeypatch):
    from northstar_codereview import real_source_contracts as contracts
    from datetime import datetime,timedelta
    original=service.run_review
    async def fake_run(*a,**kw):return await original(*a,**kw,environ=ENV,provider_factory=lambda *a:Fake())
    monkeypatch.setattr(service,'run_review',fake_run)
    at=app()
    check(at,'I authorize disclosure of this exact snapshot to Nebius for code review.')
    click(at,'Authorize model transmission')
    check(at,'I authorize one bounded live review now.')
    click(at,'Start live real-code review')
    assert at.get('download_button')
    class Later(datetime):
        @classmethod
        def now(cls,tz=None):return captured.manifest.retain_until+timedelta(seconds=1)
    monkeypatch.setattr(contracts,'datetime',Later)
    at.run()
    assert not at.get('download_button') and not at.metric
    assert any('viewing and export are blocked' in e.value for e in at.error)
