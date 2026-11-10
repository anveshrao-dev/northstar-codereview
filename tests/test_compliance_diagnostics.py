"""Value-free telemetry through real graph, authorization and persistence paths."""
import json
from hashlib import sha256

import pytest
from langchain_core.messages import AIMessage

from .test_real_source import local_intake, approval_for
from .test_real_review import service, captured, execute, ENV
from .test_real_review_failure import ScriptedReview, DISCOVERY, METADATA, READS, SOURCE, TEST, assert_attempts


@pytest.mark.parametrize('sequence,categories,files,lines', [
    ((DISCOVERY, METADATA, METADATA), ('discovery', 'metadata', 'metadata'), 0, 0),
    (READS, ('source_read', 'source_read', 'metadata'), 2, 2),
    (tuple(('get_evidence', args) if name == 'read_source_lines' else (name, args)
           for name, args in READS), ('evidence', 'evidence', 'metadata'), 2, 2),
])
def test_persisted_diagnostics_distinguish_discovery_from_observed_source(service, captured, sequence, categories, files, lines):
    result = execute(service, captured, ScriptedReview(sequence, '{"findings":[]}'))
    specialist = assert_attempts(result)
    d = specialist.diagnostics
    assert d.tool_categories == categories
    assert d.source_read_occurred is bool(files)
    assert d.files_with_observed_source == d.fully_inspected_files == files
    assert d.observed_lines == lines and d.total_lines == 2 and d.total_files == 2
    assert d.final_response.known_top_level_keys == ('findings',)
    assert d.final_response.findings_present is True
    assert d.final_response.unexpected_keys_present is False
    assert d.final_response.failure_stage == 'none'
    assert result.status == ('completed' if files else 'partial')
    assert service.load_review(captured.approval_id, result.run_id) == result
    assert 'value = 1' not in result.model_dump_json()
    assert ENV['NEBIUS_API_KEY'] not in result.model_dump_json()


@pytest.mark.parametrize('content,keys,present,unknown,stage', [
    ('{}', (), False, False, 'final_envelope'),
    ('{"findings":[]}', ('findings',), True, False, 'none'),
    ('{"findings":[],"outcome":"insufficient_evidence"}', ('findings','outcome'), True, False, 'none'),
    ('{"PRIVATE_RESPONSE_KEY":{"findings":[]}}', (), False, True, 'final_envelope'),
    ('{"findings":[],"PRIVATE_RESPONSE_KEY":"PRIVATE_RESPONSE_VALUE"}', ('findings',), True, True, 'final_envelope'),
    ('{"findings":null}', ('findings',), True, False, 'final_envelope'),
    ('[]', None, None, None, 'final_envelope'),
    ('not json PRIVATE_RESPONSE_VALUE', None, None, None, 'final_envelope'),
])
def test_response_shape_never_repairs_or_leaks_keys_or_values(service, captured, content, keys, present, unknown, stage):
    result = execute(service, captured, ScriptedReview(READS, content))
    specialist = assert_attempts(result)
    d = specialist.diagnostics.final_response
    assert d.known_top_level_keys == keys and d.findings_present is present
    assert d.unexpected_keys_present is unknown and d.failure_stage == stage
    assert not result.report.findings
    assert 'PRIVATE_RESPONSE' not in result.model_dump_json()
    if stage != 'none':
        assert result.status == 'partial' and specialist.synthesis == 'malformed_output'


@pytest.mark.parametrize('finish,want,truncation', [
    ('stop','stop',False), ('length','length',True), ('tool_calls','tool_calls',False),
    ('content_filter','content_filter',False), ('function_call','function_call',False),
    (None,'unavailable',None), ('PRIVATE_FINISH_VALUE','unrecognized',None),
    ({'PRIVATE_FINISH_KEY':'PRIVATE_FINISH_VALUE'},'unrecognized',None),
])
def test_finish_reason_is_allowlisted_not_inferred(service, captured, finish, want, truncation):
    result = execute(service, captured, ScriptedReview(READS, '{"findings":[]}', finish))
    specialist = assert_attempts(result)
    d = specialist.diagnostics.final_response
    assert d.finish_reason == want and d.truncated is truncation
    assert 'PRIVATE_FINISH' not in result.model_dump_json()
    if finish == 'length':
        assert specialist.synthesis == 'malformed_output'
        assert 'Final-output diagnostic: truncated_response.' in specialist.limitations


def test_overlapping_and_gapped_reads_count_unique_lines_without_completing_coverage(service, local_intake):
    from northstar_codereview import real_source as intake
    root, _ = local_intake
    text = 'one = 1\ntwo = 2\nthree = 3\nfour = 4\n'
    for path in (SOURCE, TEST): (root/path).write_text(text)
    receipt = approval_for(text=text)
    intake.import_sample(receipt.manifest.sample, approval=receipt)
    sequence = tuple(('read_source_lines', dict(file_path=SOURCE, start_line=a, end_line=b))
                     for a,b in ((1,2),(2,2),(4,4)))
    result = execute(service, receipt, ScriptedReview(sequence, '{"findings":[]}'))
    d = result.report.specialists[0].diagnostics
    assert d.files_with_observed_source == 1 and d.fully_inspected_files == 0
    assert d.observed_lines == 3 and d.total_lines == 8
    assert result.status == 'partial'


def test_legacy_report_without_diagnostics_preserves_content_identity(service, captured):
    from northstar_codereview.reporting import ReviewReport
    result = execute(service, captured, ScriptedReview(READS, '{"findings":[]}'))
    legacy = result.report.model_dump(mode='json', exclude={'report_id'})
    for specialist in legacy['specialists']: specialist.pop('diagnostics', None)
    digest = sha256(json.dumps(legacy, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    legacy['report_id'] = digest
    restored = ReviewReport.model_validate(legacy)
    assert restored.specialists[0].diagnostics is None
    assert restored.model_dump(mode='json') == legacy


def test_synthesis_tool_call_is_diagnosed_without_execution(service, captured):
    class ToolAtSynthesis(ScriptedReview):
        async def ainvoke(self, messages, **kwargs):
            response = await super().ainvoke(messages, **kwargs)
            if kwargs.get('synthesis_json'):
                return AIMessage(content='', response_metadata={'finish_reason':'tool_calls'},
                    tool_calls=[dict(name='read_source_lines',args={'PRIVATE_ARGUMENT':'PRIVATE_VALUE'},id='unused')])
            return response
    result = execute(service, captured, ToolAtSynthesis(READS))
    specialist = assert_attempts(result)
    assert specialist.synthesis == 'tool_call_rejected'
    assert specialist.diagnostics.final_response.failure_stage == 'final_envelope'
    assert specialist.diagnostics.final_response.finish_reason == 'tool_calls'
    assert 'PRIVATE_' not in result.model_dump_json()


def test_unavailable_synthesis_metadata_does_not_invent_a_finish_reason(service, captured):
    class FailedSynthesis(ScriptedReview):
        async def ainvoke(self, messages, **kwargs):
            if kwargs.get('synthesis_json'):
                raise RuntimeError('PRIVATE_PROVIDER_VALUE')
            return await super().ainvoke(messages, **kwargs)
    result = execute(service, captured, FailedSynthesis(READS))
    specialist = assert_attempts(result)
    assert specialist.synthesis == 'request_failed'
    assert specialist.diagnostics.final_response is None
    assert 'PRIVATE_PROVIDER' not in result.model_dump_json()


def test_failed_synthesis_does_not_reuse_an_earlier_parse_diagnostic(tmp_path):
    from northstar_codereview.local_source import collect_local_snapshot
    from northstar_codereview.models import ReviewRequest
    from .test_code_quality import ScriptedModel, call, run
    (tmp_path/'sample.py').write_text('value = 1\n')
    snapshot = collect_local_snapshot(ReviewRequest(mode='local', local_path='.'), source_root=tmp_path)
    model = ScriptedModel(AIMessage(content='{}', response_metadata={'finish_reason':'stop'}),
        call(args={'file_path':'sample.py','start_line':1,'end_line':1}), RuntimeError('PRIVATE_PROVIDER'))
    result = run(snapshot, model, max_model_calls=3)
    assert result.result.synthesis == 'request_failed'
    assert result.result.diagnostics.final_response is None
    assert result.usage.model_calls == 3 and result.usage.repairs == 1  # existing synthetic profile
