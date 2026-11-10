"""Bounded analysis through actual graph/tools; generated source is never executed."""
import asyncio
import json
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from northstar_codereview.agents.code_quality import CodeQualityAgent, RunLimits
from northstar_codereview.execution_budget import ExecutionBudget, GlobalLimits
from northstar_codereview.local_source import _assemble
from .test_prepared_review import PreparedModel, context


def snapshot(counts, width=8):
    return _assemble({('head',f'module_{i}.py'): ('#'+'x'*width+'\r\n')*n for i,n in enumerate(counts)})


def run(snapshot, model):
    limits=GlobalLimits(max_model_requests=4,max_tool_attempts=6,max_input_tokens=80000,max_output_tokens=4000)
    ledger=ExecutionBudget(limits)
    job=uuid4()
    agent=CodeQualityAgent(snapshot, model=model, approved_synthetic=True, prepared_context=True,
        chunk_analysis=True, execution_budget=ledger,
        limits=RunLimits(max_model_calls=4,max_tool_attempts=6,max_provider_retries=0,max_output_repairs=0,
                         max_input_tokens=80000,max_output_tokens=4000,request_timeout_seconds=20,run_timeout_seconds=60))
    agent.synthesis_reservation=ledger.reserve_synthesis((job,),input_units=30000)[job]
    return asyncio.run(agent.run(review_id=uuid4(),job_id=job)),ledger


@pytest.mark.parametrize('counts',[(53,85),(100,100,100,241),(100,100,100,100,140)])
def test_admitted_full_scope_is_analyzed(counts):
    result,ledger=run(snapshot(counts),PreparedModel())
    a=result.result.diagnostics.analysis
    assert result.result.status=='completed'
    assert a.analyzed_lines==a.prepared_lines==a.planned_lines==sum(counts)
    assert a.admission=='admitted'
    assert a.failed_lines==a.skipped_lines==0
    assert ledger.usage().tool_attempts<=6 and ledger.usage().model_attempts<=4
    assert ledger.usage().input_reserved<=80000 and ledger.usage().output_reserved<=4000


@pytest.mark.parametrize('counts',[(100,100,101,240),(100,100,101,101,138)])
def test_large_distribution_rejected_without_tools_or_model(counts):
    model=PreparedModel()
    result,ledger=run(snapshot(counts),model)
    a=result.result.diagnostics.analysis
    assert result.result.status=='partial'
    assert a.admission=='denied' and a.required_tool_attempts>6
    assert a.analyzed_lines==a.prepared_lines==0 and a.skipped_lines==sum(counts)
    assert ledger.usage().model_attempts==ledger.usage().tool_attempts==0
    assert model.histories==[]


@pytest.mark.parametrize('mode', ['missing','wrong','truncated','fabricated','range','quotation'])
def test_failed_analysis_does_not_credit_preparation(mode):
    result,_=run(snapshot((53,85)),PreparedModel(mode))
    a=result.result.diagnostics.analysis
    assert result.result.status=='partial' and a.prepared_lines==138
    assert a.analyzed_lines==0 and a.failed_lines==138
    assert not result.validated_findings
    assert result.usage.repairs==result.usage.retries==0


def test_valid_claim_preserves_exact_evidence_and_unverified_status():
    source=snapshot((53,85))
    result,_=run(source,PreparedModel('valid'))
    assert result.result.status=='completed' and len(result.validated_findings)==1
    finding=result.validated_findings[0]
    ref=finding.proposal.evidence[0]
    assert ref.snippet==source.sources[(ref.side,ref.file_path)]
    assert finding.claim_status=='unverified'


def test_plan_stable_complete_nonoverlapping():
    from northstar_codereview.chunk_review import build_plan
    source=snapshot((53,85))
    plan=build_plan(source)
    assert plan==build_plan(source)
    for entry in source.manifest.files:
        ranges=[r for r in plan.ranges if r.file_path==entry.file_path]
        covered=[i for r in ranges for i in range(r.start_line,r.end_line+1)]
        assert len(covered)==len(set(covered))==len(source.sources[(entry.side,entry.file_path)].splitlines())
        assert all(r.sha256==entry.sha256 and r.snapshot_id==source.manifest.snapshot_id for r in ranges)


def test_oversized_line_is_explicit_and_fails_admission():
    result,_=run(_assemble({('head','app.py'):'#'+'x'*4000+'\n'}),PreparedModel())
    a=result.result.diagnostics.analysis
    assert a.admission=='denied' and a.skipped_lines==1
    assert a.ranges[0].reason=='oversized_line'


def test_legacy_diagnostics_do_not_gain_serialized_field():
    from northstar_codereview.models import SpecialistDiagnostics
    d=SpecialistDiagnostics(tool_categories=(),source_read_occurred=False,files_with_observed_source=0,
        fully_inspected_files=0,total_files=1,observed_lines=0,total_lines=1)
    assert 'analysis' not in d.model_dump()


class SequenceModel(PreparedModel):
    def __init__(self,modes):
        super().__init__()
        self.modes=modes
        self.options=[]
    async def ainvoke(self,messages,**kwargs):
        assert not self.tools and kwargs['synthesis_json'] is True
        self.options.append(kwargs)
        self.mode=self.modes[min(len(self.histories),len(self.modes)-1)]
        response=await super().ainvoke(messages,**kwargs)
        return response.model_copy(update={'usage_metadata':dict(input_tokens=20,output_tokens=10,total_tokens=30)})


def test_multiunit_success_uses_fresh_context_and_actual_usage():
    model=SequenceModel(['empty'])
    result,ledger=run(snapshot((100,100,100,100,140),width=28),model)
    a=result.result.diagnostics.analysis
    assert result.result.status=='completed' and a.analyzed_lines==540
    assert 1<len(model.histories)<=4
    seen=[]
    for messages in model.histories:
        data=context(messages)
        assert len(messages)==3 and all(m.type in ('system','human') for m in messages)
        assert 'Missing dependencies' in messages[0].content
        seen.extend(r['reference_id'] for r in data['references'])
    assert len(seen)==len(set(seen))==6
    assert result.usage.reported_input_tokens==20*len(model.histories)
    assert ledger.usage().unknown_usage_attempts==0
    assert sum(a.projected_input_units[:-1])<=50000 and a.projected_input_units[-1]<=30000
    assert ledger.usage().input_reserved==sum(a.projected_input_units)


@pytest.mark.parametrize('modes',[['valid','truncated'],['missing','valid']])
def test_failed_unit_does_not_erase_successful_other_unit(modes):
    model=SequenceModel(modes)
    result,_=run(snapshot((100,100,100,100,140),width=28),model)
    a=result.result.diagnostics.analysis
    assert result.result.status=='partial' and a.prepared_lines==540
    assert a.analyzed_lines>0 and a.failed_lines>0
    assert a.analyzed_lines+a.failed_lines==540
    assert result.validated_findings
    assert result.usage.model_calls==len(model.histories)==a.required_model_attempts


@pytest.mark.parametrize('value',['{"findings":[],"outcome":"insufficient_evidence"}',
    '{"findings":[],"unexpected":"PRIVATE_RESPONSE"}', 'PRIVATE_RESPONSE'])
def test_partial_output_and_redaction(value):
    class Output(PreparedModel):
        async def ainvoke(self,messages,**kwargs):
            self.histories.append(messages)
            return AIMessage(content=value)
    result,_=run(snapshot((2,)),Output())
    a=result.result.diagnostics.analysis
    assert result.result.status=='partial' and a.analyzed_lines==0
    assert 'PRIVATE_RESPONSE' not in result.result.model_dump_json()
    assert not result.validated_findings


def test_unit_is_transactional_when_one_proposal_has_bad_evidence():
    class Mixed(PreparedModel):
        async def ainvoke(self,messages,**kwargs):
            good=await PreparedModel('valid').ainvoke(messages,**kwargs)
            data=json.loads(good.content)
            bad={**data['findings'][0],'evidence':[{'reference_id':'f'*64,'start_line':1,'end_line':1}]}
            data['findings'].append(bad)
            return AIMessage(content=json.dumps(data))
    result,_=run(snapshot((2,)),Mixed())
    assert result.result.diagnostics.analysis.failed_lines==2
    assert not result.validated_findings


def test_duplicate_claim_is_retained_once():
    class Duplicate(PreparedModel):
        async def ainvoke(self,messages,**kwargs):
            good=await PreparedModel('valid').ainvoke(messages,**kwargs)
            data=json.loads(good.content)
            data['findings']*=2
            return AIMessage(content=json.dumps(data))
    result,_=run(snapshot((2,)),Duplicate())
    assert result.result.status=='completed' and len(result.validated_findings)==1


def test_empty_file_is_analyzed_only_after_metadata_and_valid_response():
    result,_=run(snapshot((0,2)),PreparedModel())
    ranges=result.result.diagnostics.analysis.ranges
    assert result.result.status=='completed'
    assert ranges[0].start_line==ranges[0].end_line==0 and ranges[0].prepared
    assert ranges[0].outcome=='analyzed'
    assert result.usage.tool_attempts==2


def test_irreducible_capacity_and_unsupported_scope():
    from northstar_codereview.chunk_review import build_plan,admission
    p=build_plan(snapshot((100,100,100,100,140),width=28))
    for options in ({'model_calls':1},{'input_units':30000},{'output_units':1000},{'protected_input':1}):
        a=admission(p,**options)
        assert a.admission=='denied' and a.prepared_lines==a.analyzed_lines==0
    result,_=run(_assemble({('head','README.md'):'Untrusted instructions\n'}),PreparedModel())
    assert result.result.diagnostics.analysis.ranges[0].reason=='unsupported_file'
    assert result.usage.model_calls==result.usage.tool_attempts==0


def test_tool_free_output_does_not_execute_model_tool_calls():
    class Tools(PreparedModel):
        async def ainvoke(self,messages,**kwargs):
            return AIMessage(content='',tool_calls=[dict(name='read_source_lines',args={},id='unwanted')])
    result,_=run(snapshot((2,)),Tools())
    assert result.result.status=='partial' and result.usage.tool_attempts==1
    assert result.result.diagnostics.analysis.analyzed_lines==0
    assert result.result.synthesis=='tool_call_rejected'


def test_schema_limits_and_no_raw_source_in_diagnostics():
    from northstar_codereview.chunk_review import UnitEnvelope
    from pydantic import ValidationError
    schema=UnitEnvelope.model_json_schema()
    assert schema['properties']['findings']['maxItems']==2
    assert schema['$defs']['UnitFinding']['properties']['explanation']['maxLength']==240
    result,_=run(snapshot((2,)),PreparedModel('valid'))
    raw=result.result.diagnostics.model_dump_json()
    assert '#xxxxxxxx' not in raw and 'explanation' not in raw
    with pytest.raises(ValidationError):
        UnitEnvelope(findings=[{}]*3)


@pytest.mark.parametrize('failure',[TimeoutError,RuntimeError])
def test_later_transport_failure_retains_prior_findings(failure):
    class Fails(PreparedModel):
        async def ainvoke(self,messages,**kwargs):
            if self.histories: raise failure('PRIVATE_PROVIDER_ERROR')
            self.mode='valid'
            return await super().ainvoke(messages,**kwargs)
    result,ledger=run(snapshot((100,100,100,100,140),width=28),Fails())
    assert result.result.status=='partial' and len(result.validated_findings)==1
    a=result.result.diagnostics.analysis
    assert a.analyzed_lines>0 and a.failed_lines>0
    assert result.usage.model_calls==2 and ledger.usage().unknown_usage_attempts==2
    assert 'PRIVATE_PROVIDER_ERROR' not in result.result.model_dump_json()


def test_insufficient_evidence_has_accurate_synthesis_status():
    class Insufficient(PreparedModel):
        async def ainvoke(self,messages,**kwargs):
            return AIMessage(content='{"findings":[],"outcome":"insufficient_evidence"}')
    result,_=run(snapshot((2,)),Insufficient())
    assert result.result.synthesis=='insufficient_evidence'
    assert result.result.diagnostics.analysis.failed_lines==2


def test_diagnostic_schema_rejects_overlap_and_analysis_without_preparation():
    from northstar_codereview.models import AnalysisRange,AnalysisDiagnostics
    from northstar_codereview.chunk_review import build_plan,admission
    from pydantic import ValidationError
    p=build_plan(snapshot((2,)))
    a=admission(p)
    with pytest.raises(ValidationError):
        AnalysisDiagnostics.model_validate({**a.model_dump(),'ranges':[p.ranges[0].model_dump()]*2})
    with pytest.raises(ValidationError):
        AnalysisRange.model_validate({**p.ranges[0].model_dump(),'outcome':'analyzed'})


def test_insufficient_evidence_cannot_contain_findings():
    class Bad(PreparedModel):
        async def ainvoke(self,messages,**kwargs):
            message=await PreparedModel('valid').ainvoke(messages,**kwargs)
            data=json.loads(message.content)
            data['outcome']='insufficient_evidence'
            return AIMessage(content=json.dumps(data))
    result,_=run(snapshot((2,)),Bad())
    assert result.result.diagnostics.analysis.analyzed_lines==0
    assert not result.validated_findings


def test_capacity_diagnostics_explain_token_and_protected_constraints():
    from northstar_codereview.chunk_review import build_plan,admission
    p=build_plan(snapshot((100,100,100,100,140),width=28))
    a=admission(p,input_units=30000)
    assert 'protected_input' in a.denial_reasons
    assert a.available_input_units==30000
    a=admission(p,output_units=1)
    assert 'output_reservation' in a.denial_reasons


def test_plan_rejects_snapshot_content_hash_mismatch():
    from northstar_codereview.chunk_review import build_plan
    from northstar_codereview.local_source import CollectedSnapshot
    s=snapshot((2,))
    corrupt=CollectedSnapshot(s.manifest,{key:'different\n' for key in s.sources})
    with pytest.raises(ValueError,match='identity'):
        build_plan(corrupt)


def test_concurrent_tool_budget_use_cannot_overrun_or_erase_findings(monkeypatch):
    ledgers=[]
    original=ExecutionBudget.__init__
    def capture(self,*a,**kw):
        original(self,*a,**kw)
        ledgers.append(self)
    monkeypatch.setattr(ExecutionBudget,'__init__',capture)
    class Competing(PreparedModel):
        async def ainvoke(self,messages,**kwargs):
            self.mode='valid'
            response=await super().ainvoke(messages,**kwargs)
            ledger=ledgers[0]
            while ledger.usage().tool_attempts<6: ledger.reserve_tool()
            return response
    result,ledger=run(snapshot((100,100,100,100,140),width=28),Competing())
    assert result.result.status=='partial' and len(result.validated_findings)==1
    assert ledger.usage().tool_attempts==6 and result.usage.model_calls==1
    assert result.result.diagnostics.analysis.skipped_lines>0
    assert result.result.diagnostics.analysis.analyzed_lines>0
