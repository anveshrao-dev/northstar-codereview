"""Real LangGraph workflows, synthetic models, no provider or source execution."""

import asyncio
import json

import pytest

from northstar_codereview.evaluation_data import load_dataset
from northstar_codereview.evaluation_runner import (baseline_configuration, load_scripts, run_scripted,
                                                  render_summary, write_artifacts)
from northstar_codereview.evaluation import EvaluationError, fingerprint


@pytest.mark.parametrize('index,counts', [(0, (1, 1, 0)), (1, (1, 0, 1)), (2, (2, 0, 0))])
def test_scripted_baseline_exercises_real_graphs_and_predeclared_judgments(index, counts):
    data = load_dataset()
    run = asyncio.run(run_scripted(data, data.scenarios[index].id))
    result = run.evaluation
    assert result.review_status == 'completed'
    assert (result.quality.true_positives, result.quality.false_positives, result.quality.false_negatives) == counts
    assert result.run_kind == 'scripted_integration' and not result.independent_model_accuracy_established
    assert result.workflow.model_attempts == 3 * len(data.scenarios[index].specialists)
    assert result.workflow.tool_attempts == len(data.scenarios[index].specialists) * (1 + len(data.scenarios[index].snapshot.files))
    assert result.workflow.evidence_validation_rate == 1
    assert result.workflow.reported_input_tokens is None and result.workflow.unknown_usage_attempts > 0
    assert result.workflow.retries == result.workflow.repairs == 0
    assert {f.agent_name for f in run.report.findings} <= set(data.scenarios[index].specialists)
    assert all(f.evidence_status == 'validated' and f.claim_status == 'unverified' for f in run.report.findings)


@pytest.mark.parametrize('mode,status', [('recovery', 'partial'), ('failure', 'failed')])
def test_partial_recovery_and_failed_execution_remain_unscored(mode, status):
    data = load_dataset()
    run = asyncio.run(run_scripted(data, 'java_backend', mode=mode))
    result = run.evaluation
    assert result.review_status == status and not result.quality.accuracy_scored
    assert all(c.status != 'passed' for c in result.controls)
    if mode == 'recovery':
        assert result.workflow.recovered_failures == result.workflow.recovery_attempts == 1
        assert result.workflow.tool_failures == 1 and result.workflow.unresolved_failures == 0
    else:
        assert result.workflow.model_attempts is None and result.workflow.tool_attempts is None
        assert result.workflow.evidence_validation_rate is None


def test_script_data_is_not_passed_into_agent_instructions(monkeypatch):
    from northstar_codereview import evaluation_runner as module
    original = module.ScriptedModel.ainvoke
    seen = []
    async def inspect(self, messages, **kwargs):
        if not self.calls:
            text = '\n'.join(m.content for m in messages)
            assert 'expected_id' not in text and 'deliberate_balance_false_positive' not in text
            assert 'ground-truth' not in text.lower()
            seen.append(True)
        return await original(self, messages, **kwargs)
    monkeypatch.setattr(module.ScriptedModel, 'ainvoke', inspect)
    data = load_dataset()
    asyncio.run(run_scripted(data, 'java_backend'))
    assert len(seen) == 3


def test_invalid_evidence_does_not_receive_semantic_credit(monkeypatch):
    from northstar_codereview import evaluation_runner as module
    original = module.ScriptedModel.ainvoke
    async def corrupt(self, messages, **kwargs):
        response = await original(self, messages, **kwargs)
        if not response.tool_calls and response.content:
            payload = json.loads(response.content)
            for claim in payload['findings']:
                claim['evidence'][0]['snippet'] = 'fabricated source\n'
            response.content = json.dumps(payload)
        return response
    monkeypatch.setattr(module.ScriptedModel, 'ainvoke', corrupt)
    run = asyncio.run(run_scripted(load_dataset(), 'java_backend'))
    assert run.evaluation.review_status == 'partial'
    assert not run.report.findings
    assert run.evaluation.workflow.evidence_validation_rate == 0
    assert run.evaluation.quality.true_positives == 0


def test_safe_artifacts_and_configuration_are_reproducible(tmp_path, monkeypatch):
    from northstar_codereview import evaluation_runner as module
    data = load_dataset()
    run = asyncio.run(run_scripted(data, 'java_backend'))
    config = baseline_configuration(load_scripts(), 'baseline')
    assert fingerprint(config) == run.evaluation.configuration_id
    text = render_summary([run.evaluation])
    assert 'Scripted integration' in text and 'model accuracy' in text
    assert 'return amount' not in text and 'snippet' not in text
    # Fixed artifact root is replaced with a project-local test root only.
    monkeypatch.setattr(module, 'PROJECT_ROOT', tmp_path)
    paths = write_artifacts([run.evaluation], config)
    assert all(p.is_relative_to(tmp_path / '.runtime' / 'evaluation') for p in paths)
    payload = json.loads(paths[0].read_text())
    assert payload['configuration'] == config and payload['network_attempts'] == 0
    assert payload['results'][0]['dataset_id'] == fingerprint(data)
    assert paths[1].read_text() == text
    # No overwrite, even when the same completed evaluation is saved twice.
    with pytest.raises(EvaluationError, match='artifact_write_failed'):
        write_artifacts([run.evaluation], config)


def test_artifact_writer_refuses_symlink_and_configuration_mismatch(tmp_path, monkeypatch):
    from northstar_codereview import evaluation_runner as module
    run = asyncio.run(run_scripted(load_dataset(), 'java_backend'))
    config = baseline_configuration(load_scripts(), 'baseline')
    monkeypatch.setattr(module, 'PROJECT_ROOT', tmp_path)
    (tmp_path / 'other').mkdir()
    (tmp_path / '.runtime').symlink_to(tmp_path / 'other', target_is_directory=True)
    with pytest.raises(EvaluationError, match='artifact_write_failed'):
        write_artifacts([run.evaluation], config)
    with pytest.raises(EvaluationError, match='configuration_mismatch'):
        write_artifacts([run.evaluation], {**config, 'mode': 'changed'})


def test_offline_guard_detects_even_caught_connection_attempt():
    import socket
    from northstar_codereview.evaluation_runner import _offline_only
    with pytest.raises(EvaluationError, match='network_attempt_blocked'):
        with _offline_only():
            try:
                socket.getaddrinfo('fictional.example.invalid', 443)
            except EvaluationError:
                pass


def test_cli_has_no_live_opt_in(capsys):
    from northstar_codereview.evaluation_runner import main
    with pytest.raises(SystemExit) as error:
        main(['--allow-live'])
    assert error.value.code == 2
    assert 'unrecognized arguments' in capsys.readouterr().err


def test_changed_scripted_claim_is_pending_not_silently_rejudged(monkeypatch):
    from northstar_codereview import evaluation_runner as module
    original = module.ScriptedModel.ainvoke
    async def alter(self, messages, **kwargs):
        response = await original(self, messages, **kwargs)
        if not response.tool_calls and response.content:
            payload = json.loads(response.content)
            for claim in payload['findings']:
                claim['explanation'] = 'A different claim on the same source lines.'
            response.content = json.dumps(payload)
        return response
    monkeypatch.setattr(module.ScriptedModel, 'ainvoke', alter)
    run = asyncio.run(run_scripted(load_dataset(), 'java_backend'))
    assert run.evaluation.quality.pending == 2 and run.evaluation.quality.true_positives == 0
    assert not run.evaluation.quality.accuracy_scored
    assert run.evaluation.controls[0].status == 'pending'


def test_public_runner_enforces_guard_even_when_model_catches_attempt(monkeypatch):
    import socket
    from northstar_codereview import evaluation_runner as module
    original = module.ScriptedModel.ainvoke
    outer_attempts = []
    def outer_network(*args, **kwargs):
        outer_attempts.append(True)
        raise RuntimeError('Outer synthetic deny sentinel')
    async def try_network(self, messages, **kwargs):
        try:
            socket.getaddrinfo('fictional.example.invalid', 443)
        except (EvaluationError, RuntimeError):
            pass
        return await original(self, messages, **kwargs)
    monkeypatch.setattr(socket, 'getaddrinfo', outer_network)
    monkeypatch.setattr(module.ScriptedModel, 'ainvoke', try_network)
    with pytest.raises(EvaluationError, match='network_attempt_blocked'):
        asyncio.run(run_scripted(load_dataset(), 'java_backend'))
    assert outer_attempts == []
