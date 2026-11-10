"""Reproduce the retained failure shape, not an invented raw live response.

Only generated source, scripted responses and in-memory HTTP are used. The real
receipt, claim, LangGraph, inspection, parsing and reporting paths remain active.
"""
import asyncio
import json
from itertools import product

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage

from .test_real_source import local_intake, approval_for
from .test_real_review import service, captured, Fake, ENV, execute, permit
from .test_nebius import completion
from northstar_codereview import real_source as intake
from northstar_codereview.providers.nebius import NebiusModel


SOURCE = 'src/stock_portfolio_analyzer/risk.py'
TEST = 'tests/test_risk.py'
DISCOVERY = ('list_snapshot_files', {})
METADATA = ('get_source_metadata', {'file_path': SOURCE})
READS = (('read_source_lines', {'file_path': SOURCE, 'start_line': 1, 'end_line': 1}),
         ('read_source_lines', {'file_path': TEST, 'start_line': 1, 'end_line': 1}), METADATA)


def assert_contract(messages):
    schema = json.loads(messages[0].content.split('Final response JSON schema:\n')[1])
    assert schema['type'] == 'object' and schema['required'] == ['findings']
    assert schema['additionalProperties'] is False
    assert schema['properties']['findings']['type'] == 'array'
    assert set(schema['$defs']['FindingProposal']['required']) == {
        'title', 'category', 'severity', 'confidence', 'explanation', 'suggested_remediation', 'evidence'}
    replies = [m for m in messages if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in replies] == ['inspect-0', 'inspect-1', 'inspect-2']
    assert len([m for m in messages if isinstance(m, AIMessage) and m.tool_calls]) == 3


class ScriptedReview(Fake):
    def __init__(self, sequence, final='{}', finish='stop'):
        super().__init__()
        self.sequence, self.final, self.finish = sequence, final, finish

    async def ainvoke(self, messages, **kwargs):
        index = len(self.histories)
        self.histories.append(list(messages))
        assert index < 4, 'unexpected retry or additional model call'
        assert kwargs['max_tokens'] == 1000
        if index < 3:
            assert len(self.tools) == 4 and not kwargs.get('synthesis_json')
            name, args = self.sequence[index]
            return AIMessage(content='', tool_calls=[dict(name=name, args=args, id=f'inspect-{index}')])
        assert not self.tools and kwargs['synthesis_json'] is True
        assert_contract(messages)
        content = self.final(messages) if callable(self.final) else self.final
        return AIMessage(content=content, response_metadata={'finish_reason': self.finish})


def assert_attempts(result):
    assert result.usage.model_attempts == 4 and result.usage.tool_attempts == 3
    specialist = result.report.specialists[0]
    assert specialist.usage.retries == specialist.usage.repairs == 0
    assert specialist.inspection.successful_attempts == 3
    assert specialist.inspection.recovered_errors == specialist.inspection.unresolved_errors == 0
    assert result.persisted and result.cleanup == 'closed'
    return specialist


@pytest.mark.parametrize('choices', list(product((0, 1), repeat=3)))
def test_discovery_metadata_sequences_reproduce_same_failure_shape(service, captured, choices):
    model = ScriptedReview(tuple((DISCOVERY, METADATA)[choice] for choice in choices))
    result = execute(service, captured, model)
    specialist = assert_attempts(result)
    assert result.status == 'partial' and specialist.synthesis == 'malformed_output'
    assert specialist.reviewed_files == () and not result.report.findings
    assert 'Final-format diagnostic: schema_mismatch.' in specialist.limitations
    assert 'Final-output diagnostic: missing_required_fields.' in specialist.limitations
    assert service.load_review(captured.approval_id, result.run_id) == result
    assert all(not json.loads(m.content)['evidence'] for m in model.histories[-1] if isinstance(m, ToolMessage))


@pytest.mark.parametrize('content', ['{"results":[]}', '{"review":{"findings":[]}}',
    '{"outcome":"insufficient_evidence"}', '{"PRIVATE_RESPONSE_KEY":"PRIVATE_RESPONSE_VALUE"}'])
def test_missing_top_level_findings_is_never_repaired_or_renamed(service, captured, content):
    result = execute(service, captured, ScriptedReview(READS, content))
    specialist = assert_attempts(result)
    assert specialist.reviewed_files == tuple(sorted((SOURCE, TEST)))
    assert result.status == 'partial' and specialist.synthesis == 'malformed_output'
    assert 'Final-output diagnostic: missing_required_fields.' in specialist.limitations
    assert not result.report.findings
    assert 'PRIVATE_RESPONSE' not in result.model_dump_json()
    assert ENV['NEBIUS_API_KEY'] not in result.model_dump_json()


@pytest.mark.parametrize('tool', ['read_source_lines', 'get_evidence'])
def test_actual_source_observations_survive_valid_empty_findings(service, captured, tool):
    sequence = tuple((tool, args) if name == 'read_source_lines' else (name, args) for name, args in READS)
    result = execute(service, captured, ScriptedReview(sequence, '{"findings":[]}'))
    specialist = assert_attempts(result)
    assert result.status == 'completed' and specialist.synthesis == 'succeeded'
    assert specialist.reviewed_files == tuple(sorted((SOURCE, TEST)))
    assert not result.report.findings


@pytest.mark.parametrize('content,synthesis', [('{"findings":[]}', 'succeeded'),
    ('{"findings":[],"outcome":"insufficient_evidence"}', 'insufficient_evidence')])
def test_valid_envelope_does_not_turn_discovery_into_source_inspection(service, captured, content, synthesis):
    result = execute(service, captured, ScriptedReview((DISCOVERY, METADATA, METADATA), content))
    specialist = assert_attempts(result)
    assert result.status == 'partial' and specialist.synthesis == synthesis
    assert not specialist.reviewed_files and not result.report.findings


@pytest.mark.parametrize('forged', [False, True])
def test_observed_finding_still_passes_separate_evidence_gate(service, captured, forged):
    def final(messages):
        observation = next(m for m in messages if isinstance(m, ToolMessage))
        evidence = json.loads(observation.content)['evidence']
        if forged:
            evidence[0]['snippet'] = 'fabricated quotation'
        proposal = dict(title='Fictional arithmetic claim', category='correctness', severity='low', confidence=0.5,
            explanation='A scripted claim used only to exercise evidence validation.',
            suggested_remediation='Human assessment is still required.', evidence=evidence)
        return json.dumps({'findings':[proposal]})
    result = execute(service, captured, ScriptedReview(READS, final))
    specialist = assert_attempts(result)
    assert specialist.synthesis == 'succeeded'
    assert len(result.report.findings) == (0 if forged else 1)
    assert result.status == ('partial' if forged else 'completed')
    if not forged:
        assert result.report.findings[0].claim_status == 'unverified'
    else:
        assert any('Proposal 1 rejected:' in note for note in specialist.limitations)


def test_partial_line_coverage_is_retained_but_never_completed(service, local_intake):
    root, _ = local_intake
    text = 'first = 1\nsecond = 2\n'
    for path in (SOURCE, TEST): (root/path).write_text(text)
    receipt = approval_for(text=text)
    intake.import_sample(receipt.manifest.sample, approval=receipt)
    result = execute(service, receipt, ScriptedReview(READS, '{"findings":[]}'))
    specialist = assert_attempts(result)
    assert specialist.reviewed_files == tuple(sorted((SOURCE, TEST)))
    assert result.status == 'partial' and specialist.synthesis == 'succeeded'
    assert any('source lines were not inspected' in note for note in specialist.limitations)


@pytest.mark.parametrize('finish,diagnostic', [('stop','missing_required_fields'), ('length','truncated_response')])
def test_real_adapter_preserves_contract_and_finish_metadata_offline(service, captured, finish, diagnostic):
    requests = []
    async def respond(request):
        payload = json.loads(request.content)
        index = len(requests)
        requests.append(payload)
        assert index < 4 and payload['model'] == ENV['NEBIUS_MODEL']
        if index < 3:
            assert len(payload['tools']) == 4 and 'response_format' not in payload
            name, args = (DISCOVERY, METADATA, METADATA)[index]
            return httpx.Response(200, json=completion({'content':None,'tool_calls':[
                {'id':f'inspect-{index}','type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}))
        assert payload['response_format'] == {'type':'json_object'}
        assert 'tools' not in payload and 'tool_choice' not in payload
        schema = json.loads(payload['messages'][0]['content'].split('Final response JSON schema:\n')[1])
        assert schema['required'] == ['findings'] and schema['additionalProperties'] is False
        assert [m['tool_call_id'] for m in payload['messages'] if m['role']=='tool'] == ['inspect-0','inspect-1','inspect-2']
        body = completion({'content':'{"results":[]}'})
        body['choices'][0]['finish_reason'] = finish
        return httpx.Response(200, json=body)
    approval = permit(service, captured)
    result = asyncio.run(service.run_review(captured.manifest.sample,captured.approval_id,
        transmission_id=approval.approval_id,allow_live=True,environ=ENV,prepared_context=False,
        provider_factory=lambda settings,timeout:NebiusModel(settings,allow_live=True,timeout_seconds=timeout,
                                                            transport=httpx.MockTransport(respond))))
    specialist = assert_attempts(result)
    assert result.status == 'partial' and specialist.synthesis == 'malformed_output'
    assert f'Final-output diagnostic: {diagnostic}.' in specialist.limitations
    assert len(requests) == 4
    assert result.usage.reported_input_tokens == 400 and result.usage.reported_output_tokens == 80
    assert result.usage.unknown_usage_attempts == 0
