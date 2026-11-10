"""Specialist authority is enforced on generated snapshots, never real source."""
import asyncio
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from .test_real_source import local_intake, imported, transmission_for, ReviewModel
from .test_real_review import service, captured, permit, Fake, ENV
from northstar_codereview import real_source as intake
from northstar_codereview.real_source_contracts import TransmissionApproval, IntakeRejected
from northstar_codereview.models import AgentName
from northstar_codereview.transmission_claims import execution_scope
from northstar_codereview.orchestrator import SPECIALIST_SPECS, ReviewOrchestrator
from northstar_codereview.evidence_tools import build_snapshot_tools
from northstar_codereview.transmission_claims import require_execution, agent_execution, orchestration_execution


def scoped(receipt, names=('code_quality',)):
    return TransmissionApproval.model_validate({**transmission_for(receipt).model_dump(), 'specialists': names})


def test_legacy_unscoped_approval_is_rejected(imported):
    _, receipt = imported
    data = transmission_for(receipt).model_dump()
    data.pop('specialists', None)
    with pytest.raises(ValidationError):
        TransmissionApproval.model_validate(data)


@pytest.mark.parametrize('names', [(), ('unknown',), ('*',), ('code_quality','code_quality'),
    'code_quality', None, (True,), (b'code_quality',), ({'name':'code_quality'},), {'code_quality'}, (' code_quality',)])
def test_invalid_specialist_scope_rejected(imported, names):
    _, receipt = imported
    with pytest.raises(ValidationError): scoped(receipt, names)


@pytest.mark.parametrize('name', ['security', 'test_coverage', 'gitops', 'cicd_groovy'])
def test_code_quality_scope_rejects_other_specialists_before_binding(imported, name):
    snapshot, receipt = imported
    approval = scoped(receipt)
    model = ReviewModel()
    agent_type = next(s.agent_type for s in SPECIALIST_SPECS if s.name.value == name)
    with execution_scope(snapshot, approval, run_id=uuid4(), identity=('nebius', approval.model),
                         specialists=(AgentName.CODE_QUALITY,)):
        with pytest.raises(IntakeRejected):
            agent_type(snapshot, model=model, transmission_approval=approval)
    assert not model.bindings and not model.histories


def test_orchestrator_rejects_whole_unauthorized_route_before_factory(imported):
    snapshot, receipt = imported
    starts = []
    def factory(name):
        starts.append(name)
        return ReviewModel()
    runner = ReviewOrchestrator(snapshot, model_factory=factory, transmission_approval=scoped(receipt))
    with pytest.raises(IntakeRejected): asyncio.run(runner.run(review_id=uuid4()))
    assert not starts
    assert not (intake._STORE/'transmission_claims').exists()


def test_service_mints_code_quality_only_authority(service, captured):
    approval = permit(service, captured)
    assert approval.model_dump(mode='json').get('specialists') == ['code_quality']


def test_wrong_specialist_runner_rejected_before_credentials_or_provider(service, captured, monkeypatch):
    approval = scoped(captured, ('security',))
    intake.record_transmission(approval)
    monkeypatch.setattr(service, 'load_nebius_settings', lambda *a: pytest.fail('credentials accessed'))
    result = asyncio.run(service.run_review(captured.manifest.sample,captured.approval_id,
        transmission_id=approval.approval_id,allow_live=True,
        provider_factory=lambda *a:pytest.fail('provider started')))
    assert result.status == 'blocked' and result.category == 'authorization'
    assert result.usage.model_attempts == 0
    assert not (intake._STORE/'transmission_claims').exists()


@pytest.mark.parametrize('names', [('code_quality',), ('security',), ('test_coverage',), ('gitops',), ('cicd_groovy',),
    ('code_quality', 'security', 'test_coverage')])
def test_valid_explicit_scopes_roundtrip(imported, names):
    _, receipt = imported
    approval = scoped(receipt, names)
    assert TransmissionApproval.model_validate_json(approval.model_dump_json()).specialists == names


def test_claim_records_approved_and_requested_scope(imported):
    snapshot, receipt = imported
    approval = scoped(receipt, ('code_quality', 'security'))
    with execution_scope(snapshot,approval,run_id=uuid4(),identity=('nebius',approval.model),specialists=('code_quality',)):
        path = intake._STORE/'transmission_claims'/f'{approval.approval_id}.json'
        record = json.loads(path.read_text())
        assert record['specialists'] == ['code_quality','security']
        assert record['requested_specialists'] == ['code_quality']
        with pytest.raises(IntakeRejected):
            require_execution(snapshot,approval,specialists=('security',))


@pytest.mark.parametrize('field,value', [('specialists',['security']), ('requested_specialists',['security'])])
def test_persisted_claim_scope_mismatch_blocks_tools(imported, field, value):
    snapshot, receipt = imported
    approval = scoped(receipt)
    with execution_scope(snapshot,approval,run_id=uuid4(),identity=('nebius',approval.model),specialists=('code_quality',)):
        tool = build_snapshot_tools(snapshot,transmission_approval=approval)[0]
        path = intake._STORE/'transmission_claims'/f'{approval.approval_id}.json'
        record = json.loads(path.read_text()); record[field] = value; path.write_text(json.dumps(record))
        with pytest.raises(IntakeRejected): tool.invoke({})


def test_scope_rejects_unapproved_request_without_consuming(imported):
    snapshot, receipt = imported
    approval = scoped(receipt)
    with pytest.raises(IntakeRejected):
        with execution_scope(snapshot,approval,run_id=uuid4(),identity=('nebius',approval.model),specialists=('security',)): pass
    assert not (intake._STORE/'transmission_claims').exists()


def test_tool_scope_cannot_be_broadened_with_caller_label(imported):
    snapshot, receipt = imported
    approval = scoped(receipt, ('code_quality','security'))
    with execution_scope(snapshot,approval,run_id=uuid4(),identity=('nebius',approval.model),specialists=('code_quality',)):
        with pytest.raises(IntakeRejected):
            build_snapshot_tools(snapshot,transmission_approval=approval,specialist='security')


def test_specialist_cannot_borrow_another_specialists_tools(imported):
    snapshot, receipt = imported
    approval = scoped(receipt, ('code_quality','security'))
    run_id = uuid4()
    jobs = {AgentName.CODE_QUALITY:uuid4(),AgentName.SECURITY:uuid4()}
    with orchestration_execution(snapshot,approval,review_id=run_id,jobs=jobs):
        tool = build_snapshot_tools(snapshot,transmission_approval=approval,specialist=AgentName.CODE_QUALITY)[0]
        with agent_execution(snapshot,approval,review_id=run_id,job_id=jobs[AgentName.SECURITY],specialist=AgentName.SECURITY):
            with pytest.raises(IntakeRejected): tool.invoke({})
        with agent_execution(snapshot,approval,review_id=run_id,job_id=jobs[AgentName.CODE_QUALITY],specialist=AgentName.CODE_QUALITY):
            assert '"status":"ok"' in tool.invoke({})


@pytest.mark.parametrize('method', ['invoke', 'ainvoke', 'run', 'arun'])
def test_unauthorized_tools_block_even_argument_error_metadata(imported, method):
    snapshot, receipt = imported
    approval = scoped(receipt)
    with execution_scope(snapshot,approval,run_id=uuid4(),identity=('nebius',approval.model),specialists=('code_quality',)):
        tool = next(t for t in build_snapshot_tools(snapshot,transmission_approval=approval) if t.name=='read_source_lines')
    with pytest.raises(IntakeRejected):
        result = getattr(tool,method)({'file_path':'tests/test_risk.py','start_line':-1,'end_line':1})
        if method.startswith('a'): asyncio.run(result)


@pytest.mark.parametrize('kind', ['security', 'legacy'])
def test_cli_rejects_non_code_quality_authority(service, captured, monkeypatch, capsys, kind):
    from northstar_codereview.real_review_cli import main
    approval = scoped(captured, ('security',))
    intake.record_transmission(approval)
    if kind == 'legacy':
        path = intake._STORE/'imports'/str(captured.approval_id)/'receipts'/f'transmission-{approval.approval_id}.json'
        raw = json.loads(path.read_text()); raw.pop('specialists'); path.write_text(json.dumps(raw))
    monkeypatch.setattr(service,'load_nebius_settings',lambda *a:pytest.fail('credentials accessed'))
    assert main(['run','--sample','portfolio_risk','--import-id',str(captured.approval_id),
                 '--transmission-id',str(approval.approval_id),'--allow-live']) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['category']=='authorization' and result['usage']['model_attempts']==0


def test_cli_authorization_has_no_scope_override(service, captured, capsys):
    from northstar_codereview.real_review_cli import main
    args = ['authorize','--sample','portfolio_risk','--import-id',str(captured.approval_id),
            '--manifest-digest',captured.manifest.digest,'--allow-transmission']
    assert main([*args,'--specialists','security']) == 2
    assert json.loads(capsys.readouterr().out)['category']=='invalid_arguments'
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)['specialists']==['code_quality']


@pytest.mark.parametrize('kind', ['security', 'legacy'])
def test_ui_rejects_non_code_quality_receipt(service, captured, monkeypatch, kind):
    from .test_real_review_ui import app, button
    approval = scoped(captured, ('security',))
    intake.record_transmission(approval)
    if kind == 'legacy':
        path = intake._STORE/'imports'/str(captured.approval_id)/'receipts'/f'transmission-{approval.approval_id}.json'
        raw = json.loads(path.read_text()); raw.pop('specialists'); path.write_text(json.dumps(raw))
    monkeypatch.setattr(service,'load_nebius_settings',lambda *a:pytest.fail('credentials accessed'))
    at = app()
    key = f'real:{captured.approval_id}:{captured.manifest.digest}'
    at.session_state[key+':permit'] = str(approval.approval_id)
    at.session_state[key+':live-consent'] = True
    at.run()
    assert not at.exception
    assert button(at,'Start live real-code review').disabled
    assert not (intake._STORE/'transmission_claims').exists()
