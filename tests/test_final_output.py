"""Final-envelope failures expose categories only; source validation stays separate."""
import asyncio
import json
from importlib import import_module
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from northstar_codereview.agents.code_quality import CodeQualityAgent,RunLimits
from .test_code_quality import snapshot,call,READ,proposal
from .test_synthesis_gate import BoundModel


def parse(message):
    return import_module('northstar_codereview.final_output').parse_final_message(message)


@pytest.mark.parametrize('content,category',[
 ('','empty_response'),(' \n\t','empty_response'),('{','invalid_json'),
 ('```json\n{"findings":[]}\n```','invalid_json'),
 ('[]','top_level_type_mismatch'),('null','top_level_type_mismatch'),('true','top_level_type_mismatch'),
 ('{}','missing_required_fields'),('{"outcome":"insufficient_evidence"}','missing_required_fields'),
 ('{"findings":null}','invalid_field_types'),('{"findings":{}}','invalid_field_types'),
 ('{"findings":[1]}','invalid_field_types'),('{"findings":[],"outcome":false}','invalid_field_types'),
 ('{"findings":[],"PRIVATE_KEY":"PRIVATE_VALUE"}','unknown_validation_failure'),
 ('{"findings":[{}],"outcome":"insufficient_evidence"}','unknown_validation_failure'),
 ([], 'invalid_field_types')])
def test_closed_categories_without_values(content,category):
    envelope,diagnostic=parse(AIMessage(content=content))
    assert envelope is None and diagnostic==category
    assert 'PRIVATE' not in str(diagnostic)


@pytest.mark.parametrize('content',['{"findings":[]}','{"findings":[],"outcome":"findings"}',
                                  '{"findings":[],"outcome":"insufficient_evidence"}'])
def test_valid_envelopes(content):
    envelope,diagnostic=parse(AIMessage(content=content))
    assert diagnostic is None and envelope.findings==[]


@pytest.mark.parametrize('content',['{"findings":[]}','{',''])
def test_explicit_finish_length_takes_precedence(content):
    assert parse(AIMessage(content=content,response_metadata={'finish_reason':'length'}))==(None,'truncated_response')


def test_token_counts_and_unknown_metadata_are_not_truncation_evidence():
    envelope,diagnostic=parse(AIMessage(content='{"findings":[]}',response_metadata={'finish_reason':'PRIVATE_UNKNOWN'},
        usage_metadata={'input_tokens':1,'output_tokens':1000,'total_tokens':1001}))
    assert envelope is not None and diagnostic is None


def test_unexpected_tool_call_category_without_arguments():
    message=call(args={'PRIVATE_KEY':'PRIVATE_VALUE'})
    assert parse(message)==(None,'unexpected_tool_call')


def test_prompt_schema_has_outcome_and_resolvable_root_definitions(snapshot):
    model=BoundModel(AIMessage(content='{"findings":[]}'))
    asyncio.run(CodeQualityAgent(snapshot,model=model,approved_synthetic=True).run(review_id=uuid4()))
    schema=json.loads(model.histories[0][0].content.split('Final response JSON schema:\n')[1])
    assert schema['properties']['outcome']['enum']==['findings','insufficient_evidence']
    assert schema['required']==['findings'] and schema['additionalProperties'] is False
    def visit(node):
        if isinstance(node,dict):
            if '$ref' in node:
                ref=node['$ref'];assert ref.startswith('#/$defs/')
                assert ref.split('/')[-1] in schema['$defs']
            for value in node.values():visit(value)
        elif isinstance(node,list):
            for value in node:visit(value)
    visit(schema)


@pytest.mark.parametrize('forged',[False,True])
def test_valid_findings_still_require_exact_observed_evidence(snapshot,forged):
    def final(messages):
        item=proposal(messages)
        if forged:item['evidence'][0]['snippet']='FABRICATED'
        return AIMessage(content=json.dumps({'findings':[item]}))
    model=BoundModel(call(args=READ),final)
    result=asyncio.run(CodeQualityAgent(snapshot,model=model,approved_synthetic=True,
        limits=RunLimits(max_model_calls=2)).run(review_id=uuid4()))
    assert len(result.validated_findings)==(0 if forged else 1)
    assert result.result.synthesis=='succeeded'


def test_safe_category_survives_specialist_summary_without_raw_output(snapshot):
    model=BoundModel(call(args=READ),AIMessage(content='PRIVATE_PROVIDER_VALUE'))
    result=asyncio.run(CodeQualityAgent(snapshot,model=model,approved_synthetic=True,
        limits=RunLimits(max_model_calls=2)).run(review_id=uuid4()))
    text=result.result.model_dump_json()
    assert 'Final-output diagnostic: invalid_json.' in text
    assert 'PRIVATE_PROVIDER_VALUE' not in text
    assert result.usage.model_calls==2 and result.usage.retries==result.usage.repairs==0


def test_installed_adapter_preserves_length_indicator_without_network(snapshot):
    import httpx
    from northstar_codereview.providers.nebius import NebiusModel
    from .test_nebius import settings,completion
    requests=[]
    async def respond(request):
        requests.append(True)
        if len(requests)==1:
            body=completion({'content':None,'tool_calls':[{'id':'read','type':'function',
                'function':{'name':'read_source_lines','arguments':json.dumps(READ)}}]})
        else:
            body=completion({'content':'{"findings":[]}'})
            body['choices'][0]['finish_reason']='length'
        return httpx.Response(200,json=body)
    async def scenario():
        async with NebiusModel(settings(),allow_live=True,transport=httpx.MockTransport(respond)) as model:
            result=await CodeQualityAgent(snapshot,model=model,approved_synthetic=True,
                limits=RunLimits(max_model_calls=2)).run(review_id=uuid4())
            assert result.result.synthesis=='malformed_output'
            assert 'Final-output diagnostic: truncated_response.' in result.result.limitations
            assert result.usage.model_calls==2 and result.usage.repairs==result.usage.retries==0
            assert not result.validated_findings
    asyncio.run(scenario())
    assert len(requests)==2


@pytest.mark.parametrize('content,expected',[
 ('{"findings":[]}','succeeded'),
 ('{"findings":[],"outcome":"insufficient_evidence"}','insufficient_evidence'),
 ('{}','malformed_output'),('[]','malformed_output'),('','malformed_output')])
def test_synthesis_integration_outcomes(snapshot,content,expected):
    model=BoundModel(call('list_snapshot_files'),AIMessage(content=content))
    result=asyncio.run(CodeQualityAgent(snapshot,model=model,approved_synthetic=True,
        limits=RunLimits(max_model_calls=2)).run(review_id=uuid4()))
    assert result.result.synthesis==expected and result.result.status=='partial'
    assert not result.validated_findings and not result.result.reviewed_files
    assert result.usage.model_calls==2 and result.usage.repairs==result.usage.retries==0
