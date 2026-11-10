"""Unit-local contract and sanitized telemetry through actual graph execution."""
import asyncio
import json
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from northstar_codereview.agents.code_quality import CodeQualityAgent
from northstar_codereview.chunk_review import build_plan, admission
from northstar_codereview.execution_budget import ExecutionBudget
from northstar_codereview.execution_profiles import get_profile
from northstar_codereview.models import AnalysisDiagnostics
from .test_chunk_review import run, snapshot
from .test_prepared_review import PreparedModel


@pytest.mark.parametrize('name,tools', [('standard',6),('extended_csv',9),('extended_bm25',11)])
def test_authorized_profile_reaches_unit_request(name,tools):
    p=get_profile(name)
    ledger=ExecutionBudget(p.global_limits(),execution_profile=p)
    model=PreparedModel()
    job=uuid4()
    agent=CodeQualityAgent(snapshot((2,)),model=model,approved_synthetic=True,
        prepared_context=True,chunk_analysis=True,execution_budget=ledger,limits=p.agent_limits())
    agent.synthesis_reservation=ledger.reserve_synthesis((job,),input_units=30000)[job]
    result=asyncio.run(agent.run(review_id=uuid4(),job_id=job))
    text=model.histories[0][0].content
    assert f'Authorized profile: {name}; model attempts <= 4; tool attempts <= {tools}' in text
    assert 'six-tool profile' not in text
    assert 'whole-repository completeness is not required' in text
    assert 'materially prevents responsible analysis' in text
    assert result.result.status=='completed'
    assert ledger.usage().input_reserved==sum(build_plan(agent.snapshot,execution_profile=p).input_units)


class Responses(PreparedModel):
    def __init__(self, modes):
        super().__init__()
        self.modes=modes

    async def ainvoke(self,messages,**kwargs):
        index=len(self.histories)
        mode=self.modes[min(index,len(self.modes)-1)]
        self.mode=mode
        message=await super().ainvoke(messages,**kwargs)
        if mode=='abstain':
            message=AIMessage(content='{"findings":[],"outcome":"insufficient_evidence"}',
                              response_metadata={'finish_reason':'stop'})
        if mode=='secret':
            message=AIMessage(content='PRIVATE_PROVIDER_VALUE',response_metadata={'finish_reason':'PRIVATE_FINISH'})
        usage=None if mode=='secret' else dict(input_tokens=100+index,output_tokens=10+index,total_tokens=110+2*index)
        return message.model_copy(update={'usage_metadata':usage})


@pytest.mark.parametrize('mode,status,classification,schema', [
    ('empty','completed','findings','valid'),
    ('abstain','partial','insufficient_evidence','valid'),
    ('missing','partial','invalid_output','invalid'),
    ('truncated','partial','invalid_output','invalid'),
    ('fabricated','partial','invalid_evidence','valid'),
    ('secret','partial','invalid_output','invalid')])
def test_unit_outcome_diagnostics_preserve_validation(mode,status,classification,schema):
    # A missing dependency is source data, not a license to invent findings.
    from northstar_codereview.local_source import _assemble
    source=_assemble({('head','app.py'):'from unavailable import transform\ndef local(x):\n    return transform(x)\n'})
    result,ledger=run(source,Responses([mode]))
    a=result.result.diagnostics.analysis
    assert result.result.status==status
    assert (a.analyzed_lines==3)==(mode=='empty')
    assert not result.validated_findings
    d=a.units[0]
    assert d.unit_id==1 and d.range_indices==(0,)
    assert d.classification==classification and d.schema_status==schema
    assert d.insufficient_evidence_reason=='unavailable'
    if mode=='secret':
        assert d.response.finish_reason=='unrecognized'
        assert d.reported_input_tokens is d.reported_output_tokens is None
    else:
        assert d.reported_input_tokens==100 and d.reported_output_tokens==10
    if mode=='truncated': assert d.response.truncated is True
    assert 'PRIVATE_' not in a.model_dump_json() and 'transform' not in a.model_dump_json()
    assert ledger.usage().model_attempts==1
    assert result.usage.retries==result.usage.repairs==0


def test_per_unit_diagnostics_do_not_overwrite_or_erase_valid_findings():
    result,_=run(snapshot((100,100,100,100,140),width=28),Responses(['valid','abstain','truncated']))
    a=result.result.diagnostics.analysis
    assert len(a.units)==a.required_model_attempts>=2
    assert a.units[0].classification=='findings'
    assert a.units[1].classification=='insufficient_evidence'
    assert a.units[0].reported_input_tokens==100 and a.units[1].reported_input_tokens==101
    assert a.units[0].range_indices!=a.units[1].range_indices
    assert result.validated_findings and result.result.status=='partial'
    assert a.analyzed_lines>0 and a.failed_lines>0


def test_legacy_analysis_diagnostics_keep_serialized_shape():
    data=admission(build_plan(snapshot((2,)))).model_dump(mode='json')
    data.pop('units',None)
    old=AnalysisDiagnostics.model_validate(data)
    assert old.units is None
    assert old.model_dump(mode='json')==data


def test_request_failure_retains_unavailable_unit_metadata():
    class Failed(PreparedModel):
        async def ainvoke(self,messages,**kwargs): raise TimeoutError()
    result,ledger=run(snapshot((2,)),Failed())
    d=result.result.diagnostics.analysis.units[0]
    assert d.classification=='request_failed' and d.schema_status=='unavailable'
    assert d.response is None and d.reported_input_tokens is None
    assert ledger.usage().model_attempts==1 and ledger.usage().unknown_usage_attempts==1


@pytest.mark.parametrize('field,value', [('classification','PRIVATE_RESPONSE'),
    ('insufficient_evidence_reason','PRIVATE_REASON'),('range_indices',(-1,)),
    ('range_indices',(999,))])
def test_unit_metadata_rejects_untrusted_labels_and_invalid_ranges(field,value):
    from pydantic import ValidationError
    result,_=run(snapshot((2,)),Responses(['empty']))
    data=result.result.diagnostics.analysis.model_dump(mode='json')
    data['units'][0][field]=value
    with pytest.raises(ValidationError): AnalysisDiagnostics.model_validate(data)


def test_earlier_finish_metadata_survives_later_response():
    result,_=run(snapshot((100,100,100,100,140),width=28),Responses(['truncated','empty']))
    units=result.result.diagnostics.analysis.units
    assert units[0].response.finish_reason=='length' and units[0].response.truncated
    assert units[1].response.finish_reason=='stop' and not units[1].response.truncated
    assert result.result.status=='partial'


def test_rejected_synthesis_tool_call_keeps_available_finish_and_usage():
    class ToolCall(PreparedModel):
        async def ainvoke(self,messages,**kwargs):
            return AIMessage(content='', tool_calls=[{'name':'read_source_lines','id':'fake-call',
                'args':{'private':'PRIVATE_ARGUMENT'},'type':'tool_call'}],
                response_metadata={'finish_reason':'tool_calls'},
                usage_metadata={'input_tokens':10,'output_tokens':5,'total_tokens':15})
    result,ledger=run(snapshot((2,)),ToolCall())
    d=result.result.diagnostics.analysis.units[0]
    assert d.classification=='invalid_output' and d.schema_status=='invalid'
    assert d.response.finish_reason=='tool_calls'
    assert d.reported_input_tokens==10 and d.reported_output_tokens==5
    assert 'PRIVATE_ARGUMENT' not in result.result.diagnostics.model_dump_json()
    assert ledger.usage().tool_attempts==1 and ledger.usage().model_attempts==1


@pytest.mark.parametrize('oversized',[False,True])
def test_known_usage_survives_preparse_rejection(oversized):
    class Rejected(PreparedModel):
        async def ainvoke(self,messages,**kwargs):
            return AIMessage(content='x'*131073 if oversized else '{"findings":[]}',
                usage_metadata={'input_tokens':10,'output_tokens':5 if oversized else 1001,
                                'total_tokens':15 if oversized else 1011})
    result,ledger=run(snapshot((2,)),Rejected())
    d=result.result.diagnostics.analysis.units[0]
    assert d.reported_input_tokens==10
    assert d.reported_output_tokens==(5 if oversized else 1001)
    assert d.response is None and d.schema_status=='unavailable'
    assert d.classification=='rejected_before_parse'
    assert result.result.status=='partial' and ledger.usage().model_attempts==1
