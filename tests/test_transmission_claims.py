"""Single-use real-source authority: generated snapshots and local processes only."""
import asyncio
import importlib
import json
import os
import subprocess
import sys
from uuid import uuid4

import pytest

from .test_real_source import local_intake, imported, transmission_for, ReviewModel
from .test_real_review import captured, service, permit, Fake, ENV
from northstar_codereview import real_source as intake
from northstar_codereview.real_source_contracts import IntakeRejected


@pytest.fixture
def claims():
    assert importlib.util.find_spec('northstar_codereview.transmission_claims'), 'durable single-use gate missing'
    return importlib.import_module('northstar_codereview.transmission_claims')


def test_first_claim_binds_full_identity_and_remains_after_exit(claims, imported):
    snapshot, receipt = imported
    approval = transmission_for(receipt)
    run = uuid4()
    with claims.execution_scope(snapshot, approval, run_id=run, identity=('nebius', approval.model), specialists=('code_quality',)):
        claims.require_execution(snapshot, approval, run_id=run)
    path = intake._STORE/'transmission_claims'/f'{approval.approval_id}.json'
    record = json.loads(path.read_text())
    assert record['run_id'] == str(run) and record['approval_id'] == str(approval.approval_id)
    assert record['snapshot_id'] == str(snapshot.manifest.snapshot_id)
    assert record['snapshot_revision'] == snapshot.manifest.revision
    assert record['manifest_digest'] == receipt.manifest.digest
    assert record['provider']=='nebius' and record['model']==approval.model
    assert path.stat().st_mode & 0o777 == 0o600
    assert 'value = 1' not in path.read_text()
    with pytest.raises(IntakeRejected):claims.require_execution(snapshot,approval,run_id=run)
    for next_run in (run, uuid4()):
        with pytest.raises(IntakeRejected):
            with claims.execution_scope(snapshot,approval,run_id=next_run,identity=('nebius',approval.model), specialists=('code_quality',)):pass


@pytest.mark.parametrize('change',['expired','snapshot','model','provider','digest','malformed'])
def test_invalid_approval_not_claimed(claims,imported,change):
    from datetime import datetime,timedelta,timezone
    snapshot,receipt=imported
    approval=transmission_for(receipt)
    updates={'snapshot':{'snapshot':approval.snapshot.model_copy(update={'snapshot_id':uuid4()})},
        'model':{'model':'wrong'},'provider':{'provider':'other'},'digest':{'manifest_digest':'0'*64},
        'expired':{'approved_at':datetime.now(timezone.utc)-timedelta(hours=2),'expires_at':datetime.now(timezone.utc)-timedelta(hours=1)},
        'malformed':{'approval_id':'../unsafe'}}
    with pytest.raises(IntakeRejected):
        with claims.execution_scope(snapshot,approval.model_copy(update=updates[change]),run_id=uuid4(),identity=('nebius',approval.model), specialists=('code_quality',)):pass
    assert not (intake._STORE/'transmission_claims').exists()


def test_cross_process_competition_and_restart(claims,imported,tmp_path):
    snapshot,receipt=imported
    approval=transmission_for(receipt)
    intake.record_transmission(approval)
    worker = '''import sys,socket
def deny(*a,**k): raise RuntimeError("network forbidden")
socket.socket.connect=deny; socket.socket.connect_ex=deny; socket.create_connection=deny; socket.getaddrinfo=deny
from uuid import UUID,uuid4
from pathlib import Path
from northstar_codereview import real_source as intake
from northstar_codereview.real_review import read_transmission
from northstar_codereview.transmission_claims import execution_scope
from northstar_codereview.real_source_contracts import IntakeRejected
intake._STORE=Path(sys.argv[1])
snapshot=intake.load_import(UUID(sys.argv[2]))
permit=read_transmission(UUID(sys.argv[2]),UUID(sys.argv[3]))
if len(sys.argv)>4:
 print("ready",flush=True); sys.stdin.readline()
try:
 with execution_scope(snapshot,permit,run_id=uuid4(),identity=("nebius",permit.model), specialists=('code_quality',)): print("claimed")
except IntakeRejected: print("rejected")
'''
    command=[sys.executable,'-B','-c',worker,str(intake._STORE),str(receipt.approval_id),str(approval.approval_id)]
    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','TMPDIR':str(tmp_path),'LANGSMITH_TRACING':'false','LANGCHAIN_TRACING_V2':'false'}
    processes=[subprocess.Popen([*command,'race'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env) for _ in range(2)]
    assert all(p.stdout.readline().strip() == 'ready' for p in processes)
    for process in processes:
        process.stdin.write('go\n')
        process.stdin.flush()
    outputs=[p.communicate(timeout=30) for p in processes]
    assert all(p.returncode==0 for p in processes), [e for _,e in outputs]
    assert sorted(o.strip() for o,_ in outputs)==['claimed','rejected']
    restarted=subprocess.run(command,capture_output=True,text=True,env=env,timeout=30)
    assert restarted.returncode==0 and restarted.stdout.strip()=='rejected'


def test_provider_startup_failure_consumes_approval(service,captured):
    approval=permit(service,captured)
    starts=[]
    def fail(*a): starts.append(True); raise RuntimeError('PRIVATE_STARTUP')
    async def run():
        return await service.run_review(captured.manifest.sample,captured.approval_id,transmission_id=approval.approval_id,
            allow_live=True,environ=ENV,provider_factory=fail)
    first=asyncio.run(run()); second=asyncio.run(run())
    assert len(starts)==1, 'transmission approval replayed with a new run ID'
    assert second.category=='transmission_consumed'
    assert 'PRIVATE_STARTUP' not in first.model_dump_json()


def test_direct_agent_cannot_use_unclaimed_approval(imported):
    from northstar_codereview.agents.code_quality import CodeQualityAgent
    snapshot,receipt=imported
    with pytest.raises(IntakeRejected):
        CodeQualityAgent(snapshot,model=ReviewModel(),transmission_approval=transmission_for(receipt))


def test_direct_tools_cannot_use_unclaimed_approval(imported):
    from northstar_codereview.evidence_tools import build_snapshot_tools
    snapshot,receipt=imported
    with pytest.raises(IntakeRejected):build_snapshot_tools(snapshot,transmission_approval=transmission_for(receipt))


@pytest.mark.parametrize('failure', ['cancel', 'timeout'])
def test_execution_interrupt_never_releases_approval(service, captured, failure):
    approval = permit(service, captured)
    starts = []
    class Interrupted(Fake):
        async def ainvoke(self, *args, **kwargs):
            raise asyncio.CancelledError() if failure == 'cancel' else TimeoutError()
    def factory(*args):
        starts.append(True)
        return Interrupted()
    async def run():
        return await service.run_review(captured.manifest.sample, captured.approval_id,
            transmission_id=approval.approval_id, allow_live=True, environ=ENV, provider_factory=factory)
    first = asyncio.run(run())
    second = asyncio.run(run())
    assert first.status == 'partial'
    assert second.category == 'transmission_consumed' and len(starts) == 1


def test_provider_startup_observes_durable_bound_claim(service, captured, claims):
    approval = permit(service, captured)
    run_id = uuid4()
    def factory(*args):
        snapshot = intake.load_import(captured.approval_id)
        claims.require_execution(snapshot, approval, run_id=run_id)
        path = intake._STORE / 'transmission_claims' / f'{approval.approval_id}.json'
        assert json.loads(path.read_text())['run_id'] == str(run_id)
        return Fake()
    result = asyncio.run(service.run_review(captured.manifest.sample, captured.approval_id,
        transmission_id=approval.approval_id, run_id=run_id, allow_live=True, environ=ENV, provider_factory=factory))
    assert result.status == 'completed'


@pytest.mark.parametrize('failure', ['write', 'fsync'])
def test_incomplete_claim_remains_consumed(claims, imported, monkeypatch, failure):
    snapshot, receipt = imported
    approval = transmission_for(receipt)
    original_sync = os.fsync
    calls = []
    def fail_write(*args): raise OSError('PRIVATE_ERROR')
    def fail_sync(fd):
        calls.append(fd)
        if len(calls) == 2: raise OSError('PRIVATE_ERROR')
        return original_sync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(os, 'write' if failure == 'write' else 'fsync', fail_write if failure == 'write' else fail_sync)
        with pytest.raises(IntakeRejected) as error:
            with claims.execution_scope(snapshot, approval, run_id=uuid4(), identity=('nebius', approval.model), specialists=('code_quality',)):
                pytest.fail('failed persistence admitted execution')
        assert 'PRIVATE_ERROR' not in str(error.value)
    assert (intake._STORE/'transmission_claims'/f'{approval.approval_id}.json').exists()
    with pytest.raises(IntakeRejected, match='transmission_consumed'):
        with claims.execution_scope(snapshot, approval, run_id=uuid4(), identity=('nebius', approval.model), specialists=('code_quality',)): pass


def test_retained_tools_and_agent_cannot_reenter_finished_scope(claims, imported):
    from northstar_codereview.agents.code_quality import CodeQualityAgent, RunLimits
    snapshot, receipt = imported
    approval, run_id = transmission_for(receipt), uuid4()
    model = ReviewModel()
    with claims.execution_scope(snapshot, approval, run_id=run_id, identity=('nebius', approval.model), specialists=('code_quality',)):
        agent = CodeQualityAgent(snapshot, model=model, transmission_approval=approval,
            limits=RunLimits(max_model_calls=2, max_provider_retries=0, max_output_repairs=0))
        result = asyncio.run(agent.run(review_id=run_id))
        assert len(result.validated_findings) == 1
        for next_id in (run_id, uuid4()):
            with pytest.raises(IntakeRejected): asyncio.run(agent.run(review_id=next_id))
        with pytest.raises(IntakeRejected): agent.tools['list_snapshot_files'].invoke({})
    with pytest.raises(IntakeRejected): agent.tools['list_snapshot_files'].invoke({})
    assert len(model.histories) == 1


def test_raw_agent_graph_cannot_bypass_run_gate(claims, imported):
    from time import monotonic
    from northstar_codereview.agents.code_quality import CodeQualityAgent, _Session
    snapshot, receipt = imported
    approval, run_id = transmission_for(receipt), uuid4()
    model = ReviewModel()
    with claims.execution_scope(snapshot, approval, run_id=run_id, identity=('nebius', approval.model), specialists=('code_quality',)):
        agent = CodeQualityAgent(snapshot, model=model, transmission_approval=approval)
        with pytest.raises(IntakeRejected):
            asyncio.run(agent.graph.ainvoke({'session': _Session(run_id, uuid4(), monotonic()+30)}))
    assert not model.histories


def test_orchestrator_claim_precedes_all_factories_and_cannot_replay(claims, imported):
    from northstar_codereview.orchestrator import ReviewOrchestrator
    from northstar_codereview.agents.code_quality import RunLimits
    snapshot, receipt = imported
    approval, run_id = transmission_for(receipt, ("code_quality", "security", "test_coverage")), uuid4()
    starts = []
    def factory(name):
        claims.require_execution(snapshot, approval, run_id=run_id)
        starts.append(name)
        return ReviewModel({'code_quality':'correctness','security':'security','test_coverage':'tests'}[name.value])
    runner = ReviewOrchestrator(snapshot, model_factory=factory, transmission_approval=approval,
        specialist_limits=RunLimits(max_model_calls=2, max_provider_retries=0, max_output_repairs=0))
    output = asyncio.run(runner.run(review_id=run_id))
    assert len(starts) == 3 and len(output.validated_findings) == 3
    for next_id in (run_id, uuid4()):
        with pytest.raises(IntakeRejected): asyncio.run(runner.run(review_id=next_id))
    assert len(starts) == 3


def test_raw_orchestrator_graph_cannot_start_factory(imported):
    from types import MappingProxyType
    from northstar_codereview.orchestrator import ReviewOrchestrator, _ReviewContext, SPECIALIST_SPECS
    snapshot, receipt = imported
    approval, run_id = transmission_for(receipt), uuid4()
    runner = ReviewOrchestrator(snapshot, model_factory=lambda *a: pytest.fail('provider started'), transmission_approval=approval)
    context = _ReviewContext(run_id, snapshot, MappingProxyType({spec.name:uuid4() for spec in SPECIALIST_SPECS}))
    with pytest.raises(IntakeRejected):
        asyncio.run(runner.graph.ainvoke({'review_id':run_id,'snapshot_id':snapshot.manifest.snapshot_id,'deliveries':()},context=context))


def test_cli_replay_uses_same_authority_gate(service, captured, monkeypatch, capsys):
    from northstar_codereview.real_review_cli import main
    approval = permit(service, captured)
    models = []
    monkeypatch.setattr(service, 'load_nebius_settings', lambda *a: type('Settings',(),{'model':ENV['NEBIUS_MODEL']})())
    def factory(*args, **kwargs):
        model = Fake(); models.append(model); return model
    monkeypatch.setattr(service, 'NebiusModel', factory)
    args = ['run','--sample',captured.manifest.sample.value,'--import-id',str(captured.approval_id),
            '--transmission-id',str(approval.approval_id),'--allow-live']
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(args) == 2
    second = json.loads(capsys.readouterr().out)
    assert first['status'] == 'completed' and second['category'] == 'transmission_consumed'
    assert len(models) == 1 and 'value = 1' not in json.dumps([first,second])


def test_scope_pid_binding_and_claim_integrity(claims, imported, monkeypatch):
    snapshot, receipt = imported
    approval, run_id = transmission_for(receipt), uuid4()
    with claims.execution_scope(snapshot,approval,run_id=run_id,identity=('nebius',approval.model), specialists=('code_quality',)):
        with monkeypatch.context() as patch:
            patch.setattr(claims.os,'getpid',lambda: -1)
            with pytest.raises(IntakeRejected): claims.require_execution(snapshot,approval)
        path = intake._STORE/'transmission_claims'/f'{approval.approval_id}.json'
        record = json.loads(path.read_text()); record['run_id'] = str(uuid4()); path.write_text(json.dumps(record))
        with pytest.raises(IntakeRejected): claims.require_execution(snapshot,approval)


def test_abrupt_process_exit_cannot_restore_approval(claims, imported, tmp_path):
    snapshot, receipt = imported
    approval = transmission_for(receipt)
    intake.record_transmission(approval)
    worker = """import os,sys,socket
from pathlib import Path
from uuid import UUID,uuid4
def deny(*a,**k): raise RuntimeError('network forbidden')
socket.socket.connect=deny; socket.socket.connect_ex=deny
socket.create_connection=deny; socket.getaddrinfo=deny
from northstar_codereview import real_source as intake
from northstar_codereview.real_review import read_transmission
from northstar_codereview.transmission_claims import execution_scope
intake._STORE=Path(sys.argv[1])
snapshot=intake.load_import(UUID(sys.argv[2]))
permit=read_transmission(UUID(sys.argv[2]),UUID(sys.argv[3]))
with execution_scope(snapshot,permit,run_id=uuid4(),identity=('nebius',permit.model), specialists=('code_quality',)):
 os._exit(17)
"""
    result = subprocess.run([sys.executable,'-B','-c',worker,str(intake._STORE),str(receipt.approval_id),str(approval.approval_id)],
        capture_output=True, timeout=30, env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','TMPDIR':str(tmp_path),
                                            'LANGSMITH_TRACING':'false','LANGCHAIN_TRACING_V2':'false'})
    assert result.returncode == 17 and not result.stdout and not result.stderr
    with pytest.raises(IntakeRejected, match='transmission_consumed'):
        with claims.execution_scope(snapshot,approval,run_id=uuid4(),identity=('nebius',approval.model), specialists=('code_quality',)): pass


def test_new_explicit_approval_allows_new_execution_after_failure(service, captured):
    first = permit(service,captured)
    def failure(*a): raise RuntimeError('sanitized startup failure')
    result = asyncio.run(service.run_review(captured.manifest.sample,captured.approval_id,
        transmission_id=first.approval_id,allow_live=True,environ=ENV,provider_factory=failure))
    assert result.status == 'blocked'
    second = permit(service,captured)
    assert second.approval_id != first.approval_id
    result = asyncio.run(service.run_review(captured.manifest.sample,captured.approval_id,
        transmission_id=second.approval_id,allow_live=True,environ=ENV,provider_factory=lambda *a:Fake()))
    assert result.status == 'completed'
    assert len(list((intake._STORE/'transmission_claims').glob('*.json'))) == 2


def test_corrupt_claim_traceback_does_not_reveal_record_values(claims, imported):
    import traceback
    snapshot, receipt = imported
    approval = transmission_for(receipt)
    with claims.execution_scope(snapshot,approval,run_id=uuid4(),identity=('nebius',approval.model), specialists=('code_quality',)):
        path = intake._STORE/'transmission_claims'/f'{approval.approval_id}.json'
        record = json.loads(path.read_text())
        record['provider'] = 'PRIVATE_CORRUPT_CLAIM_VALUE'
        path.write_text(json.dumps(record))
        with pytest.raises(IntakeRejected) as error:
            claims.require_execution(snapshot,approval)
        rendered = ''.join(traceback.format_exception(error.value))
        assert 'PRIVATE_CORRUPT_CLAIM_VALUE' not in rendered
        assert 'ValidationError' not in rendered
