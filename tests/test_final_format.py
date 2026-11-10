"""Format observations never repair output or contact a provider."""
import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from northstar_codereview import final_output
from northstar_codereview.agents.code_quality import CodeQualityAgent, RunLimits
from northstar_codereview.providers.nebius import NebiusModel
from .test_code_quality import snapshot
from .test_nebius import settings, completion
from .test_synthesis_gate import BoundModel


@pytest.mark.parametrize('content,expected',[
    ('{"findings":[]}',None), ('', 'empty_response'), ('  ', 'empty_response'),
    ('```json\n{"findings":[]}\n```','markdown_fenced_json'),
    ('```\n{"findings":[]}\n```','markdown_fenced_json'),
    ('Here is the result: {"findings":[]}','non_json_leading_text'),
    ('synthetic-test-secret PRIVATE_RESPONSE','non_json_leading_text'),
    ('{"findings":','invalid_json_syntax'), ('{"findings":[]} trailing','invalid_json_syntax'),
    ('{}','schema_mismatch'), ('[]','schema_mismatch'),
    ('{"findings":false}','schema_mismatch'), ([], 'unknown_format_failure'),
])
def test_closed_format_labels_preserve_acceptance(content,expected):
    message=AIMessage(content=content)
    before=final_output.parse_final_message(message)
    assert final_output.final_format_diagnostic(message)==expected
    assert final_output.parse_final_message(message)==before
    if expected:
        assert before[0] is None
        assert 'PRIVATE' not in expected and 'secret' not in expected


@pytest.mark.parametrize('finish', ['stop','length',None])
def test_installed_adapter_preserves_content_finish_usage_without_format_mode(finish):
    content='```json\n{"findings":[]}\n```'
    requests=[]
    def respond(request):
        payload=json.loads(request.content)
        requests.append(True)
        assert not any(k in payload for k in ('tools','tool_choice','response_format'))
        assert payload.get('max_completion_tokens',payload.get('max_tokens'))==1000
        body=completion({'content':content})
        body['choices'][0]['finish_reason']=finish
        return httpx.Response(200,json=body)
    async def exercise():
        async with NebiusModel(settings(),allow_live=True,transport=httpx.MockTransport(respond)) as model:
            response=await model.bind_tools(()).ainvoke([HumanMessage(content='Fictional test')],max_tokens=1000)
            assert response.content==content
            assert response.response_metadata.get('finish_reason')==finish
            assert {k:response.usage_metadata[k] for k in ('input_tokens','output_tokens','total_tokens')}=={
                'input_tokens':100,'output_tokens':20,'total_tokens':120}
            expected='truncated_response' if finish=='length' else 'markdown_fenced_json'
            assert final_output.final_format_diagnostic(response)==expected
    asyncio.run(exercise())
    assert len(requests)==1


def test_safe_format_label_in_synthesis_no_repair(snapshot):
    model=BoundModel(AIMessage(content='```json\n{"PRIVATE":"synthetic-test-secret"}\n```'))
    result=asyncio.run(CodeQualityAgent(snapshot,model=model,approved_synthetic=True,
        limits=RunLimits(max_model_calls=1)).run(review_id=uuid4()))
    assert 'Final-format diagnostic: markdown_fenced_json.' in result.result.limitations
    assert result.result.synthesis=='malformed_output'
    assert model.bindings==[()] and result.usage.model_calls==1
    assert result.usage.repairs==result.usage.retries==result.usage.tool_attempts==0
    assert not result.validated_findings
    assert 'PRIVATE' not in result.result.model_dump_json()
    assert 'synthetic-test-secret' not in result.result.model_dump_json()
