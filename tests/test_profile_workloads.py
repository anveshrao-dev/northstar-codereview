"""Generated sample-shaped workloads, never the stored application source."""
import asyncio
from datetime import datetime,timedelta,timezone
from hashlib import sha256
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from northstar_codereview import real_source as intake
from northstar_codereview.real_source_contracts import SAMPLES,SampleId,IntakeManifest,ImportApproval,snapshot_from_hashes
from northstar_codereview.execution_profiles import get_profile
from northstar_codereview.execution_budget import ExecutionBudget,BudgetDenied
from northstar_codereview.chunk_review import build_plan,admission
from northstar_codereview.agents.code_quality import CodeQualityAgent
from .test_real_source import local_intake
from .test_real_review import service,Fake,ENV


def generated(local_intake,sample,counts,widths):
    root,_=local_intake
    texts={path:('#'+'x'*width+'\n')*count for path,count,width in zip(SAMPLES[sample][1],counts,widths)}
    for path,text in texts.items():
        p=root/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
    # Both fixture projects point only to this test's generated tree.
    intake._SOURCE_ROOTS={SAMPLES[sample][0]:root}
    now=datetime.now(timezone.utc)
    manifest=IntakeManifest(sample=sample,project=SAMPLES[sample][0],
        snapshot=snapshot_from_hashes(sample,{p:sha256(t.encode()).hexdigest() for p,t in texts.items()}),
        retain_until=now+timedelta(days=1))
    receipt=ImportApproval(approval_id=uuid4(),manifest=manifest,state='approved',approved_at=now,
        expires_at=now+timedelta(minutes=30),privacy_reviewed=True)
    intake.import_sample(sample,approval=receipt)
    return receipt


@pytest.mark.parametrize('sample,name,counts,widths,reads,calls',[
    (SampleId.CSV_IMPORT,'extended_csv',(161,52,61,267),(50,35,35,40),9,3),
    (SampleId.BM25_RETRIEVAL,'extended_bm25',(55,104,94,135,152),(50,55,60,60,60),11,4)])
def test_extended_complete_analysis_and_standard_denial(service,local_intake,sample,name,counts,widths,reads,calls):
    receipt=generated(local_intake,sample,counts,widths)
    snapshot=intake.load_import(receipt.approval_id)
    plan=build_plan(snapshot,execution_profile=get_profile(name))
    assert len(plan.ranges)==reads and len(plan.units)==calls
    model=Fake()
    permit=service.authorize_transmission(sample,receipt.approval_id,expected_digest=receipt.manifest.digest,allow_transmission=True,execution_profile=name)
    result=asyncio.run(service.run_review(sample,receipt.approval_id,transmission_id=permit.approval_id,
        allow_live=True,environ=ENV,execution_profile=name,provider_factory=lambda *a:model))
    assert result.status=='completed' and model.closed
    assert result.usage.tool_attempts==reads and result.usage.model_attempts==calls
    a=result.report.specialists[0].diagnostics.analysis
    assert a.prepared_lines==a.analyzed_lines==sum(counts)
    assert result.usage.output_reserved==calls*1000
    assert result.usage.input_reserved==sum(plan.input_units)
    assert all(f.claim_status=='unverified' for f in result.report.findings)
    assert service.load_review(receipt.approval_id,result.run_id)==result
    permit=service.authorize_transmission(sample,receipt.approval_id,expected_digest=receipt.manifest.digest,allow_transmission=True)
    result=asyncio.run(service.run_review(sample,receipt.approval_id,transmission_id=permit.approval_id,
        allow_live=True,environ=ENV,provider_factory=lambda *a:pytest.fail('provider must not start')))
    assert result.category=='capacity' and result.execution_profile.name=='standard'
    assert result.usage.model_attempts==result.usage.tool_attempts==0


@pytest.mark.parametrize('name,deadline',[('standard',60),('extended_csv',70),('extended_bm25',90)])
def test_deadline_boundary_and_all_attempt_ceilings(name,deadline):
    p=get_profile(name)
    time=[0.0]
    ledger=ExecutionBudget(p.global_limits(),execution_profile=p,clock=lambda:time[0])
    time[0]=deadline-.01
    ticket=ledger.reserve_model(1,1000)
    time[0]=deadline
    with pytest.raises(BudgetDenied,match='deadline'):ledger.reserve_tool()
    ledger.record_usage(ticket,1,1)
    assert ledger.usage().model_attempts==1 and ledger.usage().input_reserved==1
    ledger=ExecutionBudget(p.global_limits(),execution_profile=p)
    for _ in range(4):ledger.reserve_model(1,1000)
    with pytest.raises(BudgetDenied):ledger.reserve_model(1,1)
    assert ledger.usage().model_attempts==4
    ledger=ExecutionBudget(p.global_limits(),execution_profile=p)
    ledger.reserve_model(p.max_input_tokens,1)
    with pytest.raises(BudgetDenied):ledger.reserve_model(1,1)
    ledger=ExecutionBudget(p.global_limits(),execution_profile=p)
    with pytest.raises(BudgetDenied):ledger.reserve_model(1,1001)


def test_measured_bm25_hold_projection_needs_extended_input():
    from dataclasses import replace
    from northstar_codereview.local_source import _assemble
    # Recorded numeric projections only; no real source or model text.
    plan=replace(build_plan(_assemble({('head','a.py'):'x=1\n'})),
        units=((0,),(0,),(0,),(0,)),input_units=(19367,17606,17484,14983))
    assert 'protected_input' in admission(plan,tool_attempts=11,input_units=80000).denial_reasons
    assert admission(plan,tool_attempts=11,input_units=90000).admission=='admitted'


@pytest.mark.parametrize('name',['extended_csv','extended_bm25'])
@pytest.mark.parametrize('mode',['cancel','timeout','truncated','malformed','bad_evidence'])
def test_extended_failure_no_retry_cleanup_and_consumption(service,local_intake,name,mode):
    receipt=generated(local_intake,SampleId.PORTFOLIO_RISK,(53,85),(8,8))
    permit=service.authorize_transmission(receipt.manifest.sample,receipt.approval_id,expected_digest=receipt.manifest.digest,allow_transmission=True,execution_profile=name)
    class Fails(Fake):
        async def ainvoke(self,messages,**kwargs):
            self.histories.append(messages)
            if mode=='cancel':raise asyncio.CancelledError()
            if mode=='timeout':raise TimeoutError()
            if mode=='truncated':return AIMessage(content='{"findings":[]}',response_metadata={'finish_reason':'length'})
            if mode=='bad_evidence':
                from .test_prepared_review import PreparedModel
                return await PreparedModel('fabricated').ainvoke(messages,**kwargs)
            return AIMessage(content='{}')
    model=Fails()
    result=asyncio.run(service.run_review(receipt.manifest.sample,receipt.approval_id,
        transmission_id=permit.approval_id,allow_live=True,environ=ENV,execution_profile=name,provider_factory=lambda *a:model))
    assert result.status=='partial' and model.closed and len(model.histories)==1
    assert not result.report.findings
    assert result.report.specialists[0].diagnostics.analysis.analyzed_lines==0
    result=asyncio.run(service.run_review(receipt.manifest.sample,receipt.approval_id,
        transmission_id=permit.approval_id,allow_live=True,environ=ENV,execution_profile=name,provider_factory=lambda *a:pytest.fail('replay')))
    assert result.category=='transmission_consumed'


@pytest.mark.parametrize('name,seconds',[('extended_csv',70),('extended_bm25',90)])
def test_runner_overall_deadline_cancels_and_cleans_up(service,local_intake,monkeypatch,name,seconds):
    receipt=generated(local_intake,SampleId.PORTFOLIO_RISK,(53,85),(8,8))
    permit=service.authorize_transmission(receipt.manifest.sample,receipt.approval_id,
        expected_digest=receipt.manifest.digest,allow_transmission=True,execution_profile=name)
    time=[0.0]
    original_budget=service.ExecutionBudget
    def budget(limits,**kwargs):return original_budget(limits,clock=lambda:time[0],**kwargs)
    monkeypatch.setattr(service,'ExecutionBudget',budget)
    original_timeout=asyncio.timeout
    deadlines=[]
    def accelerated_timeout(delay):
        deadlines.append(delay)
        if len(deadlines)==1:
            # Exercise the real cancellation context without a 70/90 second sleep.
            assert delay==seconds
            time[0]=seconds
            return original_timeout(0)
        return original_timeout(delay)
    monkeypatch.setattr(asyncio,'timeout',accelerated_timeout)
    model=Fake()
    result=asyncio.run(service.run_review(receipt.manifest.sample,receipt.approval_id,
        transmission_id=permit.approval_id,allow_live=True,environ=ENV,execution_profile=name,provider_factory=lambda *a:model))
    assert result.status=='partial' and result.category=='deadline' and model.closed
    assert result.usage.model_attempts==0 and not model.histories
    assert result.persisted
    assert result.execution_profile.evaluation_timeout_seconds==seconds
