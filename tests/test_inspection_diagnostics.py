"""Only closed categories and counts cross the public diagnostic boundary."""
import asyncio
import json
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from northstar_codereview import live_evaluation as live
from northstar_codereview.models import InspectionSummary
from .test_read_recovery import snapshot, run, ARGS
from .test_live_evaluation import ENV, FakeProvider
from northstar_codereview.evaluation_data import load_dataset, load_scenario


def test_recovered_and_unresolved_categories_remain_distinct(snapshot):
    recovered,_=run(snapshot)
    unresolved,_=run(snapshot,{**ARGS,'start_line':50})
    assert recovered.result.inspection.model_dump(mode='json')['failures']==[dict(category='invalid_line_range',recovered=1,unresolved=0)]
    assert unresolved.result.inspection.model_dump(mode='json')['failures']==[dict(category='invalid_line_range',recovered=0,unresolved=1)]
    assert recovered.usage.tool_attempts==2 and unresolved.usage.tool_attempts==1


def test_failed_correction_counts_both_physical_errors(snapshot):
    from northstar_codereview.local_source import CollectedSnapshot
    from hashlib import sha256
    text='x'*15000+'\n'+'y'*15000+'\n'
    entry=snapshot.manifest.files[0].model_copy(update={'sha256':sha256(text.encode()).hexdigest()})
    changed=CollectedSnapshot(snapshot.manifest.model_copy(update={'files':(entry,)}),{(entry.side,entry.file_path):text})
    result,_=run(changed)
    assert result.result.inspection.model_dump(mode='json')['failures']==[
        dict(category='invalid_line_range',recovered=0,unresolved=1),
        dict(category='output_size_limit',recovered=0,unresolved=1)]
    assert result.usage.tool_attempts==2


@pytest.mark.parametrize('bad',[
    [dict(category='raw-secret-content',recovered=0,unresolved=1)],
    [dict(category='invalid_arguments',recovered=0,unresolved=2)],
    [dict(category='invalid_arguments',recovered=0,unresolved=1,arguments={'secret':'x'})],
    [dict(category='invalid_arguments',recovered=0,unresolved=1)]*2])
def test_summary_rejects_unknown_categories_values_and_inconsistent_counts(bad):
    with pytest.raises(ValueError):
        InspectionSummary(unresolved_errors=1,failures=bad)


def test_parallel_failure_isolation_and_versioned_public_retention():
    snapshot=load_scenario(next(s for s in load_dataset().scenarios if s.id=='java_backend'))
    models=[]
    def factory(*args):
        model=FakeProvider(snapshot,mode='recovery' if not models else 'complete')
        models.append(model)
        return model
    result=asyncio.run(live.run_live_evaluation(allow_live=True,profile='java',environ=ENV,provider_factory=factory))
    assert [s.status for s in result.specialists]==['partial','completed','completed']
    counts=[dict(category='invalid_line_range',recovered=1,unresolved=0)]
    assert result.specialists[0].inspection.model_dump(mode='json')['failures']==counts
    assert result.evaluation.workflow.model_dump(mode='json')['inspection_failures']==counts
    payload=json.loads(live.render_live_json(result))
    assert payload['schema_version']==3 and payload['report']['schema_version']==4
    assert payload['evaluation']['schema_version']==2
    assert 'invalid_line_range' in live.render_live_summary(result)
    assert all(key not in json.dumps(payload['specialists'][0]['inspection']) for key in ['arguments','snippet','file_path','synthetic-test-key'])
    assert result.budget_usage.model_attempts==6 and result.budget_usage.tool_attempts==10

    # Missing categories in one selected branch must not become an empty success.
    from northstar_codereview.evaluation import _workflow
    scenario=next(s for s in load_dataset().scenarios if s.id=='java_backend')
    summaries=tuple(s.model_copy(update={'inspection':s.inspection.model_copy(update={'failures':None})})
                    if s.selected and s.agent_name=='security' else s for s in result.report.specialists)
    metrics=_workflow(result.report.model_copy(update={'specialists':summaries}),scenario,None,None,0)
    assert metrics.inspection_failures is None and metrics.recovered_failures==1


def test_unknown_legacy_telemetry_is_not_invented():
    assert InspectionSummary(unresolved_errors=1).model_dump()['failures'] is None
