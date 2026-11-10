"""Explicit profile selection must not enlarge the default policy."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from northstar_codereview import live_evaluation as live
from northstar_codereview.execution_budget import ExecutionBudget, GlobalLimits, JavaGlobalLimits, BudgetDenied
from northstar_codereview.evaluation_data import load_dataset, load_scenario
from northstar_codereview.evaluation_runner import ScriptedModel
from .test_live_evaluation import ENV, FakeProvider, NoEnvironmentReads


def test_default_unchanged_and_explicit_java_ceiling():
    assert GlobalLimits().model_dump() == dict(max_model_requests=6, max_tool_attempts=12,
        max_input_tokens=120000, max_output_tokens=6000, output_tokens_per_call=1000,
        request_timeout_seconds=20, evaluation_timeout_seconds=60)
    assert JavaGlobalLimits().model_dump() == {**GlobalLimits().model_dump(),
        'max_model_requests': 9, 'max_tool_attempts': 15, 'max_input_tokens': 180000, 'max_output_tokens': 9000}
    with pytest.raises(ValidationError):
        ExecutionBudget(JavaGlobalLimits())


@pytest.mark.parametrize('scenario', ['angular_frontend', 'jenkins_aks', None])
def test_java_ledger_rejects_wrong_scenario(scenario):
    with pytest.raises(ValueError):
        ExecutionBudget(JavaGlobalLimits(), profile='java', scenario_id=scenario)


@pytest.mark.parametrize('field,value', [('max_model_requests',10), ('max_tool_attempts',16),
    ('max_input_tokens',180001), ('max_output_tokens',9001), ('output_tokens_per_call',1001),
    ('request_timeout_seconds',21), ('evaluation_timeout_seconds',61)])
def test_profile_ceiling_cannot_be_increased(field,value):
    with pytest.raises(ValidationError):
        JavaGlobalLimits(**{field:value})
    with pytest.raises(ValidationError):
        ExecutionBudget(JavaGlobalLimits().model_copy(update={field:value}), profile='java', scenario_id='java_backend')


@pytest.mark.parametrize('kind,cap', [('model',9),('tool',15)])
def test_concurrent_java_admission_enforces_exact_cap(kind,cap):
    ledger=ExecutionBudget(JavaGlobalLimits(),profile='java',scenario_id='java_backend')
    def attempt(_):
        try:
            if kind=='model': ledger.reserve_model(1,1000)
            else: ledger.reserve_tool()
            return True
        except BudgetDenied:return False
    with ThreadPoolExecutor(max_workers=12) as pool:
        assert sum(pool.map(attempt,range(50)))==cap
    assert getattr(ledger.usage(),'model_attempts' if kind=='model' else 'tool_attempts')==cap


@pytest.mark.parametrize('field,amount,reason', [('max_input_tokens',180000,'input_reservation'),
                                               ('max_output_tokens',1000,'output_reservation')])
def test_java_reservations_are_not_refunded(field,amount,reason):
    limits=JavaGlobalLimits(**{field:amount})
    ledger=ExecutionBudget(limits,profile='java',scenario_id='java_backend')
    ledger.reserve_model(amount if field=='max_input_tokens' else 1,1000)
    with pytest.raises(BudgetDenied):ledger.reserve_model(1,1)
    assert ledger.reason==reason and ledger.usage().unknown_usage_attempts==1


@pytest.mark.parametrize('scenario',['angular_frontend','jenkins_aks'])
def test_runner_rejects_wrong_scenario_before_credentials(scenario,monkeypatch):
    monkeypatch.setattr(live,'load_dataset',lambda:pytest.fail('Wrong-profile fixture access'))
    result=asyncio.run(live.run_live_evaluation(profile='java',scenario_id=scenario,allow_live=True,environ=NoEnvironmentReads()))
    assert result.reason=='configuration_error' and not result.started


def test_api_requires_explicit_profile_to_accept_larger_limits():
    result=asyncio.run(live.run_live_evaluation(allow_live=True,limits=JavaGlobalLimits(),environ=NoEnvironmentReads()))
    assert result.reason=='configuration_error' and result.budget_usage.model_attempts==0


@pytest.mark.parametrize('recovery',[False,True])
def test_real_parallel_graph_uses_nine_requests_and_counts_recovery(recovery):
    snapshot=load_scenario(load_dataset().scenarios[0])
    models=[]
    class Provider(ScriptedModel):
        async def __aexit__(self,*args):self.closed=True
    def factory(*args):
        model=Provider(snapshot,(),mode='recovery' if recovery else 'baseline')
        models.append(model)
        return model
    result=asyncio.run(live.run_live_evaluation(allow_live=True,profile='java',environ=ENV,provider_factory=factory))
    assert sum(m.calls for m in models)==result.budget_usage.model_attempts==9
    assert result.budget_usage.tool_attempts==(15 if recovery else 12)
    assert result.reason==('partial' if recovery else 'completed')
    assert result.evaluation.workflow.recovered_failures==(3 if recovery else 0)
    assert all(m.closed for m in models)
    assert result.configuration['specialist_limits']['max_provider_retries']==0
    assert result.configuration['specialist_limits']['max_output_repairs']==0
    assert json.loads(live.render_live_json(result))['profile']=='java'
    assert 'java' in live.render_live_summary(result)
    if recovery:assert all(c.status!='passed' for c in result.evaluation.controls)


@pytest.mark.parametrize('mode',['error','deadline'])
def test_java_failure_and_cancellation_retain_unknown_usage(mode):
    snapshot=load_scenario(load_dataset().scenarios[0]);models=[]
    def factory(*args):
        model=FakeProvider(snapshot,mode='error' if mode=='error' else 'complete',delay=1 if mode=='deadline' else 0)
        models.append(model);return model
    limits=JavaGlobalLimits(evaluation_timeout_seconds=.15 if mode=='deadline' else 60)
    result=asyncio.run(live.run_live_evaluation(allow_live=True,profile='java',limits=limits,environ=ENV,provider_factory=factory))
    assert result.reason==('partial' if mode=='error' else 'deadline')
    assert result.budget_usage.unknown_usage_attempts==result.budget_usage.model_attempts
    assert sum(m.calls for m in models)<=3 and all(m.closed for m in models)
    assert all(c.status!='passed' for c in result.evaluation.controls)


def test_cli_selects_profile_defaults_and_only_allows_reductions(monkeypatch,capsys):
    seen=[]
    original=live.run_live_evaluation
    async def disabled(**kwargs):
        assert not kwargs['allow_live']
        seen.append(kwargs)
        return await original(**kwargs,environ=NoEnvironmentReads())
    monkeypatch.setattr(live,'run_live_evaluation',disabled)
    live.main(['--budget-profile','java','--scenario','java_backend'])
    output=json.loads(capsys.readouterr().out)
    assert output['reason']=='disabled' and output['profile']=='java'
    assert seen[-1]['limits'].max_model_requests==9
    live.main(['--budget-profile','java','--max-model-requests','8'])
    capsys.readouterr()
    assert seen[-1]['limits'].max_model_requests==8
    for args in (['--max-model-requests','9'],['--budget-profile','java','--max-model-requests','10'],
                 ['--budget-profile','java','--scenario','angular_frontend'],['--budget-profile','unknown']):
        with pytest.raises(SystemExit) as error:live.main(args)
        assert error.value.code==2
        capsys.readouterr()


@pytest.mark.parametrize('change',[{'profile':'default'},{'scenario_id':'angular_frontend'}])
def test_report_cannot_relabel_java_profile_as_default_or_other_scenario(change):
    result=asyncio.run(live.run_live_evaluation(profile='java',environ=NoEnvironmentReads()))
    with pytest.raises(live.EvaluationError,match='unsafe_output'):
        live.render_live_json(result.model_copy(update=change))


def test_lowered_java_profile_stays_explicit_in_configuration_identity():
    from northstar_codereview.evaluation import fingerprint
    defaults=GlobalLimits()
    java=JavaGlobalLimits(**defaults.model_dump())
    assert fingerprint(live._configuration(defaults))!=fingerprint(live._configuration(java,'java'))
