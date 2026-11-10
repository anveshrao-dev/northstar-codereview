"""Controlled live entry point tested exclusively with fake models/transports."""

import asyncio
import json
from hashlib import sha256

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from northstar_codereview import live_evaluation as live
from northstar_codereview.execution_budget import GlobalLimits
from northstar_codereview.evaluation_data import load_dataset, load_scenario


ENV = {'NEBIUS_API_KEY': 'synthetic-test-key', 'NEBIUS_MODEL': 'Qwen/Qwen3-30B-A3B-Instruct-2507'}


class NoEnvironmentReads(dict):
    def get(self, *args):
        pytest.fail('Environment read before successful opt-in/preflight')


class FakeProvider:
    def __init__(self, snapshot, *, mode='complete', delay=0):
        self.snapshot, self.mode, self.delay = snapshot, mode, delay
        self.calls = 0
        self.closed = self.cancelled = False

    def bind_tools(self, tools):
        self.tools = tools
        return self

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            text = json.dumps([m.model_dump() for m in messages])
            assert 'full_refund' not in text and 'exact_stock_test_gap' not in text
            assert 'completeness_boundary' not in text and 'predeclared_script' not in text
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.mode == 'error':
            raise RuntimeError('RAW_PRIVATE_ERROR synthetic-test-key')
        if self.calls == 1:
            return AIMessage(content='', tool_calls=[dict(name='read_source_lines', args=dict(
                file_path=f.file_path, start_line=1, end_line=len(self.snapshot.sources[f.side, f.file_path].splitlines())
                + int(self.mode == 'recovery' and i == 0)), id=f'read-{i}') for i,f in enumerate(self.snapshot.manifest.files)])
        findings = []
        if self.mode in {'finding', 'secret'}:
            observation = next(json.loads(m.content) for m in reversed(messages) if isinstance(m, ToolMessage))
            findings = [dict(title='A synthetic unjudged concern', category='correctness', severity='medium', confidence=.5,
                explanation='sk-FictionalSecretNeverReal1234567890' if self.mode == 'secret' else 'This claim requires semantic adjudication.',
                suggested_remediation='Investigate the captured behavior.', evidence=observation['evidence'])]
        return AIMessage(content=json.dumps({'findings': findings}))

    async def __aexit__(self, *args):
        self.closed = True


def execute(*, scenario='java_backend', mode='complete', limits=None, delay=0):
    snapshot = load_scenario(next(s for s in load_dataset().scenarios if s.id == scenario))
    models = []
    def factory(settings, timeout):
        model = FakeProvider(snapshot, mode=mode if not models else 'complete', delay=delay)
        models.append(model)
        return model
    output = asyncio.run(live.run_live_evaluation(scenario_id=scenario, allow_live=True, environ=ENV,
                                                 limits=limits, provider_factory=factory))
    return output, models


@pytest.mark.parametrize('flag', [False, None, 1, 'true'])
def test_disabled_does_not_read_environment_or_fixtures(flag, monkeypatch):
    monkeypatch.setattr(live, 'load_dataset', lambda: pytest.fail('disabled fixture read'))
    result = asyncio.run(live.run_live_evaluation(allow_live=flag, environ=NoEnvironmentReads()))
    assert result.reason == 'disabled' and not result.started
    assert result.budget_usage.model_attempts == 0


@pytest.mark.parametrize('scenario', ['other', '../java_backend', '.', 'all'])
def test_only_approved_single_scenarios(scenario):
    result = asyncio.run(live.run_live_evaluation(scenario_id=scenario, allow_live=True, environ=NoEnvironmentReads()))
    assert result.reason == 'configuration_error' and not result.started


@pytest.mark.parametrize('config', [{}, {**ENV, 'NEBIUS_MODEL': 'wrong'}, {**ENV, 'NEBIUS_BASE_URL': 'https://example.invalid/'}])
def test_invalid_settings_do_not_start_providers(config):
    result = asyncio.run(live.run_live_evaluation(allow_live=True, environ=config,
        provider_factory=lambda *args: pytest.fail('Invalid settings constructed provider')))
    assert result.reason == 'configuration_error'


def test_dataset_tampering_rejected_before_environment(monkeypatch):
    original = load_dataset()
    monkeypatch.setattr(live, 'load_dataset', lambda: original.model_copy(update={'id': 'changed'}))
    result = asyncio.run(live.run_live_evaluation(allow_live=True, environ=NoEnvironmentReads()))
    assert result.reason == 'fixture_rejected'


@pytest.mark.parametrize('scenario', ['java_backend', 'angular_frontend', 'jenkins_aks'])
def test_three_real_workflows_bounded_without_source_changes(scenario):
    data = load_dataset()
    before = data.model_dump_json()
    output, models = execute(scenario=scenario)
    assert output.report is not None and output.evaluation is not None
    assert output.budget_usage.model_attempts == sum(m.calls for m in models) <= 6
    assert output.budget_usage.tool_attempts <= 12
    assert all(m.closed for m in models)
    assert output.configuration['specialist_limits']['max_provider_retries'] == 0
    assert output.configuration['specialist_limits']['max_output_repairs'] == 0
    assert all(s.selected for s in output.specialists)
    assert before == load_dataset().model_dump_json()


@pytest.mark.parametrize('cap',[1,3])
def test_global_cap_retains_partial_and_never_passes_controls(cap):
    output, models = execute(limits=GlobalLimits(max_model_requests=cap))
    if cap < 3:
        assert not models and not output.started and output.reason == 'configuration_error'
        assert output.evaluation is None and output.budget_usage.model_attempts == 0
        return
    assert sum(m.calls for m in models) == 3
    assert output.reason == 'partial'
    assert all(c.status == 'not_evaluated' for c in output.evaluation.controls)


def test_source_recovery_is_counted_and_keeps_review_partial():
    output, _ = execute(mode='recovery')
    assert output.reason == 'partial'
    assert output.evaluation.workflow.recovered_failures == 1
    assert output.budget_usage.tool_attempts == 10
    assert all(c.status != 'passed' for c in output.evaluation.controls)


def test_unjudged_finding_stays_pending_even_with_valid_evidence():
    output, _ = execute(mode='finding')
    assert output.validated_findings == 1
    assert output.evaluation.quality.pending == 1 and output.evaluation.quality.true_positives == 0
    assert output.evaluation.quality.precision is None
    assert output.evaluation.controls[0].status == 'pending'


def test_provider_errors_have_no_retries_or_payload_leakage():
    output, models = execute(mode='error')
    assert models[0].calls == 1 and output.reason == 'partial'
    text = live.render_live_json(output)
    assert 'RAW_PRIVATE_ERROR' not in text and 'synthetic-test-key' not in text
    assert output.budget_usage.unknown_usage_attempts == output.budget_usage.model_attempts


def synchronized_deadline(monkeypatch, *, before_request=False):
    """Exercise real asyncio timeout cancellation after a known lifecycle event.

    Only the outer deadline is manually scheduled; graph execution, provider
    cancellation, cleanup and ledger behavior remain real. A watchdog bounds
    a broken test but is not used to decide when the deadline should fire.
    """
    async def scenario():
        entered=asyncio.Event()
        blocked=asyncio.Event()
        deadlines=[]
        class AsyncioProxy:
            def __getattr__(self,name): return getattr(asyncio,name)
            def timeout(self,delay):
                if not deadlines:
                    timer=asyncio.timeout(None)
                    deadlines.append(timer)
                    return timer
                return asyncio.timeout(delay)
        monkeypatch.setattr(live,'asyncio',AsyncioProxy())
        snapshot=load_scenario(next(s for s in load_dataset().scenarios if s.id=='java_backend'))
        models=[]
        class WaitingProvider(FakeProvider):
            async def ainvoke(self,messages,**kwargs):
                self.calls+=1
                entered.set()
                try: await blocked.wait()
                except asyncio.CancelledError:
                    self.cancelled=True
                    raise
        def factory(*args):
            model=WaitingProvider(snapshot)
            models.append(model)
            return model
        if before_request:
            async def waiting_setup(self,**kwargs):
                entered.set()
                await blocked.wait()
            monkeypatch.setattr(live.ReviewOrchestrator,'run',waiting_setup)
        task=asyncio.create_task(live.run_live_evaluation(allow_live=True,environ=ENV,provider_factory=factory))
        try:
            await asyncio.wait_for(entered.wait(),5)
            deadlines[0].reschedule(asyncio.get_running_loop().time())
            return await asyncio.wait_for(task,5),models
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
    return asyncio.run(scenario())


def test_deadline_cancels_inflight_calls_and_preserves_budget_telemetry(monkeypatch):
    output, models = synchronized_deadline(monkeypatch)
    assert output.reason == 'deadline' and output.report is None
    assert 1 <= output.budget_usage.model_attempts <= 3
    assert output.budget_usage.unknown_usage_attempts == output.budget_usage.model_attempts
    assert all(m.closed for m in models) and any(m.cancelled for m in models)
    assert output.evaluation.review_status == 'missing'
    assert output.evaluation.quality.false_negatives is None
    assert all(c.status == 'not_evaluated' for c in output.evaluation.controls)
    assert all(s.status == 'unavailable' for s in output.specialists)


def test_deadline_before_request_entry_requires_no_inflight_cancellation(monkeypatch):
    output,models=synchronized_deadline(monkeypatch,before_request=True)
    assert output.reason=='deadline' and output.report is None
    assert output.budget_usage.model_attempts==0 and not models
    assert output.cleanup=='not_needed'
    assert output.evaluation.review_status=='missing'


def test_secret_claim_withheld_and_raw_observations_never_exported():
    output, _ = execute(mode='secret')
    assert output.report.safety and output.reason == 'partial'
    for text in (live.render_live_json(output), live.render_live_summary(output)):
        assert 'sk-FictionalSecret' not in text and 'snippet' not in text
        assert 'return amount - 1' not in text and 'synthetic-test-key' not in text


def test_provider_initialization_failure_stays_failed():
    def failed(*args):
        raise RuntimeError('SECRET_FAILURE')
    output = asyncio.run(live.run_live_evaluation(allow_live=True, environ=ENV, provider_factory=failed))
    assert output.reason == 'failed' and output.budget_usage.model_attempts == 0
    assert all(s.status == 'failed' for s in output.specialists)
    assert 'SECRET_FAILURE' not in live.render_live_json(output)


@pytest.mark.parametrize('attribute,value', [('version_info', (3,11)), ('prefix', '/not-the-project-venv')])
def test_wrong_python_stops_before_environment(attribute, value, monkeypatch):
    monkeypatch.setattr(live.sys, attribute, value)
    result = asyncio.run(live.run_live_evaluation(allow_live=True, environ=NoEnvironmentReads()))
    assert result.reason == 'configuration_error'


def test_wrong_root_stops_before_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = asyncio.run(live.run_live_evaluation(allow_live=True, environ=NoEnvironmentReads()))
    assert result.reason == 'configuration_error'


def test_cli_disabled_and_invalid_options_are_sanitized(capsys, monkeypatch):
    monkeypatch.setattr(live, 'load_nebius_settings', lambda *args: pytest.fail('disabled credentials access'))
    live.main([])
    assert json.loads(capsys.readouterr().out)['reason'] == 'disabled'
    for arguments in (['--max-model-requests', '7'], ['--source-root', 'PRIVATE_SENTINEL'], ['--scenario', 'PRIVATE_SENTINEL']):
        with pytest.raises(SystemExit) as error:
            live.main(arguments)
        assert error.value.code == 2
        captured = capsys.readouterr()
        assert 'PRIVATE_SENTINEL' not in captured.err + captured.out


def test_raised_limit_copy_is_rejected_without_provider():
    output = asyncio.run(live.run_live_evaluation(allow_live=True, environ=NoEnvironmentReads(),
        limits=GlobalLimits().model_copy(update={'max_tool_attempts': 99})))
    assert output.reason == 'configuration_error' and not output.started


def test_caller_cancellation_propagates_and_closes_providers():
    async def scenario():
        snapshot = load_scenario(load_dataset().scenarios[0])
        models = []
        ready = asyncio.Event()
        class WaitingProvider(FakeProvider):
            async def ainvoke(self, messages, **kwargs):
                ready.set()
                return await super().ainvoke(messages, **kwargs)
        def factory(*args):
            model = WaitingProvider(snapshot, delay=10)
            models.append(model)
            return model
        task = asyncio.create_task(live.run_live_evaluation(allow_live=True, environ=ENV, provider_factory=factory))
        await asyncio.wait_for(ready.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(m.closed for m in models) and any(m.cancelled for m in models)
    asyncio.run(scenario())


def test_cleanup_is_bounded_and_never_restarts_work(monkeypatch):
    monkeypatch.setattr(live, 'CLEANUP_SECONDS', .02)
    async def hanging(self, *args):
        await asyncio.sleep(1)
    monkeypatch.setattr(FakeProvider, '__aexit__', hanging)
    output, models = execute()
    assert output.cleanup == 'timed_out'
    assert sum(m.calls for m in models) <= 6


def test_safe_artifacts_are_local_and_do_not_overwrite(tmp_path, monkeypatch):
    output, _ = execute(mode='finding')
    monkeypatch.setattr(live, 'PROJECT_ROOT', tmp_path)
    paths = live.save_live_artifacts(output)
    assert all(p.is_relative_to(tmp_path / '.runtime/evaluation') for p in paths)
    saved = json.loads(paths[0].read_text())
    assert saved['evaluation']['quality']['pending'] == 1
    assert saved['report']['report_id'] == output.report.report_id
    assert 'snippet' not in paths[0].read_text()
    with pytest.raises(live.EvaluationError, match='artifact_write_failed'):
        live.save_live_artifacts(output)
    with pytest.raises(live.EvaluationError, match='unsafe_output'):
        live.render_live_json(output.model_copy(update={'configuration': {'raw': 'PRIVATE'}}))


@pytest.mark.parametrize('status', [200, 429, 500])
def test_real_nebius_adapter_mock_http_has_no_hidden_retries_or_tracing(status, monkeypatch, caplog):
    import httpx
    import logging
    from langsmith import get_tracing_context
    snapshot = load_scenario(load_dataset().scenarios[0])
    before = {f.file_path: sha256(text.encode()).hexdigest() for f in snapshot.manifest.files
              for (side,path), text in snapshot.sources.items() if path == f.file_path}
    requests = []
    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert get_tracing_context()['enabled'] is False
        assert request.method == 'POST' and str(request.url) == 'https://api.tokenfactory.nebius.com/v1/chat/completions'
        assert body['model'] == ENV['NEBIUS_MODEL'] and body['max_completion_tokens'] == 1000
        assert 'full_refund' not in request.content.decode() and 'expected_id' not in request.content.decode()
        if status != 200:
            return httpx.Response(status, json={'error': {'message': 'PRIVATE_PROVIDER_BODY synthetic-test-key'}})
        if len(body['messages']) == 2:
            message = {'role': 'assistant', 'content': None, 'reasoning': None, 'tool_calls': [
                {'id': f'read-{i}', 'type': 'function', 'function': {'name': 'read_source_lines',
                'arguments': json.dumps({'file_path': f.file_path, 'start_line': 1,
                        'end_line': len(snapshot.sources[f.side,f.file_path].splitlines())})}}
                for i,f in enumerate(snapshot.manifest.files)]}
        else:
            message = {'role': 'assistant', 'content': '{"findings": []}', 'reasoning': None}
        return httpx.Response(200, json={'id': 'offline-mock', 'object': 'chat.completion', 'created': 0,
            'model': ENV['NEBIUS_MODEL'], 'choices': [{'index': 0, 'message': message,
            'finish_reason': 'tool_calls' if message.get('tool_calls') else 'stop'}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}})
    original = live.NebiusModel
    def offline(settings, **kwargs):
        return original(settings, **kwargs, transport=httpx.MockTransport(respond))
    monkeypatch.setattr(live, 'NebiusModel', offline)
    monkeypatch.setenv('LANGSMITH_TRACING', 'true')
    monkeypatch.setenv('LANGCHAIN_TRACING_V2', 'true')
    with caplog.at_level(logging.DEBUG):
        output = asyncio.run(live.run_live_evaluation(allow_live=True, environ=ENV))
    assert output.budget_usage.model_attempts == len(requests) == (6 if status == 200 else 3)
    assert output.budget_usage.unknown_usage_attempts == (0 if status == 200 else 3)
    assert output.reason == ('completed' if status == 200 else 'partial')
    assert 'PRIVATE_PROVIDER_BODY' not in caplog.text + live.render_live_json(output)
    assert 'synthetic-test-key' not in caplog.text + live.render_live_json(output)
    after = load_scenario(load_dataset().scenarios[0])
    assert before == {f.file_path: sha256(after.sources[f.side,f.file_path].encode()).hexdigest() for f in after.manifest.files}
