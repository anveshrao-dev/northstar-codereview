"""Bounded synthesis uses real graphs and tools, never a provider connection."""
import asyncio
import json
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from northstar_codereview.agents.code_quality import CodeQualityAgent, RunLimits, TransientModelError
from northstar_codereview.execution_budget import ExecutionBudget, GlobalLimits
from .test_code_quality import snapshot, ScriptedModel, call, READ, proposal


class BoundModel(ScriptedModel):
    def bind_tools(self, tools):
        self.bound=tuple(t.name for t in tools)
        return self
    async def ainvoke(self,messages,**kwargs):
        self.bindings=getattr(self,'bindings',[])+[self.bound]
        return await super().ainvoke(messages,**kwargs)


def run(snapshot,model,ledger=None,**limits):
    return asyncio.run(CodeQualityAgent(snapshot,model=model,approved_synthetic=True,
        limits=RunLimits(max_model_calls=3,**limits),execution_budget=ledger).run(review_id=uuid4()))


def test_last_individual_call_is_tool_free_synthesis_with_valid_evidence(snapshot):
    def final(messages):
        assert isinstance(messages[-1],ToolMessage) and messages[-1].tool_call_id=='read'
        return AIMessage(content=json.dumps({'findings':[proposal(messages)]}))
    model=BoundModel(call('list_snapshot_files'),call(args=READ,ident='read'),final)
    output=run(snapshot,model)
    assert [len(x) for x in model.bindings]==[4,4,0]
    assert output.usage.model_calls==3 and output.usage.tool_attempts==2
    assert output.result.synthesis=='succeeded' and output.result.status=='completed'
    assert len(output.validated_findings)==1


@pytest.mark.parametrize('content,status',[
 ('{"findings":[]}','succeeded'),
 ('{"findings":[],"outcome":"insufficient_evidence"}','insufficient_evidence'),
 ('RAW_PRIVATE_MALFORMED','malformed_output'),
 ('{"findings":[{}],"outcome":"insufficient_evidence"}','malformed_output')])
def test_synthesis_outcomes_preserve_incomplete_coverage(snapshot,content,status):
    model=BoundModel(call('list_snapshot_files'),call('list_snapshot_files',ident='second'),AIMessage(content=content))
    output=run(snapshot,model)
    assert output.result.synthesis==status and output.result.status=='partial'
    assert not output.validated_findings and not output.result.reviewed_files
    assert output.usage.repairs==0 and len(model.histories)==3
    assert 'RAW_PRIVATE' not in output.result.model_dump_json()


def test_synthesis_tool_response_is_not_executed(snapshot):
    model=BoundModel(call(args=READ),call(args=READ,ident='second'),call(args=READ,ident='forbidden'))
    output=run(snapshot,model)
    assert output.result.synthesis=='tool_call_rejected'
    assert output.usage.tool_attempts==2 and len(output.events)==2 and output.result.status=='partial'


def test_synthesis_request_failure_has_no_retry(snapshot):
    model=BoundModel(call(args=READ),call(args=READ,ident='second'),TransientModelError())
    output=run(snapshot,model)
    assert output.result.synthesis=='request_failed'
    assert output.usage.retries==0 and output.usage.repairs==0 and output.usage.model_calls==3


def test_global_budget_denies_synthesis_explicitly(snapshot):
    ledger=ExecutionBudget(GlobalLimits(max_model_requests=2))
    model=BoundModel(call(args=READ),call(args=READ,ident='second'))
    output=run(snapshot,model,ledger)
    assert output.result.synthesis=='budget_denied'
    assert ledger.usage().model_attempts==output.usage.model_calls==2
    assert any('Synthesis' in s and 'budget' in s for s in output.result.limitations)


def test_recovery_remains_partial_after_valid_synthesis(snapshot):
    model=BoundModel(call(args={**READ,'end_line':10}),call('list_snapshot_files',ident='list'),AIMessage(content='{"findings":[]}'))
    output=run(snapshot,model)
    assert output.result.synthesis=='succeeded' and output.result.status=='partial'
    assert output.result.inspection.recovered_errors==1 and output.usage.tool_attempts==3


def test_synthesis_does_not_bypass_evidence_checks(snapshot):
    def forged(messages):
        p=proposal(messages)
        p['evidence'][0]['snippet']='FABRICATED'
        return AIMessage(content=json.dumps({'findings':[p]}))
    output=run(snapshot,BoundModel(call('list_snapshot_files'),call(args=READ,ident='read'),forged))
    assert output.result.synthesis=='succeeded' and not output.validated_findings
    assert output.result.status=='partial'


def test_parallel_competition_never_overdraws_shared_budget(snapshot):
    async def scenario():
        ledger=ExecutionBudget(GlobalLimits(max_model_requests=5))
        arrived=0
        ready=asyncio.Event()
        class Concurrent(BoundModel):
            async def ainvoke(self,messages,**kwargs):
                nonlocal arrived
                if not self.histories:
                    arrived+=1
                    if arrived==3: ready.set()
                    await ready.wait()
                return await super().ainvoke(messages,**kwargs)
        agents=[CodeQualityAgent(snapshot,model=Concurrent(call(args=READ),AIMessage(content='{"findings":[]}')),
            approved_synthetic=True,limits=RunLimits(max_model_calls=2),execution_budget=ledger) for _ in range(3)]
        output=await asyncio.wait_for(asyncio.gather(*(a.run(review_id=uuid4()) for a in agents)),5)
        assert ledger.usage().model_attempts==sum(r.usage.model_calls for r in output)==5
        assert sorted(r.result.synthesis for r in output)==['budget_denied','succeeded','succeeded']
        assert sum(r.result.status=='completed' for r in output)==2
    asyncio.run(scenario())


def test_actual_adapter_omits_tools_and_replays_ids_in_synthesis(snapshot):
    import httpx
    from northstar_codereview.providers.nebius import NebiusModel
    from .test_nebius import completion,settings
    requests=[]
    async def respond(request):
        payload=json.loads(request.content)
        requests.append(payload)
        if len(requests)==1:
            assert len(payload['tools'])==4
            return httpx.Response(200,json=completion({'content':None,'tool_calls':[
                {'id':'inspect','type':'function','function':{'name':'read_source_lines','arguments':json.dumps(READ)}}]}))
        assert len(requests)==2 and 'tools' not in payload
        assistant=next(m for m in payload['messages'] if m['role']=='assistant')
        observation=next(m for m in payload['messages'] if m['role']=='tool')
        assert assistant['tool_calls'][0]['id']==observation['tool_call_id']=='inspect'
        return httpx.Response(200,json=completion({'content':'{"findings":[]}'}))
    async def exercise():
        async with NebiusModel(settings(),allow_live=True,transport=httpx.MockTransport(respond)) as model:
            result=await CodeQualityAgent(snapshot,model=model,approved_synthetic=True,
                limits=RunLimits(max_model_calls=2)).run(review_id=uuid4())
            assert result.result.synthesis=='succeeded' and result.result.status=='completed'
            assert result.usage.model_calls==2 and result.usage.tool_attempts==1
    asyncio.run(exercise())


def test_one_call_budget_synthesizes_insufficient_evidence_without_tools(snapshot):
    model=BoundModel(AIMessage(content='{"findings":[],"outcome":"insufficient_evidence"}'))
    result=asyncio.run(CodeQualityAgent(snapshot,model=model,approved_synthetic=True,
        limits=RunLimits(max_model_calls=1)).run(review_id=uuid4()))
    assert model.bindings==[()] and result.usage.model_calls==1 and result.usage.tool_attempts==0
    assert result.result.status=='partial' and result.result.synthesis=='insufficient_evidence'


@pytest.mark.parametrize('module,name',[
 ('code_quality','CodeQualityAgent'),('security','SecurityAgent'),('test_coverage','TestCoverageAgent'),
 ('gitops','GitOpsAgent'),('cicd_groovy','CICDGroovyAgent')])
def test_all_specialists_preserve_synthesis_status(snapshot,module,name):
    from importlib import import_module
    cls=getattr(import_module('northstar_codereview.agents.'+module),name)
    model=BoundModel(call(args=READ),AIMessage(content='{"findings":[]}'))
    result=asyncio.run(cls(snapshot,model=model,approved_synthetic=True,limits=RunLimits(max_model_calls=2)).run(review_id=uuid4()))
    assert result.result.synthesis=='succeeded' and model.bindings[-1]==()
    assert result.usage.model_calls==2 and result.usage.tool_attempts==1


def test_exact_tool_attempt_limit_transitions_to_synthesis(snapshot):
    model=BoundModel(call(args=READ),AIMessage(content='{"findings":[]}'))
    result=run(snapshot,model,max_tool_attempts=1)
    assert result.result.synthesis=='succeeded' and model.bindings[-1]==()
    assert result.usage.model_calls==2 and result.usage.tool_attempts==1


def test_oversized_batch_preserves_reply_ids_without_executing_excess(snapshot):
    first=AIMessage(content='',tool_calls=[dict(name='read_source_lines',args=READ,id=i) for i in ('one','two')])
    def final(messages):
        replies=[m for m in messages if isinstance(m,ToolMessage)]
        assert [m.tool_call_id for m in replies]==['one','two']
        assert json.loads(replies[-1].content)['status']=='blocked'
        return AIMessage(content='{"findings":[]}')
    model=BoundModel(first,final)
    result=run(snapshot,model,max_tool_attempts=1)
    assert result.result.synthesis=='succeeded' and result.result.status=='partial'
    assert result.usage.tool_attempts==len(result.events)==1 and result.usage.model_calls==2


def test_global_stop_during_inspection_marks_synthesis_unavailable(snapshot):
    ledger=ExecutionBudget(GlobalLimits())
    def stopped(messages):
        ledger.stop('model_requests')
        return call(args=READ)
    result=run(snapshot,BoundModel(stopped),ledger)
    assert result.result.synthesis=='budget_denied' and result.result.status=='partial'
    assert result.usage.model_calls==1 and result.usage.tool_attempts==0
    assert any('Synthesis budget' in note for note in result.result.limitations)


def test_sequential_reuse_restores_inspection_binding(snapshot):
    async def scenario():
        model=BoundModel(call(args=READ),AIMessage(content='{"findings":[]}'),
                         call(args=READ),AIMessage(content='{"findings":[]}'))
        agent=CodeQualityAgent(snapshot,model=model,approved_synthetic=True,limits=RunLimits(max_model_calls=2))
        first=await agent.run(review_id=uuid4())
        second=await agent.run(review_id=uuid4())
        assert first.result.synthesis==second.result.synthesis=='succeeded'
        assert [len(x) for x in model.bindings]==[4,0,4,0]
    asyncio.run(scenario())
