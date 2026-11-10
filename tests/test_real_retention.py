"""Retention replacement uses generated source only, never original projects."""
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from northstar_codereview import real_source as intake
from northstar_codereview.real_source_contracts import IntakeRejected
from .test_real_source import local_intake, approval_for, transmission_for


@pytest.fixture
def captured(local_intake):
    receipt = approval_for()
    intake.import_sample(receipt.manifest.sample, approval=receipt)
    return receipt


def renew(receipt, **kw):
    assert callable(getattr(intake, 'renew_import', None)), 'supported renewal operation missing'
    return intake.renew_import(receipt.approval_id, expected_digest=receipt.manifest.digest,
                               allow_renewal=True, **kw)


def test_renewal_new_identity_receipt_preserves_bytes_and_prior_bundle(captured):
    old = intake.load_import(captured.approval_id)
    before = captured.model_dump_json()
    result = renew(captured)
    new = intake.load_import(result.approval_id)
    assert result.approval_id != captured.approval_id
    assert new.manifest == old.manifest and new.sources == old.sources
    assert intake.load_import(captured.approval_id).real_source.import_approval.model_dump_json() == before
    receipt = new.real_source.import_approval
    assert timedelta(days=29) < receipt.manifest.retain_until - datetime.now(timezone.utc) <= timedelta(days=30)
    assert receipt.manifest.digest != captured.manifest.digest
    receipts = intake._STORE / 'imports' / str(result.approval_id) / 'receipts'
    audit = json.loads((receipts / 'renewal.json').read_text())
    assert audit['previous_import_id'] == str(captured.approval_id)
    assert audit['previous_manifest_digest'] == captured.manifest.digest
    assert not list(receipts.glob('transmission-*'))
    from northstar_codereview.real_source_authorization import require_transmission
    with pytest.raises(IntakeRejected):
        require_transmission(new, transmission_for(captured))


def test_expired_snapshot_remains_blocked_until_explicit_replacement(captured, monkeypatch):
    from northstar_codereview import real_source_contracts as contracts
    future = captured.manifest.retain_until + timedelta(days=1)
    class Later(datetime):
        @classmethod
        def now(cls, tz=None): return future
    monkeypatch.setattr(contracts, 'datetime', Later)
    monkeypatch.setattr(intake, 'datetime', Later, raising=False)
    with pytest.raises(IntakeRejected): intake.load_import(captured.approval_id)
    result = renew(captured, days=7)
    assert intake.load_import(result.approval_id).manifest == captured.manifest.snapshot
    with pytest.raises(IntakeRejected): intake.load_import(captured.approval_id)


@pytest.mark.parametrize('days', [0, -1, 366, True, 1.5, '30'])
def test_invalid_retention_creates_nothing(captured, days):
    before = set((intake._STORE / 'imports').iterdir())
    with pytest.raises((IntakeRejected, ValueError)): renew(captured, days=days)
    assert set((intake._STORE / 'imports').iterdir()) == before


def test_no_implicit_renewal_or_stale_digest(captured):
    assert callable(getattr(intake, 'renew_import', None)), 'supported renewal operation missing'
    with pytest.raises(IntakeRejected):
        intake.renew_import(captured.approval_id, expected_digest=captured.manifest.digest)
    with pytest.raises(IntakeRejected):
        intake.renew_import(captured.approval_id, expected_digest='0'*64, allow_renewal=True)


def test_tampered_expired_source_cannot_be_renewed(captured):
    path = intake._STORE / 'imports' / str(captured.approval_id) / 'snapshots/source.json'
    content = json.loads(path.read_text())
    content[next(iter(content))] = 'changed\n'
    path.write_text(json.dumps(content))
    with pytest.raises(IntakeRejected): renew(captured)


def test_renewal_never_opens_original_source(captured, monkeypatch):
    monkeypatch.setattr(intake, '_capture', lambda *a: pytest.fail('original source access'))
    assert renew(captured).status == 'committed'


def test_failed_renewal_removes_only_its_staging_bundle(captured,monkeypatch):
    old_imports=set((intake._STORE/'imports').iterdir())
    old_claims=set((intake._STORE/'claims').iterdir())
    original=intake._write
    def fail_audit(parent,name,payload):
        if name=='renewal.json':raise OSError('synthetic failure')
        return original(parent,name,payload)
    monkeypatch.setattr(intake,'_write',fail_audit)
    with pytest.raises(IntakeRejected):renew(captured)
    assert set((intake._STORE/'imports').iterdir())==old_imports
    assert set((intake._STORE/'claims').iterdir())==old_claims
    assert intake.load_import(captured.approval_id).real_source.import_approval==captured


def test_revoked_archive_cannot_be_renewed(captured):
    path=intake._STORE/'imports'/str(captured.approval_id)/'receipts/import.json'
    record=json.loads(path.read_text())
    record['state']='revoked'
    path.write_text(json.dumps(record))
    with pytest.raises(IntakeRejected):renew(captured)
