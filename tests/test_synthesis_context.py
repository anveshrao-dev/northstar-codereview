"""Generated two-file histories; actual LangGraph/tools/ledger, no live model."""
import asyncio
import json
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from northstar_codereview.agents.code_quality import CodeQualityAgent, RunLimits, input_reservation
from northstar_codereview.execution_budget import ExecutionBudget, GlobalLimits
from northstar_codereview.local_source import collect_local_snapshot
from northstar_codereview.models import ReviewRequest


def generated_snapshot(tmp_path, *, width=36, newline='\n'):
    for name in ('module.py', 'test_module.py'):
        text = ''.join(f'value_{i:03} = {i}  # ' + 'x' * width + newline for i in range(69))
        (tmp_path/name).write_bytes(text.encode())
    return collect_local_snapshot(ReviewRequest(mode='local', local_path='.'), source_root=tmp_path)


class InspectThenSynthesize:
    def __init__(self, final='empty', verbose=0):
        self.histories = []
        self.final = final
        self.verbose = verbose

    def bind_tools(self, tools):
        self.tools = tools
        return self

    async def ainvoke(self, messages, **kwargs):
        self.histories.append(list(messages))
        index = len(self.histories)
        if self.tools:
            assert not kwargs.get('synthesis_json') and index <= 2
            calls = ([dict(name='read_source_lines', args=dict(file_path=path, start_line=1,end_line=69), id=f'read-{i}')
                      for i,path in enumerate(('module.py','test_module.py'))] if index == 1 else
                     [dict(name='get_evidence', args=dict(file_path='module.py',start_line=1,end_line=69),id='citation')])
            return AIMessage(content='v'*self.verbose, tool_calls=calls)
        assert kwargs['synthesis_json'] is True and index <= 3
        findings = []
        if self.final != 'empty':
            evidence = json.loads(next(m.content for m in messages if isinstance(m,ToolMessage)))['evidence']
            if self.final == 'forged': evidence[0]['snippet'] = 'UNOBSERVED SOURCE'
            findings = [dict(title='Synthetic claim',category='correctness',severity='low',confidence=0.5,
                explanation='A generated claim requiring independent assessment.',
                suggested_remediation='Verify the behavior against the missing dependency contract.',
                assumptions=['Supporting dependency implementation is unavailable.'], evidence=evidence)]
        return AIMessage(content=json.dumps({'findings':findings}),response_metadata={'finish_reason':'stop'})


def execute(snapshot, model):
    ledger = ExecutionBudget(GlobalLimits(max_model_requests=4,max_tool_attempts=6,max_input_tokens=80000,max_output_tokens=4000))
    owner = uuid4()
    agent = CodeQualityAgent(snapshot, model=model, approved_synthetic=True, execution_budget=ledger,
        limits=RunLimits(max_model_calls=3,max_tool_attempts=6,max_provider_retries=0,max_output_repairs=0,
                         max_input_tokens=80000,max_output_tokens=4000,request_timeout_seconds=20,run_timeout_seconds=60))
    agent.synthesis_reservation = ledger.reserve_synthesis((owner,),input_units=30000)[owner]
    result = asyncio.run(agent.run(review_id=uuid4(),job_id=owner))
    return result, ledger


def test_duplicate_source_history_can_synthesize_within_original_allowance(tmp_path):
    snapshot = generated_snapshot(tmp_path)
    model = InspectThenSynthesize()
    result, ledger = execute(snapshot, model)
    assert result.result.synthesis == 'succeeded'
    d = result.result.diagnostics.synthesis_context
    assert d.before.projected_units > 30000 >= d.after.projected_units
    assert d.protected_allowance == 30000 and d.compaction_attempted
    assert d.admission == 'admitted'
    assert result.result.diagnostics.fully_inspected_files == 2
    assert result.result.diagnostics.observed_lines == 138
    assert result.usage.model_calls == ledger.usage().model_attempts == 3
    assert result.usage.tool_attempts == 3 and result.usage.repairs == result.usage.retries == 0
    assert ledger.usage().input_reserved <= 80000
    assert not result.validated_findings and result.result.status == 'completed'


@pytest.mark.parametrize('final', ['empty','valid','forged'])
@pytest.mark.parametrize('newline', ['\n','\r\n'])
def test_compaction_preserves_exact_references_replay_and_validation(tmp_path, final, newline):
    snapshot = generated_snapshot(tmp_path,newline=newline)
    original = dict(snapshot.sources)
    model = InspectThenSynthesize(final)
    result, ledger = execute(snapshot,model)
    assert result.result.synthesis == 'succeeded'
    history = model.histories[-1]
    refs = []
    for message in history:
        if isinstance(message,ToolMessage):
            observation = json.loads(message.content)
            assert observation['data'] is not None
            assert 'text' not in observation['data'] and 'snippet' not in observation['data']
            from northstar_codereview.models import ToolResult
            ToolResult.model_validate(observation)
            refs.extend(observation['evidence'])
            ref = observation['evidence'][0]
            assert ref['snapshot_id'] == str(snapshot.manifest.snapshot_id)
            assert ref['side'] == 'head' and ref['start_line'] == 1 and ref['end_line'] == 69
            assert ref['snippet'] == original[('head',ref['file_path'])]
    original_refs = [ref.model_dump(mode='json') for event in result.events for ref in event.observation.evidence]
    assert refs == original_refs  # Including the repeated citation: no observation dropped.
    assert snapshot.sources == original
    assistants = [m for m in history if isinstance(m,AIMessage)]
    assert all(any(m is prior for prior in model.histories[-2]) for m in assistants[:1])
    assert [c['id'] for m in assistants for c in m.tool_calls] == [m.tool_call_id for m in history if isinstance(m,ToolMessage)]
    schema = json.loads(history[0].content.split('Final response JSON schema:\n')[1])
    assert schema['required'] == ['findings']
    assert 'dependency' in history[0].content
    assert len(result.validated_findings) == (1 if final == 'valid' else 0)
    if final == 'valid':
        assert result.validated_findings[0].proposal.assumptions == ('Supporting dependency implementation is unavailable.',)
    if final == 'forged': assert result.result.status == 'partial'
    assert ledger.usage().model_attempts == len(model.histories) == 3
    assert result.usage.repairs == result.usage.retries == 0


def test_numeric_breakdown_matches_unchanged_reservation_and_redacts_content(tmp_path):
    snapshot = generated_snapshot(tmp_path)
    model = InspectThenSynthesize()
    result,_ = execute(snapshot,model)
    d = result.result.diagnostics.synthesis_context
    assert d.before.message_count == d.after.message_count == 8
    assert d.before.duplicate_source_payloads == 3 and d.after.duplicate_source_payloads == 0
    assert d.before.repeated_evidence_references == d.after.repeated_evidence_references == 1
    assert d.after.projected_units == input_reservation(model.histories[-1], [])
    for size in (d.before,d.after):
        contributions = [size.system_units,size.human_units,size.assistant_units,size.tool_units,
                         size.other_units,size.tool_schema_units,size.framing_units]
        assert sum(contributions) == size.projected_units
        assert all(type(value) is int and value >= 0 for value in size.model_dump().values())
    assert d.before.system_units == d.after.system_units
    assert d.before.human_units == d.after.human_units
    assert d.before.assistant_units == d.after.assistant_units
    assert d.before.tool_units > d.after.tool_units
    assert 'value_000' not in result.result.model_dump_json()
    assert 'xxxxxxx' not in result.result.model_dump_json()


def test_irreducible_history_fails_closed_without_synthesis_request(tmp_path):
    result,ledger = execute(generated_snapshot(tmp_path),InspectThenSynthesize(verbose=40000))
    d = result.result.diagnostics.synthesis_context
    assert d.compaction_attempted and d.after.projected_units > 30000
    assert d.admission == 'denied' and result.result.synthesis == 'budget_denied'
    assert result.usage.model_calls == ledger.usage().model_attempts == 1
    assert not result.validated_findings and result.result.status == 'partial'
    assert 'vvvvvv' not in result.result.model_dump_json()


def test_small_context_is_not_rewritten(tmp_path):
    result,_ = execute(generated_snapshot(tmp_path,width=0),InspectThenSynthesize())
    d = result.result.diagnostics.synthesis_context
    assert not d.compaction_attempted and d.before == d.after
    assert d.admission == 'admitted'


def test_reduction_is_deterministic_idempotent_and_does_not_mutate_messages(tmp_path):
    from northstar_codereview.synthesis_context import compact_synthesis_messages
    snapshot = generated_snapshot(tmp_path)
    model = InspectThenSynthesize()
    result,_ = execute(snapshot,model)
    messages = model.histories[-2]
    before = [m.model_dump() for m in messages]
    observed = [ref for event in result.events for ref in event.observation.evidence]
    first = compact_synthesis_messages(messages,observed)
    second = compact_synthesis_messages(messages,observed)
    assert first == second == compact_synthesis_messages(first,observed)
    assert [m.model_dump() for m in messages] == before
    assert input_reservation(first,[]) < input_reservation(messages,[])


@pytest.mark.parametrize('mutation',['text','identity','extra','unobserved','failed','unknown_tool'])
def test_nonredundant_or_untrusted_payloads_are_not_removed(tmp_path,mutation):
    from northstar_codereview.synthesis_context import compact_synthesis_messages
    snapshot = generated_snapshot(tmp_path)
    model = InspectThenSynthesize()
    result,_ = execute(snapshot,model)
    original = next(m for m in model.histories[-2] if isinstance(m,ToolMessage))
    value = json.loads(original.content)
    observed = [ref for event in result.events for ref in event.observation.evidence]
    if mutation == 'text': value['data']['text'] += 'MATERIAL_INFORMATION'
    if mutation == 'identity': value['data']['file_path'] = 'different.py'
    if mutation == 'extra': value['data']['additional'] = 'MATERIAL_INFORMATION'
    if mutation == 'unobserved': observed = []
    if mutation == 'failed': value['status'] = 'error'; value['error'] = 'Synthetic failure'
    if mutation == 'unknown_tool': value['tool_name'] = 'unknown_tool'
    changed = original.model_copy(update={'content':json.dumps(value)})
    assert compact_synthesis_messages([changed],observed) == [changed]


def test_compacted_history_passes_actual_sdk_replay_guard_without_extra_requests(tmp_path):
    import httpx
    from northstar_codereview.providers.nebius import NebiusModel
    from .test_nebius import settings, completion
    snapshot = generated_snapshot(tmp_path)
    requests = []
    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        index = len(requests)
        assert index <= 3 and body['max_completion_tokens'] == 1000
        if 'response_format' not in body:
            assert index <= 2 and len(body['tools']) == 4
            specs = ([('read_source_lines',path,f'read-{i}') for i,path in enumerate(('module.py','test_module.py'))]
                     if index == 1 else [('get_evidence','module.py','citation')])
            calls = [dict(id=ident,type='function',function=dict(name=name,
                arguments=json.dumps(dict(file_path=path,start_line=1,end_line=69)))) for name,path,ident in specs]
            return httpx.Response(200,json=completion({'content':None,'tool_calls':calls}))
        assert body['response_format'] == {'type':'json_object'} and 'tools' not in body
        assistants = [m for m in body['messages'] if m['role']=='assistant']
        observations = [m for m in body['messages'] if m['role']=='tool']
        assert [c['id'] for m in assistants for c in m['tool_calls']] == [m['tool_call_id'] for m in observations]
        assert len(observations) == 3
        for m in observations:
            value = json.loads(m['content'])
            assert 'text' not in value['data'] and 'snippet' not in value['data']
            ref = value['evidence'][0]
            assert ref['snippet'] == snapshot.sources[('head',ref['file_path'])]
        return httpx.Response(200,json=completion({'content':'{"findings":[]}'}))
    async def exercise():
        ledger = ExecutionBudget(GlobalLimits(max_model_requests=4,max_tool_attempts=6,max_input_tokens=80000,max_output_tokens=4000))
        owner = uuid4()
        async with NebiusModel(settings(),allow_live=True,transport=httpx.MockTransport(respond)) as model:
            agent = CodeQualityAgent(snapshot,model=model,approved_synthetic=True,execution_budget=ledger,
                limits=RunLimits(max_model_calls=3,max_tool_attempts=6,max_input_tokens=80000,max_output_tokens=4000,
                                 max_provider_retries=0,max_output_repairs=0))
            agent.synthesis_reservation = ledger.reserve_synthesis((owner,),input_units=30000)[owner]
            result = await agent.run(review_id=uuid4(),job_id=owner)
            assert result.result.synthesis == 'succeeded'
            assert result.result.diagnostics.synthesis_context.compaction_attempted
            assert result.usage.model_calls == ledger.usage().model_attempts == len(requests) == 3
            assert result.usage.repairs == result.usage.retries == 0
            assert 'synthetic-test-secret' not in result.result.model_dump_json()
            assert 'value_000' not in result.result.model_dump_json()
    asyncio.run(exercise())


def test_report_before_context_telemetry_still_has_same_digest(tmp_path):
    from hashlib import sha256
    from northstar_codereview.orchestrator import OrchestrationResult, SpecialistExecution
    from northstar_codereview.reporting import build_report, ReviewReport
    from northstar_codereview.models import AgentName
    snapshot = generated_snapshot(tmp_path)
    result,_ = execute(snapshot,InspectThenSynthesize())
    outcome = OrchestrationResult(review_id=result.result.review_id,snapshot_id=snapshot.manifest.snapshot_id,
        selected_specialists=(AgentName.CODE_QUALITY,),
        specialist_results=(SpecialistExecution(result=result.result,usage=result.usage,
                                                validated_findings=result.validated_findings),),
        validated_findings=(),limitations=(),failures=(),status=result.result.status)
    report = build_report(outcome,snapshot=snapshot.manifest)
    value = report.model_dump(mode='json',exclude={'report_id'})
    del value['specialists'][0]['diagnostics']['synthesis_context']
    value['report_id'] = sha256(json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    restored = ReviewReport.model_validate(value)
    assert restored.model_dump(mode='json') == value
    assert restored.specialists[0].diagnostics.synthesis_context is None


def test_reduction_preserves_status_recovery_and_all_nontext_fields(tmp_path):
    from northstar_codereview.synthesis_context import compact_synthesis_messages
    snapshot = generated_snapshot(tmp_path)
    model = InspectThenSynthesize()
    result,_ = execute(snapshot,model)
    original = next(m for m in model.histories[-2] if isinstance(m,ToolMessage))
    value = json.loads(original.content)
    value['recovery'] = dict(original_call_id=str(uuid4()),original_category='invalid_line_range',
        original_guidance='use_captured_line_bounds',line_count=69,action='clamp_end_line',outcome='recovered')
    with_recovery = original.model_copy(update={'content':json.dumps(value)})
    observed = [ref for event in result.events for ref in event.observation.evidence]
    compacted = compact_synthesis_messages([with_recovery],observed)[0]
    actual = json.loads(compacted.content)
    expected = json.loads(with_recovery.content)
    del expected['data']['text']
    assert actual == expected
    assert compacted.tool_call_id == with_recovery.tool_call_id
    assert compacted.artifact is with_recovery.artifact
