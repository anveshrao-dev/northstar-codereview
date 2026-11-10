"""UI boundary tests: fixed fixtures, typed projections, no live execution."""
import asyncio
import json
from uuid import uuid4

import pytest

from northstar_codereview import ui_service as service
from northstar_codereview.ui_models import present_review
from northstar_codereview.approval_models import ApprovalError
from northstar_codereview.evaluation import fingerprint
from northstar_codereview.evaluation_data import load_dataset
from northstar_codereview.evaluation_runner import run_scripted
from northstar_codereview.live_evaluation import LiveEvaluation, _configuration
from northstar_codereview.execution_budget import GlobalLimits, ExecutionBudget


@pytest.fixture(scope='module')
def scripted():
    return asyncio.run(run_scripted(load_dataset(), 'java_backend'))


@pytest.mark.parametrize('scenario', ['java_backend', 'angular_frontend', 'jenkins_aks'])
def test_three_pinned_scenarios_use_existing_scripted_graph(scenario):
    bundle = service.run_baseline(scenario)
    screen = present_review(bundle)
    assert screen.scenario_id == scenario and screen.provenance == 'scripted_integration'
    assert len(screen.specialists) == 5
    assert screen.network_attempts == 0 and screen.accuracy_established is False
    assert screen.findings and all(f.evidence_status == 'validated' and f.claim_status == 'unverified' for f in screen.findings)
    assert screen.report_id == bundle.report.report_id


@pytest.mark.parametrize('scenario', ['../outside', 'gitops_selector_slice', 'PRIVATE'])
def test_arbitrary_scenarios_rejected(scenario):
    with pytest.raises(service.UIError) as caught:
        service.run_baseline(scenario)
    assert 'PRIVATE' not in str(caught.value)


def test_severity_confidence_and_evidence_presentation(scripted):
    bundle = service.ReviewBundle(scenario_id='java_backend', provenance='scripted_integration',
                                  report=scripted.report, evaluation=scripted.evaluation)
    view = present_review(bundle)
    assert [f.finding_id for f in view.findings] == [f.finding_id for f in scripted.report.findings]
    assert view.findings[0].confidence_percent == scripted.report.findings[0].confidence * 100
    assert all(f.locations and f.remediation for f in view.findings)
    assert 'snippet' not in view.model_dump_json()
    assert view.approval_threshold is None


def test_partial_empty_result_is_not_clean_negative_control():
    run = asyncio.run(run_scripted(load_dataset(), 'java_backend', mode='failure'))
    bundle = service.ReviewBundle(scenario_id='java_backend', provenance='scripted_integration', report=run.report, evaluation=run.evaluation)
    view = present_review(bundle)
    assert not view.findings and view.status != 'completed'
    assert all(c.status != 'passed' for c in view.controls)


def live_payload():
    # In-memory HTTP/model use is unnecessary: this is a typed saved-result projection.
    limits = GlobalLimits()
    return LiveEvaluation(reason='deadline', started=True, scenario_id='angular_frontend',
                          configuration=_configuration(limits), budget_usage=ExecutionBudget(limits).usage()).model_dump(mode='json')


def write_artifact(root, payload):
    directory = root / '.runtime/evaluation'
    directory.mkdir(parents=True, exist_ok=True)
    name = fingerprint(payload) + '.json'
    (directory / name).write_text(json.dumps(payload))
    return name


def test_saved_live_is_read_only_and_cannot_be_confused_with_scripted(tmp_path, monkeypatch):
    monkeypatch.setattr(service, 'PROJECT_ROOT', tmp_path)
    name = write_artifact(tmp_path, live_payload())
    records = service.saved_live('angular_frontend')
    assert len(records) == 1 and records[0].artifact_name == name
    view = present_review(records[0])
    assert view.provenance == 'recorded_live' and view.status == 'missing'
    assert len(view.specialists) == 5 and not view.findings
    assert not view.accuracy_established


@pytest.mark.parametrize('attack', ['wrong_digest', 'symlink', 'directory_symlink', 'oversize', 'secret'])
def test_unsafe_artifacts_are_never_displayed(tmp_path, monkeypatch, attack):
    monkeypatch.setattr(service, 'PROJECT_ROOT', tmp_path)
    payload = live_payload()
    if attack == 'secret': payload['configuration']['private'] = 'Bearer PRIVATE_SENSITIVE_VALUE'
    name = write_artifact(tmp_path, payload)
    path = tmp_path / '.runtime/evaluation' / name
    if attack == 'wrong_digest': path.write_text('{}')
    if attack == 'symlink':
        actual = tmp_path / 'synthetic.json'
        path.rename(actual)
        path.symlink_to(actual)
    if attack == 'directory_symlink':
        directory = path.parent
        destination = tmp_path / 'moved'
        directory.rename(destination)
        directory.symlink_to(destination)
    if attack == 'oversize': path.write_text('x' * (4*1024*1024+1))
    assert service.saved_live('angular_frontend') == ()


def test_mismatched_evaluation_cannot_relabel_report(scripted):
    with pytest.raises(ValueError):
        service.ReviewBundle(scenario_id='angular_frontend', provenance='scripted_integration',
                            report=scripted.report, evaluation=scripted.evaluation)


@pytest.mark.parametrize('mismatch', ['configuration', 'configuration_id', 'snapshot'])
def test_rehashed_live_artifact_still_requires_contract_consistency(tmp_path, monkeypatch, mismatch):
    from northstar_codereview.evaluation import evaluate
    payload = live_payload()
    evaluation = evaluate(load_dataset(), 'angular_frontend', None,
        configuration_id=fingerprint(payload['configuration']), run_kind='external_review')
    payload['evaluation'] = evaluation.model_dump(mode='json')
    if mismatch == 'configuration': payload['configuration']['tracing'] = True
    if mismatch == 'configuration_id': payload['evaluation']['configuration_id'] = '0'*64
    if mismatch == 'snapshot': payload['evaluation']['snapshot_id'] = str(uuid4())
    monkeypatch.setattr(service, 'PROJECT_ROOT', tmp_path)
    write_artifact(tmp_path, payload)
    assert service.saved_live('angular_frontend') == ()


def test_local_approval_requires_explicit_decision_and_preserves_claims(scripted):
    # Approval storage remains project-local; pytest's tmp_path is elsewhere than
    # .runtime and is intentionally not accepted by the existing workflow.
    from northstar_codereview.local_source import PROJECT_ROOT
    database = PROJECT_ROOT / '.runtime' / f'ui-test-{uuid4()}.sqlite3'
    try:
        service_approval = service.LocalReview(database=database)
        view = service_approval.start(scripted.report)
        assert view.status == 'pending' and view.outcome is None
        result = service_approval.submit(view.thread_id, scripted.report, 'reject', acknowledge_partial=False)
        assert result.outcome.decision == 'reject'
        assert result.report == scripted.report
        assert all(f.claim_status == 'unverified' for f in result.report.findings)
        with pytest.raises(ApprovalError):
            service_approval.submit(view.thread_id, scripted.report, 'approve', acknowledge_partial=True)
    finally:
        for suffix in ('', '-wal', '-shm', '-journal'):
            path = database.with_name(database.name + suffix)
            if path.exists(): path.unlink()
