"""Value-free proposal gates using fictional snapshots and model responses."""
import asyncio
import json
from importlib import import_module
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from northstar_codereview.agents.code_quality import CodeQualityAgent, RunLimits
from northstar_codereview.final_output import final_response_schema, parse_final_message
from .test_code_quality import snapshot, READ, call, proposal
from .test_synthesis_gate import BoundModel


@pytest.mark.parametrize('mutation,category',[
    ('missing','invalid_proposal_structure'),('type','invalid_proposal_structure'),
    ('category','wrong_specialist_category'),('unobserved','unobserved_evidence_range'),
    ('snapshot','source_identity_mismatch'),('path','unsupported_evidence'),
    ('quote','exact_source_mismatch'),('fabricated','exact_source_mismatch')])
def test_precise_rejection_without_values(snapshot,mutation,category):
    def final(messages):
        item=proposal(messages)
        if mutation=='missing':del item['title']
        elif mutation=='type':item['confidence']='PRIVATE_SECRET'
        elif mutation=='category':item['category']='security'
        elif mutation=='unobserved':item['evidence'][0]['end_line']=3
        elif mutation=='snapshot':item['evidence'][0]['snapshot_id']=str(uuid4())
        elif mutation=='path':item['evidence'][0]['file_path']='private-secret.py'
        elif mutation=='quote':item['evidence'][0]['snippet']='Incorrect quotation PRIVATE_SECRET'
        else:item['evidence'][0]['snippet']='Fabricated source PRIVATE_SECRET'
        return AIMessage(content=json.dumps({'findings':[item]}))
    read={**READ,'end_line':2} if mutation=='unobserved' else READ
    result=asyncio.run(CodeQualityAgent(snapshot,model=BoundModel(call(args=read),final),approved_synthetic=True,
        limits=RunLimits(max_model_calls=2)).run(review_id=uuid4()))
    assert f'Proposal 1 rejected: {category}.' in result.result.limitations
    assert result.result.status=='partial' and not result.validated_findings
    assert result.usage.model_calls==2 and result.usage.retries==result.usage.repairs==0
    assert 'PRIVATE_SECRET' not in result.result.model_dump_json()
    assert 'private-secret.py' not in result.result.model_dump_json()


@pytest.mark.parametrize('module,classname,category',[
    ('code_quality','CodeQualityAgent','correctness'),('security','SecurityAgent','security'),
    ('test_coverage','TestCoverageAgent','tests'),('gitops','GitOpsAgent','gitops'),
    ('cicd_groovy','CICDGroovyAgent','cicd')])
@pytest.mark.parametrize('wrong',[False,True])
def test_specialist_categories_share_diagnostics_and_acceptance(snapshot,module,classname,category,wrong):
    def final(messages):
        item=proposal(messages)
        item['category']=('security' if category!='security' else 'correctness') if wrong else category
        return AIMessage(content=json.dumps({'findings':[item]}))
    cls=getattr(import_module('northstar_codereview.agents.'+module),classname)
    result=asyncio.run(cls(snapshot,model=BoundModel(call(args=READ),final),approved_synthetic=True,
        limits=RunLimits(max_model_calls=2)).run(review_id=uuid4()))
    assert len(result.validated_findings)==(0 if wrong else 1)
    if wrong:assert 'Proposal 1 rejected: wrong_specialist_category.' in result.result.limitations


@pytest.mark.parametrize('content,diagnostic',[
    ('{"findings":[]}',None),('{"findings":[],"outcome":"insufficient_evidence"}',None),
    ('{}','missing_required_fields'),('{"findings":false}','invalid_field_types')])
def test_envelope_contract_is_already_explicit(content,diagnostic):
    assert parse_final_message(AIMessage(content=content))[1]==diagnostic
    schema=final_response_schema()
    assert schema['required']==['findings']
    assert set(schema['$defs']['FindingProposal']['required'])=={
        'title','category','severity','confidence','explanation','suggested_remediation','evidence'}
    assert set(schema['$defs']['EvidenceReference']['required'])=={
        'snapshot_id','file_path','start_line','end_line','snippet'}


def test_unknown_validator_failure_is_sanitized(snapshot,monkeypatch):
    from northstar_codereview import proposal_validation
    def fail(*a,**k):raise ValueError('PRIVATE_SECRET raw source')
    monkeypatch.setattr(proposal_validation,'validate_finding',fail)
    def final(messages):return AIMessage(content=json.dumps({'findings':[proposal(messages)]}))
    result=asyncio.run(CodeQualityAgent(snapshot,model=BoundModel(call(args=READ),final),approved_synthetic=True,
        limits=RunLimits(max_model_calls=2)).run(review_id=uuid4()))
    assert 'Proposal 1 rejected: unknown_validation_failure.' in result.result.limitations
    assert 'PRIVATE_SECRET' not in result.result.model_dump_json() and not result.validated_findings


@pytest.mark.parametrize('change,category',[
    ('digest','source_identity_mismatch'),('missing_source','unsupported_evidence'),
    ('range','unsupported_evidence'),('quote','exact_source_mismatch')])
def test_source_boundary_labels_preserve_rejection(snapshot,change,category):
    from types import SimpleNamespace
    from langchain_core.messages import ToolMessage
    from northstar_codereview.models import EvidenceReference
    from northstar_codereview.proposal_validation import validate_observed_proposal
    sources=dict(snapshot.sources)
    ref=EvidenceReference(snapshot_id=snapshot.manifest.snapshot_id,file_path='refund.py',start_line=1,
        end_line=9 if change=='range' else 3,snippet=sources['head','refund.py'] if change!='quote' else 'PRIVATE_SECRET')
    if change=='digest':sources['head','refund.py']+='PRIVATE_SECRET'
    if change=='missing_source':sources.clear()
    captured=SimpleNamespace(manifest=snapshot.manifest,sources=sources)
    data=proposal([ToolMessage(content=json.dumps({'evidence':[ref.model_dump(mode='json')]}),tool_call_id='read')])
    validated,diagnostic=validate_observed_proposal(data,captured,(ref,),categories={'correctness'},agent_name='code_quality')
    assert validated is None and diagnostic==category and 'PRIVATE' not in diagnostic


def test_invalid_sibling_does_not_erase_valid_finding(snapshot):
    def final(messages):
        return AIMessage(content=json.dumps({'findings':[{'PRIVATE_SECRET':'PRIVATE_SECRET'},proposal(messages)]}))
    result=asyncio.run(CodeQualityAgent(snapshot,model=BoundModel(call(args=READ),final),approved_synthetic=True,
        limits=RunLimits(max_model_calls=2)).run(review_id=uuid4()))
    assert len(result.validated_findings)==1 and result.result.status=='partial'
    assert 'Proposal 1 rejected: invalid_proposal_structure.' in result.result.limitations
    assert 'PRIVATE_SECRET' not in result.result.model_dump_json()


@pytest.mark.parametrize('module,classname',[
    ('code_quality','CodeQualityAgent'),('security','SecurityAgent'),('test_coverage','TestCoverageAgent'),
    ('gitops','GitOpsAgent'),('cicd_groovy','CICDGroovyAgent')])
def test_each_specialist_prompt_explicitly_requires_findings(snapshot,module,classname):
    model=BoundModel(AIMessage(content='{"findings":[],"outcome":"insufficient_evidence"}'))
    cls=getattr(import_module('northstar_codereview.agents.'+module),classname)
    result=asyncio.run(cls(snapshot,model=model,approved_synthetic=True,
        limits=RunLimits(max_model_calls=1)).run(review_id=uuid4()))
    prompt=model.histories[0][0].content
    assert 'The required findings array' in prompt
    schema=json.loads(prompt.split('Final response JSON schema:\n')[1])
    assert schema['required']==['findings']
    assert result.result.synthesis=='insufficient_evidence' and result.result.status=='partial'
