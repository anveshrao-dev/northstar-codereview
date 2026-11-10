"""Synthesis-only JSON mode through the installed SDK; no external transport."""
import asyncio
import json
from importlib import import_module
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI

from northstar_codereview.agents.code_quality import CodeQualityAgent, RunLimits
from northstar_codereview.execution_budget import ExecutionBudget, GlobalLimits
from northstar_codereview.providers.nebius import NebiusModel, ProviderRequestError, ProviderConfigurationError
from .test_code_quality import snapshot, READ, proposal, call
from .test_nebius import settings, completion
from .test_synthesis_gate import BoundModel


@pytest.mark.parametrize('content,status,diagnostic',[
    ('{"findings":[]}','succeeded',None),
    ('{"findings":[],"outcome":"insufficient_evidence"}','insufficient_evidence',None),
    ('PRIVATE invalid JSON','malformed_output','invalid_json'),
    ('{}','malformed_output','missing_required_fields'),
    ('{"findings":false}','malformed_output','invalid_field_types'),
    ('```json\n{"findings":[]}\n```','malformed_output','invalid_json'),
])
def test_actual_wire_inspection_then_synthesis(snapshot,content,status,diagnostic):
    requests=[]
    def respond(request):
        payload=json.loads(request.content)
        requests.append(payload)
        assert str(request.url)=='https://api.tokenfactory.nebius.com/v1/chat/completions'
        assert payload['model']=='synthetic-model'
        assert payload['max_completion_tokens']==1000
        if len(requests)==1:
            assert len(payload['tools'])==4 and 'response_format' not in payload
            return httpx.Response(200,json=completion({'content':None,'tool_calls':[{
                'id':'inspection-id','type':'function','function':{
                    'name':'read_source_lines','arguments':json.dumps(READ)}}]}))
        assert len(requests)==2
        assert payload['response_format']=={'type':'json_object'}
        assert 'extra_body' not in payload  # SDK merges it into the wire object.
        assert 'tools' not in payload and 'tool_choice' not in payload
        assistant=next(m for m in payload['messages'] if m['role']=='assistant')
        observation=next(m for m in payload['messages'] if m['role']=='tool')
        assert assistant['tool_calls'][0]['id']==observation['tool_call_id']=='inspection-id'
        return httpx.Response(200,json=completion({'content':content,'reasoning':None}))
    async def exercise():
        ledger=ExecutionBudget(GlobalLimits(max_model_requests=2))
        async with NebiusModel(settings(),allow_live=True,transport=httpx.MockTransport(respond)) as model:
            result=await CodeQualityAgent(snapshot,model=model,approved_synthetic=True,
                limits=RunLimits(max_model_calls=2,max_provider_retries=0,max_output_repairs=0),
                execution_budget=ledger).run(review_id=uuid4())
            assert result.result.synthesis==status
            assert result.usage.model_calls==ledger.usage().model_attempts==len(requests)==2
            assert result.usage.tool_attempts==ledger.usage().tool_attempts==1
            assert ledger.usage().output_reserved==2000
            assert result.usage.repairs==result.usage.retries==0
            assert not result.validated_findings
            if diagnostic:
                assert f'Final-output diagnostic: {diagnostic}.' in result.result.limitations
            assert 'PRIVATE' not in result.result.model_dump_json()
    asyncio.run(exercise())


@pytest.mark.parametrize('http_status',[400,422,429,503])
def test_provider_rejection_has_no_fallback_retry_or_repair(snapshot,http_status):
    requests=[]
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(http_status,text='PRIVATE synthetic-test-secret')
    async def exercise():
        async with NebiusModel(settings(),allow_live=True,transport=httpx.MockTransport(respond)) as model:
            result=await CodeQualityAgent(snapshot,model=model,approved_synthetic=True,
                limits=RunLimits(max_model_calls=1)).run(review_id=uuid4())
            assert result.result.synthesis=='request_failed'
            assert result.usage.model_calls==len(requests)==1
            assert result.usage.retries==result.usage.repairs==result.usage.tool_attempts==0
            assert 'PRIVATE' not in result.result.model_dump_json()
    asyncio.run(exercise())
    assert requests[0]['response_format']=={'type':'json_object'}


@pytest.mark.parametrize('module,classname',[
    ('code_quality','CodeQualityAgent'),('security','SecurityAgent'),('test_coverage','TestCoverageAgent'),
    ('gitops','GitOpsAgent'),('cicd_groovy','CICDGroovyAgent')])
def test_all_specialists_signal_json_only_during_synthesis(snapshot,module,classname):
    class PhaseModel(BoundModel):
        async def ainvoke(self,messages,**kwargs):
            assert kwargs.get('synthesis_json',False) is bool(self.histories)
            return await super().ainvoke(messages,**kwargs)
    model=PhaseModel(call(args=READ),AIMessage(content='{"findings":[]}'))
    cls=getattr(import_module('northstar_codereview.agents.'+module),classname)
    result=asyncio.run(cls(snapshot,model=model,approved_synthetic=True,
        limits=RunLimits(max_model_calls=2)).run(review_id=uuid4()))
    assert model.bindings[-1]==() and result.result.synthesis=='succeeded'


def test_format_is_per_invocation_not_sticky():
    formats=[]
    def respond(request):
        payload=json.loads(request.content)
        formats.append(payload.get('response_format'))
        return httpx.Response(200,json=completion({'content':'{"findings":[]}'}))
    async def exercise():
        async with NebiusModel(settings(),allow_live=True,transport=httpx.MockTransport(respond)) as model:
            model.bind_tools(())
            for enabled in (True,False):
                await model.ainvoke([HumanMessage(content='Fictional JSON test')],max_tokens=100,synthesis_json=enabled)
    asyncio.run(exercise())
    assert formats==[{'type':'json_object'},None]


@pytest.mark.parametrize('mutation',['omit','schema','tools'])
def test_wire_guard_rejects_serialization_drift_before_transport(monkeypatch,mutation):
    original=ChatOpenAI.ainvoke
    async def altered(self,*args,**kwargs):
        if mutation=='omit':kwargs.pop('extra_body',None)
        elif mutation=='schema':kwargs['extra_body']={'response_format':{'type':'json_schema'}}
        else:kwargs['tools']=[{'type':'function','function':{'name':'unapproved','parameters':{'type':'object'}}}]
        return await original(self,*args,**kwargs)
    monkeypatch.setattr(ChatOpenAI,'ainvoke',altered)
    calls=[]
    def respond(request):
        calls.append(True)
        return httpx.Response(200,json=completion({'content':'{}'}))
    async def exercise():
        async with NebiusModel(settings(),allow_live=True,transport=httpx.MockTransport(respond)) as model:
            with pytest.raises(ProviderRequestError):
                await model.bind_tools(()).ainvoke([HumanMessage(content='Fictional JSON test')],max_tokens=100,synthesis_json=True)
    asyncio.run(exercise())
    assert calls==[]


@pytest.mark.parametrize('forged',[False,True])
def test_json_flag_does_not_bypass_proposal_or_evidence_validation(snapshot,forged):
    def final(messages):
        item=proposal(messages)
        if forged:item['evidence'][0]['snippet']='FABRICATED'
        return AIMessage(content=json.dumps({'findings':[item]}))
    model=BoundModel(call(args=READ),final)
    result=asyncio.run(CodeQualityAgent(snapshot,model=model,approved_synthetic=True,
        limits=RunLimits(max_model_calls=2)).run(review_id=uuid4()))
    assert len(result.validated_findings)==(0 if forged else 1)


def test_json_request_rejected_if_inspection_tools_are_bound(snapshot):
    async def exercise():
        async with NebiusModel(settings(),allow_live=True,transport=httpx.MockTransport(
                lambda _:pytest.fail('transport must not run'))) as model:
            agent=CodeQualityAgent(snapshot,model=model,approved_synthetic=True)
            with pytest.raises(ProviderConfigurationError):
                await agent.model.ainvoke([HumanMessage(content='JSON')],max_tokens=100,synthesis_json=True)
    asyncio.run(exercise())


@pytest.mark.parametrize('flag',[1,None,'json_schema'])
def test_non_boolean_flags_rejected_before_transport(flag):
    async def exercise():
        async with NebiusModel(settings(),allow_live=True,transport=httpx.MockTransport(
                lambda _:pytest.fail('transport must not run'))) as model:
            with pytest.raises(ProviderConfigurationError):
                await model.bind_tools(()).ainvoke([HumanMessage(content='JSON')],max_tokens=100,synthesis_json=flag)
    asyncio.run(exercise())


def test_parallel_reserved_synthesis_keeps_shared_accounting(snapshot):
    class PhaseModel(BoundModel):
        async def ainvoke(self,messages,**kwargs):
            assert kwargs.get('synthesis_json',False) is bool(self.histories)
            return await super().ainvoke(messages,**kwargs)
    async def exercise():
        ledger=ExecutionBudget(GlobalLimits())
        jobs=[uuid4() for _ in range(3)]
        permits=ledger.reserve_synthesis(tuple(jobs))
        agents=[]
        for job in jobs:
            agent=CodeQualityAgent(snapshot,model=PhaseModel(call(args=READ),AIMessage(content='{"findings":[]}')),
                approved_synthetic=True,limits=RunLimits(max_model_calls=2),execution_budget=ledger)
            agent.synthesis_reservation=permits[job]
            agents.append(agent)
        results=await asyncio.gather(*(agent.run(review_id=uuid4(),job_id=job) for agent,job in zip(agents,jobs)))
        assert all(r.result.synthesis=='succeeded' for r in results)
        assert ledger.usage().model_attempts==6 and ledger.usage().tool_attempts==3
        assert ledger.usage().output_reserved==6000
        assert all(r.usage.repairs==r.usage.retries==0 for r in results)
    asyncio.run(exercise())


def test_adapter_without_new_option_fails_closed_no_prompt_fallback(snapshot):
    class Unsupported(BoundModel):
        async def ainvoke(self,messages,*,max_tokens):
            raise AssertionError('unsupported synthesis option must fail before invocation')
    model=Unsupported()
    result=asyncio.run(CodeQualityAgent(snapshot,model=model,approved_synthetic=True,
        limits=RunLimits(max_model_calls=1)).run(review_id=uuid4()))
    assert result.result.synthesis=='request_failed' and result.usage.model_calls==1
    assert result.usage.retries==result.usage.repairs==0 and not result.validated_findings


def test_inspection_cannot_acquire_json_mode_via_serialization_drift(monkeypatch):
    original=ChatOpenAI.ainvoke
    async def altered(self,*args,**kwargs):
        kwargs['extra_body']={'response_format':{'type':'json_object'}}
        return await original(self,*args,**kwargs)
    monkeypatch.setattr(ChatOpenAI,'ainvoke',altered)
    calls=[]
    def respond(request):
        calls.append(True)
        return httpx.Response(200,json=completion({'content':'{}'}))
    async def exercise():
        async with NebiusModel(settings(),allow_live=True,transport=httpx.MockTransport(respond)) as model:
            with pytest.raises(ProviderRequestError):
                await model.bind_tools(()).ainvoke([HumanMessage(content='JSON')],max_tokens=100)
            assert model._active_synthesis_json is None
    asyncio.run(exercise())
    assert calls==[]
