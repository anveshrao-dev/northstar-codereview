"""Named limits are authority, not automatic workload sizing."""
import asyncio
import importlib
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from northstar_codereview.execution_budget import ExecutionBudget,GlobalLimits,BudgetDenied
from northstar_codereview.real_source_contracts import TransmissionApproval,IntakeRejected
from northstar_codereview import transmission_claims as claims, real_source as intake
from .test_real_source import local_intake,transmission_for
from .test_real_review import service,captured,Fake,ENV


def profiles():
    assert importlib.util.find_spec('northstar_codereview.execution_profiles'), 'named profile registry missing'
    return importlib.import_module('northstar_codereview.execution_profiles')


@pytest.mark.parametrize('name,tools,inputs,deadline',[('standard',6,80000,60),('extended_csv',9,80000,70),('extended_bm25',11,90000,90)])
def test_exact_named_budget_enforces_limits(name,tools,inputs,deadline):
    p=profiles().get_profile(name)
    assert p.name==name and p.max_tool_attempts==tools and p.max_input_tokens==inputs
    assert p.evaluation_timeout_seconds==deadline
    assert p.max_model_requests==4 and p.max_output_tokens==4000
    assert p.output_tokens_per_call==1000 and p.request_timeout_seconds==20
    assert p.synthesis_input_hold==30000 and p.max_provider_retries==p.max_output_repairs==0
    ledger=ExecutionBudget(p.global_limits(),execution_profile=p)
    for _ in range(tools):ledger.reserve_tool()
    with pytest.raises(BudgetDenied):ledger.reserve_tool()
    assert ledger.usage().tool_attempts==tools


def test_unknown_and_modified_profiles_rejected():
    api=profiles()
    with pytest.raises(ValueError):api.get_profile('unbounded')
    p=api.get_profile('extended_bm25')
    with pytest.raises(ValidationError):type(p).model_validate({**p.model_dump(),'max_input_tokens':90001})
    with pytest.raises(ValidationError):GlobalLimits(evaluation_timeout_seconds=90)
    with pytest.raises(ValueError):ExecutionBudget(p.global_limits())


@pytest.mark.parametrize('name',['standard','extended_csv','extended_bm25'])
def test_authorization_and_durable_claim_bind_exact_profile(service,captured,name):
    api=profiles();p=api.get_profile(name)
    permit=service.authorize_transmission(captured.manifest.sample,captured.approval_id,
        expected_digest=captured.manifest.digest,allow_transmission=True,execution_profile=name)
    assert permit.execution_profile==p
    snapshot=intake.load_import(captured.approval_id)
    with claims.execution_scope(snapshot,permit,run_id=uuid4(),identity=('nebius',permit.model),
            specialists=('code_quality',),execution_profile=name) as record:
        assert record.execution_profile==p
        claims.require_execution(snapshot,permit,execution_profile=name)
    raw=json.loads((intake._STORE/'transmission_claims'/f'{permit.approval_id}.json').read_text())
    assert raw['execution_profile']==p.model_dump(mode='json')
    with pytest.raises(IntakeRejected):
        with claims.execution_scope(snapshot,permit,run_id=uuid4(),identity=('nebius',permit.model),specialists=('code_quality',),execution_profile=name):pass


def test_profile_substitution_rejected_before_provider_and_claim(service,captured):
    permit=service.authorize_transmission(captured.manifest.sample,captured.approval_id,
        expected_digest=captured.manifest.digest,allow_transmission=True,execution_profile='extended_csv')
    starts=[]
    result=asyncio.run(service.run_review(captured.manifest.sample,captured.approval_id,
        transmission_id=permit.approval_id,allow_live=True,environ=ENV,execution_profile='extended_bm25',
        provider_factory=lambda *a:starts.append(True)))
    assert result.status=='blocked' and result.category=='authorization' and starts==[]
    assert not (intake._STORE/'transmission_claims'/f'{permit.approval_id}.json').exists()


def test_legacy_approval_stays_standard_and_serialization_unchanged(captured):
    profiles()
    permit=transmission_for(captured)
    assert 'execution_profile' not in permit.model_dump()
    snapshot=intake.load_import(captured.approval_id)
    with pytest.raises(IntakeRejected):
        with claims.execution_scope(snapshot,permit,run_id=uuid4(),identity=('nebius',permit.model),specialists=('code_quality',),execution_profile='extended_csv'):pass
    with claims.execution_scope(snapshot,permit,run_id=uuid4(),identity=('nebius',permit.model),specialists=('code_quality',)) as record:
        assert record.execution_profile.name=='standard'


@pytest.mark.parametrize('name',['standard','extended_csv','extended_bm25'])
def test_runner_uses_explicit_profile_without_sample_inference(service,captured,name):
    p=profiles().get_profile(name)
    permit=service.authorize_transmission(captured.manifest.sample,captured.approval_id,
        expected_digest=captured.manifest.digest,allow_transmission=True,execution_profile=name)
    model=Fake()
    result=asyncio.run(service.run_review(captured.manifest.sample,captured.approval_id,
        transmission_id=permit.approval_id,allow_live=True,environ=ENV,execution_profile=name,
        provider_factory=lambda *a:model))
    assert result.status=='completed' and result.execution_profile==p and model.closed
    assert result.limits.max_tool_attempts==p.max_tool_attempts
    assert result.limits.evaluation_timeout_seconds==p.evaluation_timeout_seconds
    assert service.load_review(captured.approval_id,result.run_id)==result


def test_legacy_cannot_pair_with_extended_ledger(captured):
    from northstar_codereview.agents.code_quality import CodeQualityAgent,RunLimits
    p=profiles().get_profile('extended_bm25')
    snapshot=intake.load_import(captured.approval_id)
    permit=transmission_for(captured)
    with claims.execution_scope(snapshot,permit,run_id=uuid4(),identity=('nebius',permit.model),specialists=('code_quality',)):
        with pytest.raises(IntakeRejected):
            CodeQualityAgent(snapshot,model=Fake(),transmission_approval=permit,
                limits=RunLimits(max_provider_retries=0,max_output_repairs=0),
                execution_budget=ExecutionBudget(p.global_limits(),execution_profile=p))


@pytest.mark.parametrize('hold',[0,29999,30001])
def test_named_agent_rejects_incorrect_protected_hold(service,captured,hold):
    from northstar_codereview.agents.code_quality import CodeQualityAgent
    p=profiles().get_profile('extended_csv')
    permit=service.authorize_transmission(captured.manifest.sample,captured.approval_id,
        expected_digest=captured.manifest.digest,allow_transmission=True,execution_profile=p.name)
    snapshot=intake.load_import(captured.approval_id)
    run_id,job=uuid4(),uuid4()
    ledger=ExecutionBudget(p.global_limits(),execution_profile=p)
    with claims.execution_scope(snapshot,permit,run_id=run_id,identity=('nebius',permit.model),
            specialists=('code_quality',),execution_profile=p.name):
        model=Fake()
        agent=CodeQualityAgent(snapshot,model=model,transmission_approval=permit,
            limits=p.agent_limits(),execution_budget=ledger)
        agent.synthesis_reservation=ledger.reserve_synthesis((job,),input_units=hold)[job]
        with pytest.raises(IntakeRejected):asyncio.run(agent.run(review_id=run_id,job_id=job))
        assert not model.histories and ledger.usage().tool_attempts==0


def test_extended_specialist_schema_requires_named_opt_in():
    from northstar_codereview.agents.code_quality import CodeQualityAgent,ProfileRunLimits
    from northstar_codereview.local_source import _assemble
    with pytest.raises(ValueError):
        CodeQualityAgent(_assemble({('head','app.py'):'value=1\n'}),model=Fake(),approved_synthetic=True,
            limits=ProfileRunLimits(max_tool_attempts=11))


def test_stored_approval_cannot_be_rebound_to_another_valid_profile(service,captured):
    api=profiles()
    permit=service.authorize_transmission(captured.manifest.sample,captured.approval_id,
        expected_digest=captured.manifest.digest,allow_transmission=True)
    substituted=permit.model_copy(update={'execution_profile':api.get_profile('extended_bm25')})
    snapshot=intake.load_import(captured.approval_id)
    with pytest.raises(IntakeRejected):
        with claims.execution_scope(snapshot,substituted,run_id=uuid4(),identity=('nebius',permit.model),
                specialists=('code_quality',),execution_profile='extended_bm25'):pass
    assert not (intake._STORE/'transmission_claims'/f'{permit.approval_id}.json').exists()


def test_profile_limits_cannot_be_tampered_in_approval_or_claim(service,captured):
    p=profiles().get_profile('extended_csv')
    permit=service.authorize_transmission(captured.manifest.sample,captured.approval_id,
        expected_digest=captured.manifest.digest,allow_transmission=True,execution_profile=p.name)
    with pytest.raises(ValidationError):
        TransmissionApproval.model_validate({**permit.model_dump(),'execution_profile':{**p.model_dump(),'max_tool_attempts':10}})
    snapshot=intake.load_import(captured.approval_id)
    with claims.execution_scope(snapshot,permit,run_id=uuid4(),identity=('nebius',permit.model),specialists=('code_quality',),execution_profile=p.name):
        path=intake._STORE/'transmission_claims'/f'{permit.approval_id}.json'
        raw=json.loads(path.read_text())
        raw['execution_profile']=profiles().get_profile('extended_bm25').model_dump()
        path.write_text(json.dumps(raw))
        with pytest.raises(IntakeRejected):claims.require_execution(snapshot,permit)


def test_named_approval_rejects_other_specialist_and_snapshot(service,captured):
    p=profiles().get_profile('extended_csv')
    permit=service.authorize_transmission(captured.manifest.sample,captured.approval_id,
        expected_digest=captured.manifest.digest,allow_transmission=True,execution_profile=p.name)
    snapshot=intake.load_import(captured.approval_id)
    with pytest.raises(IntakeRejected):
        with claims.execution_scope(snapshot,permit,run_id=uuid4(),identity=('nebius',permit.model),specialists=('security',),execution_profile=p.name):pass
    with pytest.raises(ValidationError):
        TransmissionApproval.model_validate({**permit.model_dump(),'specialists':['security']})
    from northstar_codereview.local_source import CollectedSnapshot
    altered=CollectedSnapshot(snapshot.manifest.model_copy(update={'snapshot_id':uuid4()}),snapshot.sources,snapshot.real_source)
    with pytest.raises(IntakeRejected):
        with claims.execution_scope(altered,permit,run_id=uuid4(),identity=('nebius',permit.model),specialists=('code_quality',),execution_profile=p.name):pass
