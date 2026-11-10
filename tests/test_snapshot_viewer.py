"""Read-only local viewing over generated retained imports, never original projects."""
import json
from uuid import uuid4
import pytest
from .test_real_source import local_intake
from .test_real_review import service, captured, Fake, ENV, permit
from .test_ui_pipeline import result
from northstar_codereview.real_source_contracts import IntakeRejected


def test_viewer_reads_uncited_file_without_provider_or_source_reimport(result, service, local_intake, monkeypatch, capsys):
    _, intake = local_intake
    def forbidden(*args, **kwargs): pytest.fail('unauthorized action')
    monkeypatch.setattr(service,'NebiusModel',forbidden)
    monkeypatch.setattr(service,'authorize_transmission',forbidden)
    monkeypatch.setattr(intake,'_capture',forbidden)
    before = {p:p.read_bytes() for p in intake._STORE.rglob('*') if p.is_file()}
    files = service.source_file_catalog(result.import_id,result.run_id)
    assert len(files) == 2 and all(f.line_count == 1 for f in files)
    text = service.source_excerpt(result.import_id,result.run_id,file_path='tests/test_risk.py',start_line=1,end_line=1)
    assert text == 'value = 1\r\n'
    assert service.load_review(result.import_id,result.run_id) == result
    assert before == {p:p.read_bytes() for p in intake._STORE.rglob('*') if p.is_file()}
    assert 'value = 1' not in capsys.readouterr().out


@pytest.mark.parametrize('path,start,end', [('../risk.py',1,1),('/etc/passwd',1,1),('.env',1,1),
    ('not-approved.py',1,1),('tests/test_risk.py',0,1),('tests/test_risk.py',1,101),
    ('tests/test_risk.py',2,1),('tests/test_risk.py',1,2),('tests/test_risk.py',True,1)])
def test_viewer_rejects_invalid_scope_and_bounds(result,service,path,start,end):
    with pytest.raises(IntakeRejected):
        service.source_excerpt(result.import_id,result.run_id,file_path=path,start_line=start,end_line=end)


def test_expired_snapshot_rechecked_on_each_read(result,service,captured,monkeypatch):
    from datetime import datetime,timedelta
    from northstar_codereview import real_source_contracts as contracts
    assert service.source_file_catalog(result.import_id,result.run_id)
    class Later(datetime):
        @classmethod
        def now(cls,tz=None): return captured.manifest.retain_until+timedelta(seconds=1)
    monkeypatch.setattr(contracts,'datetime',Later)
    with pytest.raises(IntakeRejected):
        service.source_excerpt(result.import_id,result.run_id,file_path='tests/test_risk.py',start_line=1,end_line=1)


@pytest.mark.parametrize('kind',['modified','symlink','missing'])
def test_viewer_rejects_invalid_storage(result,service,local_intake,kind):
    _, intake = local_intake
    source = intake._STORE/'imports'/str(result.import_id)/'snapshots/source.json'
    original=source.read_bytes()
    if kind=='modified': source.write_bytes(original.replace(b'value = 1',b'value = 2'))
    else:
        source.unlink()
        if kind=='symlink':
            outside=intake._STORE/'synthetic-source.json';outside.write_bytes(original);source.symlink_to(outside)
    with pytest.raises(IntakeRejected): service.source_file_catalog(result.import_id,result.run_id)


def test_viewer_rejects_mismatched_run_and_output_limit(result,service,monkeypatch):
    from northstar_codereview import evidence_tools
    with pytest.raises((IntakeRejected,ValueError)):
        service.source_file_catalog(result.import_id,uuid4())
    monkeypatch.setattr(evidence_tools,'MAX_OUTPUT_BYTES',64)
    with pytest.raises(IntakeRejected):
        service.source_excerpt(result.import_id,result.run_id,file_path='tests/test_risk.py',start_line=1,end_line=1)


def test_second_snapshot_load_must_match_validated_review(result,service,monkeypatch):
    from northstar_codereview import real_source
    snapshot=real_source.load_import(result.import_id)
    receipt=snapshot.real_source.import_approval.model_copy(update={'approval_id':uuid4()})
    from dataclasses import replace
    changed=replace(snapshot,real_source=snapshot.real_source.model_copy(update={'import_approval':receipt}))
    monkeypatch.setattr(service,'load_review',lambda *a:result)
    monkeypatch.setattr(service,'load_import',lambda *a:changed)
    with pytest.raises(IntakeRejected): service.source_file_catalog(result.import_id,result.run_id)
