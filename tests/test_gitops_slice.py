"""Independent fictional GitOps slice, exercised with model-selected tool calls."""
import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage

from northstar_codereview import gitops_slice as live
from northstar_codereview.models import AgentName
from .test_code_quality import call
from .test_synthesis_gate import BoundModel
from .test_nebius import completion

ENV={'NEBIUS_API_KEY':'synthetic-test-secret','NEBIUS_MODEL':live.MODEL}
READ={'file_path':'resources.yaml','start_line':1,'end_line':32}


class Fake(BoundModel):
    async def __aexit__(self,*args):self.closed=True


def finding(messages, *, forged=False):
    assert isinstance(messages[-1],ToolMessage)
    observation=json.loads(messages[-1].content)
    evidence=observation['evidence']
    if forged:evidence[0]['snippet']='PRIVATE fabricated quotation'
    return {'title':'Service selector does not match the captured pod labels','category':'gitops','severity':'medium',
        'confidence':0.9,'explanation':'The captured Service selects a different app label from the captured Deployment pods in the same namespace.',
        'suggested_remediation':'Align the Service selector with the intended pod labels after confirming intent.',
        'assumptions':['Only these captured resources are assessed; external cluster inventory is unknown.'],
        'evidence':evidence}


def run_fake(model,**kwargs):
    selected=[]
    def factory(settings,timeout):
        selected.append(True)
        assert settings.model==live.MODEL and timeout<=20
        return model
    output=asyncio.run(live.run_slice(allow_live=True,environ=ENV,provider_factory=factory,**kwargs))
    assert len(selected)==1 and model.closed
    return output


def test_disabled_before_fixture_or_credentials(monkeypatch):
    def forbidden(*a,**k):raise AssertionError('must not load anything')
    monkeypatch.setattr(live,'load_fixture',forbidden)
    monkeypatch.setattr(live,'load_nebius_settings',forbidden)
    assert asyncio.run(live.run_slice()).reason=='disabled'


def test_fixture_and_labels_are_pinned_and_separate():
    snapshot,dataset=live.load_fixture()
    assert tuple(snapshot.sources)==(('head','resources.yaml'),)
    assert live.fingerprint(dataset)==live.DATASET_DIGEST
    assert dataset.scenarios[0].specialists==(AgentName.GITOPS,)
    assert len(dataset.scenarios[0].expected)==len(dataset.scenarios[0].controls)==1


def test_only_gitops_real_graph_flow_and_pending_semantic_judgment(monkeypatch):
    from northstar_codereview.orchestrator import ReviewOrchestrator
    def forbidden(*a,**k):raise AssertionError('outer orchestrator must not run')
    monkeypatch.setattr(ReviewOrchestrator,'__init__',forbidden)
    model=Fake(call(args=READ,ident='inspect'),lambda m:AIMessage(content=json.dumps({'findings':[finding(m)]})))
    output=run_fake(model,limits=live.SliceLimits(max_model_requests=2))
    assert output.reason=='completed'
    assert [s.agent_name for s in output.report.specialists]==[AgentName.GITOPS]
    assert len(output.report.findings)==1 and output.evaluation.quality.pending==1
    assert output.evaluation.quality.true_positives==0
    assert output.evaluation.controls[0].status=='pending'
    assert output.budget_usage.model_attempts==2 and output.budget_usage.tool_attempts==1
    assert model.bindings==[('list_snapshot_files','read_source_lines','get_evidence','get_source_metadata'),()]
    prompts=' '.join(str(m.content) for history in model.histories for m in history if m.type=='system')
    assert 'The required findings array' in prompts
    assert 'service_selector_mismatch' not in prompts and 'deployment_selector_consistent' not in prompts


@pytest.mark.parametrize('content,synthesis',[
    ('{"findings":[]}','succeeded'),
    ('{"findings":[],"outcome":"insufficient_evidence"}','insufficient_evidence'),
    ('{}','malformed_output'),('PRIVATE invalid','malformed_output')])
def test_incomplete_or_malformed_results_never_pass_control(content,synthesis):
    output=run_fake(Fake(AIMessage(content=content)),limits=live.SliceLimits(max_model_requests=1))
    assert output.reason=='partial' and output.report.specialists[0].synthesis==synthesis
    assert output.evaluation.controls[0].status=='not_evaluated'
    assert not output.report.findings
    assert 'PRIVATE' not in live.render_slice(output)


def test_fabricated_evidence_rejected():
    output=run_fake(Fake(call(args=READ),lambda m:AIMessage(content=json.dumps({'findings':[finding(m,forged=True)]}))),
        limits=live.SliceLimits(max_model_requests=2))
    assert not output.report.findings and output.reason=='partial'
    assert 'Proposal 1 rejected: exact_source_mismatch.' in output.report.limitations


def test_actual_adapter_synthesis_wire_and_replay():
    requests=[]
    def respond(request):
        payload=json.loads(request.content);requests.append(payload)
        if len(requests)==1:
            return httpx.Response(200,json=completion({'content':None,'tool_calls':[{'id':'read','type':'function',
                'function':{'name':'read_source_lines','arguments':json.dumps(READ)}}]}))
        return httpx.Response(200,json=completion({'content':'{"findings":[]}'}))
    def factory(settings,timeout):return live.NebiusModel(settings,allow_live=True,timeout_seconds=timeout,transport=httpx.MockTransport(respond))
    output=asyncio.run(live.run_slice(allow_live=True,environ=ENV,provider_factory=factory,
        limits=live.SliceLimits(max_model_requests=2)))
    assert output.reason=='completed' and len(requests)==2
    assert len(requests[0]['tools'])==4 and 'response_format' not in requests[0]
    assert requests[1]['response_format']=={'type':'json_object'} and 'tools' not in requests[1]
    assistant=next(m for m in requests[1]['messages'] if m['role']=='assistant')
    observation=next(m for m in requests[1]['messages'] if m['role']=='tool')
    assert assistant['tool_calls'][0]['id']==observation['tool_call_id']=='read'


@pytest.mark.parametrize('field,value',[('max_model_requests',5),('max_tool_attempts',7),('max_input_tokens',80001),
    ('max_output_tokens',4001),('output_tokens_per_call',1001),('request_timeout_seconds',21),('evaluation_timeout_seconds',61)])
def test_limits_cannot_increase(field,value):
    output=asyncio.run(live.run_slice(allow_live=True,environ=ENV,limits=live.SliceLimits().model_copy(update={field:value})))
    assert output.reason=='configuration_error' and output.budget_usage.model_attempts==0


@pytest.mark.parametrize('option',['--source=/tmp/PRIVATE','--scenario=java_backend','--max-model-requests=100','--allow-liv'])
def test_cli_no_overrides(option,capsys):
    with pytest.raises(SystemExit) as error:live.main([option])
    assert error.value.code==2 and 'PRIVATE' not in capsys.readouterr().err


def test_changed_pin_stops_before_provider(monkeypatch):
    monkeypatch.setattr(live,'DATASET_DIGEST','0'*64)
    output=asyncio.run(live.run_slice(allow_live=True,environ=ENV))
    assert output.reason=='fixture_rejected' and not output.started


def test_budget_exhaustion_preserves_reserved_synthesis():
    model=Fake(call('list_snapshot_files'),call('list_snapshot_files',ident='second'),
        AIMessage(content='{"findings":[]}'))
    output=run_fake(model)
    # Input hold now yields before the third inspection, preserving synthesis.
    assert output.budget_usage.model_attempts==3 and output.budget_usage.tool_attempts==2
    assert output.budget_usage.output_reserved==3000
    assert output.budget_usage.input_reserved<=80000
    assert model.bindings[-1]==()
    assert output.report.specialists[0].synthesis=='succeeded' and output.reason=='partial'
    assert output.evaluation.controls[0].status=='not_evaluated'


@pytest.mark.parametrize('target',['source/resources.yaml','dataset.json'])
def test_tampered_fixture_rejected_before_credentials(tmp_path,monkeypatch,target):
    import shutil
    shutil.copytree(live.FIXTURE,tmp_path/'fixture')
    path=tmp_path/'fixture'/target
    path.write_text(path.read_text().replace('northstar-crate','northstar-altered'))
    monkeypatch.setattr(live,'FIXTURE',tmp_path/'fixture')
    def forbidden(*a,**k):raise AssertionError('credentials must not load')
    monkeypatch.setattr(live,'load_nebius_settings',forbidden)
    output=asyncio.run(live.run_slice(allow_live=True))
    assert output.reason=='fixture_rejected' and not output.started


@pytest.mark.parametrize('environment',[{}, {**ENV,'NEBIUS_MODEL':'unapproved'}])
def test_invalid_configuration_stops_before_provider(environment):
    def forbidden(*a,**k):raise AssertionError('provider must not start')
    output=asyncio.run(live.run_slice(allow_live=True,environ=environment,provider_factory=forbidden))
    assert output.reason=='configuration_error' and output.budget_usage.model_attempts==0


@pytest.mark.parametrize('status',[429,503])
def test_provider_failure_is_not_retried_or_leaked(status):
    requests=[]
    def respond(request):
        requests.append(True)
        return httpx.Response(status,json={'error':{'message':'PRIVATE synthetic-test-secret'}})
    def factory(settings,timeout):
        return live.NebiusModel(settings,allow_live=True,timeout_seconds=timeout,transport=httpx.MockTransport(respond))
    output=asyncio.run(live.run_slice(allow_live=True,environ=ENV,provider_factory=factory,
        limits=live.SliceLimits(max_model_requests=1)))
    assert len(requests)==output.budget_usage.model_attempts==1
    assert not output.report.findings and output.cleanup=='completed'
    assert 'PRIVATE' not in live.render_slice(output) and ENV['NEBIUS_API_KEY'] not in live.render_slice(output)


def test_input_reservation_limit_prevents_model_call():
    model=Fake()
    output=run_fake(model,limits=live.SliceLimits(max_input_tokens=1))
    assert output.budget_usage.model_attempts==0 and not model.histories
    assert output.reason=='partial'


def test_output_reservation_limit_clips_request_and_prevents_followup():
    model=Fake(call('list_snapshot_files'))
    output=run_fake(model,limits=live.SliceLimits(max_output_tokens=1))
    assert output.budget_usage.output_reserved==1
    assert output.budget_usage.model_attempts==len(model.histories)==1
    assert output.reason=='partial'


def test_tool_limit_preserves_synthesis_and_partial_coverage():
    output=run_fake(Fake(call('list_snapshot_files'),AIMessage(content='{"findings":[]}')),
        limits=live.SliceLimits(max_tool_attempts=1,max_model_requests=2))
    assert output.budget_usage.tool_attempts==1 and output.budget_usage.model_attempts==2
    assert output.reason=='partial' and output.evaluation.controls[0].status=='not_evaluated'
