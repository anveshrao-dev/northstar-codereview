"""Offline platform routing, real five-subgraph execution, and report contracts."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from northstar_codereview.agents.code_quality import CodeQualityAgent, RunLimits
from northstar_codereview.agents.gitops import GitOpsAgent
from northstar_codereview.agents.cicd_groovy import CICDGroovyAgent
from northstar_codereview.local_source import collect_local_snapshot
from northstar_codereview.models import AgentName, ReviewRequest, SpecialistStatus
from northstar_codereview.orchestrator import ReviewOrchestrator, OrchestrationLimits, select_specialists
from northstar_codereview.reporting import build_report, render_json, render_markdown


FIXTURES = Path(__file__).parent / "fixtures"
NAMES = tuple(AgentName)
CORE = NAMES[:3]
PLATFORM = (AgentName.GITOPS, AgentName.CICD_GROOVY)
SEC = AgentName.SECURITY
GIT = AgentName.GITOPS
CI = AgentName.CICD_GROOVY
CATEGORIES = dict(zip(NAMES, ("correctness", "security", "tests", "gitops", "cicd"), strict=True))
SEVERITIES = dict(zip(NAMES, ("low", "critical", "medium", "high", "medium"), strict=True))


def capture(root):
    return collect_local_snapshot(ReviewRequest(mode="local", local_path="."), source_root=root)


@pytest.fixture
def integrated():
    return capture(FIXTURES / "integrated_review")


@pytest.mark.parametrize("path,text,selected,uncertain", [
    ("config.yaml", "retries: 3\n", (SEC,), True),
    ("values.yaml", "replicas: 2\n", (SEC,), True),
    ("config.json", '{"enabled": true}', (SEC,), True),
    ("workload.yaml", "kind: Deployment\n", (SEC,), True),
    ("workload.yaml", "apiVersion: apps/v1\nkind: Deployment\n", (SEC, GIT), False),
    ("service.yml", "apiVersion: v1\nkind: Service\n", (SEC, GIT), False),
    ("job.json", '{"apiVersion":"batch/v1","kind":"Job"}', (SEC, GIT), False),
    ("release.yaml", "apiVersion: helm.toolkit.fluxcd.io/v2\nkind: HelmRelease\n", (SEC, GIT), False),
    ("source.yaml", "apiVersion: source.toolkit.fluxcd.io/v1\nkind: GitRepository\n", (SEC, GIT), False),
    ("kustomization.yaml", "resources:\n  - app.yaml\n", (SEC, GIT), False),
    ("Kustomization", "resources: []\n", (GIT,), False),
    ("chart/Chart.yaml", "apiVersion: v2\nname: fictional\n", (SEC, GIT), False),
    ("gitops/release.yaml", "fictional: true\n", (SEC, GIT), False),
    ("flux/release.yaml", "fictional: true\n", (SEC, GIT), False),
    ("helm/templates/_helpers.tpl", "fictional template data", (GIT,), False),
    ("Jenkinsfile", "pipeline { agent none }", (CI,), False),
    ("ci/Jenkinsfile.release", "pipeline { agent none }", (CI,), False),
    ("standalone.groovy", "class Fictional {}", (CI,), True),
    ("src/com/northstar/Helper.groovy", "class Helper {}", (CI,), True),
    ("vars/publish.groovy", "def call() {}", (CI,), True),
    (".jenkins/config.yaml", "fictional: true", (SEC, CI), False),
    ("notes.unknown", "fictional", (), False),
    ("example.yaml", "example: |\n  apiVersion: apps/v1\n  kind: Deployment\n", (SEC,), True),
    ("example.yaml", "# apiVersion: apps/v1\n# kind: Deployment\n", (SEC,), True),
    ("example.yaml", "apiVersion: apps/v1\n---\nkind: Deployment\n", (SEC,), True),
    ("example.yaml", "apiVersion: unknown/v1\nkind: Deployment\n", (SEC,), True),
    ("example.yaml", 'apiVersion: "apps/v1"\nkind: \'Deployment\' # hint\n', (SEC, GIT), False),
])
def test_static_platform_routing(tmp_path, path, text, selected, uncertain):
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    snapshot = capture(tmp_path)
    decision = select_specialists(snapshot)
    assert decision == select_specialists(snapshot)
    assert decision.selected_specialists == selected
    assert bool(decision.uncertain_files) is uncertain
    assert bool(decision.unsupported_files) == (not selected)


@pytest.mark.parametrize("files,selected", [
    ({"chart/Chart.yaml": "name: fictional", "chart/values.yaml": "replicas: 2"}, (SEC, GIT)),
    ({"Jenkinsfile": "pipeline {}", "pom.xml": "<project/>"}, (SEC, CI)),
    ({"Jenkinsfile": "pipeline {}", "Kustomization": "resources: []"}, (GIT, CI)),
    ({"src/app.py": "pass", "src/app.ts": "export {};", "src/App.java": "class App {}"}, CORE),
])
def test_contextual_and_original_routing(tmp_path, files, selected):
    for path, content in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    decision = select_specialists(capture(tmp_path))
    assert decision.selected_specialists == selected
    assert not decision.unsupported_files
    assert not decision.uncertain_files


class IntegratedModel:
    """Uses fixture bounds explicitly: tests plumbing, never detection accuracy."""
    def __init__(self, name, snapshot, coordinate=None):
        self.name, self.snapshot, self.coordinate = name, snapshot, coordinate
        self.histories = []

    def bind_tools(self, tools):
        self.tools = tools
        if not hasattr(self, 'inspection_tools'):
            self.inspection_tools = tools
        return self

    async def ainvoke(self, messages, **kwargs):
        self.histories.append(list(messages))
        if self.coordinate:
            await self.coordinate(self)
        if len(self.histories) == 1:
            return AIMessage(content="", tool_calls=[dict(name="list_snapshot_files", args={}, id="list")])
        observations = [json.loads(m.content) for m in messages if isinstance(m, ToolMessage)]
        if len(self.histories) == 2:
            # All seven captured files are inspected through the actual tools.
            calls = [dict(name="read_source_lines", args={"file_path": path, "start_line": 1,
                          "end_line": len(text.splitlines())}, id=f"read-{index}")
                     for index, ((side, path), text) in enumerate(sorted(self.snapshot.sources.items()))]
            return AIMessage(content="", tool_calls=calls)
        evidence = next(observation["evidence"] for observation in observations if observation.get("evidence"))
        proposal = dict(title=f"Scripted {self.name.value} integration claim", category=CATEGORIES[self.name],
                        severity=SEVERITIES[self.name], confidence=0.5,
                        explanation="Synthetic plumbing check; defect correctness is not established.",
                        suggested_remediation="Investigate independently.", assumptions=["Scripted fixture bounds."],
                        evidence=evidence)
        return AIMessage(content=json.dumps({"findings": [proposal]}))


def make_orchestrator(snapshot, models=None, **kwargs):
    models = {} if models is None else models
    def factory(name):
        model = IntegratedModel(name, snapshot)
        models[name] = model
        return model
    return ReviewOrchestrator(snapshot, model_factory=factory, approved_synthetic=True, **kwargs)


def test_five_way_parallel_identity_budgets_and_prioritized_report(integrated, monkeypatch):
    labels = json.loads((FIXTURES / "integrated_review_expected.json").read_text())
    assert len(integrated.manifest.files) == labels["file_count"] == 7
    assert list(select_specialists(integrated).selected_specialists) == labels["selected_specialists"]
    before = {p: p.read_bytes() for p in (FIXTURES / "integrated_review").rglob("*") if p.is_file()}
    received = []
    original_init = CodeQualityAgent.__init__
    def record(self, snapshot, **kwargs):
        received.append((snapshot, kwargs["limits"]))
        original_init(self, snapshot, **kwargs)
    monkeypatch.setattr(CodeQualityAgent, "__init__", record)
    import subprocess
    def forbidden(*args, **kwargs):
        pytest.fail("Reviewed source or external commands must never execute")
    monkeypatch.setattr(subprocess, "Popen", forbidden)

    async def scenario():
        ready = asyncio.Event()
        arrived, finished, models = set(), [], {}
        order = (CI, GIT, SEC, AgentName.TEST_COVERAGE, AgentName.CODE_QUALITY)
        completed = {name: asyncio.Event() for name in NAMES}
        delivered, delivery_ready = [], {name: asyncio.Event() for name in NAMES}
        original_execute = ReviewOrchestrator._execute
        async def ordered_delivery(self, name, state, runtime):
            update = await original_execute(self, name, state, runtime)
            index = order.index(name)
            if index:
                await delivery_ready[order[index - 1]].wait()
            delivered.append(name)
            delivery_ready[name].set()
            return update
        monkeypatch.setattr(ReviewOrchestrator, "_execute", ordered_delivery)
        async def coordinate(model):
            if len(model.histories) == 1:
                arrived.add(model.name)
                if len(arrived) == 5:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 2)
            if len(model.histories) == 3:
                index = order.index(model.name)
                if index:
                    await completed[order[index - 1]].wait()
                finished.append(model.name)
                completed[model.name].set()
        def factory(name):
            model = IntegratedModel(name, integrated, coordinate)
            models[name] = model
            return model
        limits = RunLimits(max_provider_retries=0, max_output_repairs=0)
        orchestrator = ReviewOrchestrator(integrated, model_factory=factory, approved_synthetic=True, specialist_limits=limits)
        assert set(NAMES) <= set(orchestrator.graph.get_graph().nodes)
        output = await orchestrator.run(review_id=uuid4())
        assert arrived == set(NAMES) and finished == delivered == list(order)
        assert output.status == "completed" and not output.failures
        assert tuple(item.result.agent_name for item in output.specialist_results) == NAMES
        assert len({item.result.job_id for item in output.specialist_results}) == 5
        assert len(received) == 5 and all(source is integrated and cap is limits for source, cap in received)
        assert len({id(model.histories) for model in models.values()}) == 5
        assert len({id(model.inspection_tools[0]) for model in models.values()}) == 5
        assert all(model.tools == () for model in models.values())
        for item in output.specialist_results:
            assert item.result.review_id == output.review_id and item.result.snapshot_id == output.snapshot_id
            assert item.result.status == "completed" and len(item.result.reviewed_files) == 7
            assert item.usage.model_calls == 3 and item.usage.tool_attempts == 8
            assert item.usage.retries == item.usage.repairs == 0
        for model in models.values():
            assert len(model.histories[0]) == 2
            tool_messages = [m for m in model.histories[-1] if isinstance(m, ToolMessage)]
            assert {m.tool_call_id for m in tool_messages} == {"list", *(f"read-{i}" for i in range(7))}
        assert len(output.validated_findings) == 5
        assert {item.agent_name for item in output.validated_findings} == set(NAMES)
        assert all(item.evidence_status == "validated" and item.claim_status == "unverified" for item in output.validated_findings)
        report = build_report(output, snapshot=integrated.manifest)
        assert {item.agent_name for item in report.specialists} == set(NAMES)
        assert [f.severity for f in report.findings] == ["critical", "high", "medium", "medium", "low"]
        assert len(report.findings) == 5 and report.notices  # Overlap retained, not fused.
        reordered = output.model_copy(update={"specialist_results": tuple(reversed(output.specialist_results)),
                                             "validated_findings": tuple(reversed(output.validated_findings))})
        same = build_report(reordered, snapshot=integrated.manifest)
        assert render_json(report) == render_json(same)
        assert render_markdown(report) == render_markdown(same)
        assert report.report_id == same.report_id
        assert "Scripted" in render_markdown(report)
    asyncio.run(scenario())
    assert all(path.read_bytes() == data for path, data in before.items())
    assert dict(integrated.sources) == dict(capture(FIXTURES / "integrated_review").sources)


def test_run_report_reuses_one_run_and_existing_reporting(integrated, monkeypatch):
    orchestrator = make_orchestrator(integrated)
    original, outputs = orchestrator.run, []
    async def record(**kwargs):
        result = await original(**kwargs)
        outputs.append(result)
        return result
    monkeypatch.setattr(orchestrator, "run", record)
    report = asyncio.run(orchestrator.run_report(review_id=uuid4(), report_revision=2))
    assert len(outputs) == 1
    assert report == build_report(outputs[0], snapshot=integrated.manifest, report_revision=2)


@pytest.mark.parametrize("revision", [True, 0, -1, "1"])
def test_invalid_report_revision_rejected_before_model_construction(integrated, revision):
    constructed = []
    def forbidden(name):
        constructed.append(name)
        raise AssertionError("Invalid presentation parameters must not consume a review")
    orchestrator = ReviewOrchestrator(integrated, model_factory=forbidden, approved_synthetic=True)
    with pytest.raises(ValueError):
        asyncio.run(orchestrator.run_report(review_id=uuid4(), report_revision=revision))
    assert not constructed


@pytest.mark.parametrize("bound", [False, True])
def test_five_agent_shared_model_rejected_before_first_request(integrated, bound):
    shared = IntegratedModel(AgentName.CODE_QUALITY, integrated)
    class Wrapper:
        def bind_tools(self, tools):
            return shared
    orchestrator = ReviewOrchestrator(integrated, model_factory=lambda name: Wrapper() if bound else shared,
                                     approved_synthetic=True)
    result = asyncio.run(orchestrator.run(review_id=uuid4()))
    assert result.status == "failed" and not shared.histories
    assert len(result.failures) == 5 and {f.category for f in result.failures} == {"shared_model_context"}
    assert not build_report(result, snapshot=integrated.manifest).findings


@pytest.mark.parametrize("limited", PLATFORM)
def test_one_platform_exhausts_its_budget_without_consuming_other_budgets(integrated, limited):
    class RepeatingModel(IntegratedModel):
        async def ainvoke(self, messages, **kwargs):
            self.histories.append(list(messages))
            return AIMessage(content="", tool_calls=[dict(name="list_snapshot_files", args={}, id=f"list-{len(self.histories)}")])
    def factory(name):
        return (RepeatingModel if name == limited else IntegratedModel)(name, integrated)
    orchestrator = ReviewOrchestrator(integrated, model_factory=factory, approved_synthetic=True,
                                     specialist_limits=RunLimits(max_model_calls=3, max_provider_retries=0, max_output_repairs=0))
    output = asyncio.run(orchestrator.run(review_id=uuid4()))
    assert output.status == "partial" and len(output.validated_findings) == 4
    for item in output.specialist_results:
        assert item.usage.model_calls == 3
        assert item.result.status == ("partial" if item.result.agent_name == limited else "completed")
    assert build_report(output, snapshot=integrated.manifest).status == "partial"


@pytest.mark.parametrize("name,agent", [(GIT, GitOpsAgent), (CI, CICDGroovyAgent)])
@pytest.mark.parametrize("failure", ["exception", "timeout", "partial"])
def test_platform_failure_or_partial_preserves_other_four(integrated, monkeypatch, name, agent, failure):
    original = agent.run
    async def failing(self, **kwargs):
        if failure == "exception":
            raise RuntimeError("PRIVATE_SENTINEL")
        if failure == "timeout":
            await asyncio.sleep(1)
        run = await original(self, **kwargs)
        return run.model_copy(update={"result": run.result.model_copy(update={"status": SpecialistStatus.PARTIAL, "limitations": ("Static scope incomplete.",)})})
    monkeypatch.setattr(agent, "run", failing)
    output = asyncio.run(make_orchestrator(integrated, orchestration_limits=OrchestrationLimits(specialist_timeout_seconds=0.4)).run(review_id=uuid4()))
    assert output.status == "partial"
    assert all(item.result.status == "completed" for item in output.specialist_results if item.result.agent_name != name)
    assert len(output.validated_findings) == (5 if failure == "partial" else 4)
    assert [f.category for f in output.failures] == ([] if failure == "partial" else [failure])
    report = build_report(output, snapshot=integrated.manifest)
    assert report.status == "partial" and "PRIVATE_SENTINEL" not in render_json(report)


@pytest.mark.parametrize("name,agent", [(GIT, GitOpsAgent), (CI, CICDGroovyAgent)])
@pytest.mark.parametrize("field", ["review_id", "snapshot_id", "job_id", "agent_name"])
def test_platform_identity_substitution_is_rejected(integrated, monkeypatch, name, agent, field):
    original = agent.run
    async def corrupt(self, **kwargs):
        run = await original(self, **kwargs)
        value = AgentName.CODE_QUALITY if field == "agent_name" else uuid4()
        return run.model_copy(update={"result": run.result.model_copy(update={field: value})})
    monkeypatch.setattr(agent, "run", corrupt)
    output = asyncio.run(make_orchestrator(integrated).run(review_id=uuid4()))
    assert output.status == "partial" and len(output.validated_findings) == 4
    assert [(f.agent_name, f.category) for f in output.failures] == [(name, "invalid_output")]


@pytest.mark.parametrize("name", PLATFORM)
@pytest.mark.parametrize("delivery", ["missing", "duplicate", "conflict"])
def test_platform_delivery_join_preserves_results(integrated, monkeypatch, name, delivery):
    original = ReviewOrchestrator._execute
    async def alter(self, current, state, runtime):
        update = await original(self, current, state, runtime)
        if current == name:
            first = update["deliveries"][0]
            other = first.model_copy(update={"execution": first.execution.model_copy(update={
                "result": first.execution.result.model_copy(update={"limitations": ("Different delivery.",)})})})
            update["deliveries"] = {"missing": (), "duplicate": (first, first), "conflict": (first, other)}[delivery]
        return update
    monkeypatch.setattr(ReviewOrchestrator, "_execute", alter)
    output = asyncio.run(make_orchestrator(integrated).run(review_id=uuid4()))
    assert len(output.specialist_results) == 5
    assert len(output.validated_findings) == (5 if delivery == "duplicate" else 4)
    assert [f.category for f in output.failures] == {"missing": ["missing_result"], "duplicate": [], "conflict": ["conflicting_duplicate"]}[delivery]
    report = build_report(output, snapshot=integrated.manifest)
    assert len(report.findings) == len(output.validated_findings)


def test_routing_uncertainty_is_carried_into_report(tmp_path):
    (tmp_path / "standalone.groovy").write_text("class Fictional {}\n")
    snapshot = capture(tmp_path)
    report = asyncio.run(make_orchestrator(snapshot).run_report(review_id=uuid4()))
    assert any("Jenkins" in note and "unverified" in note for note in report.limitations)
    assert [s.agent_name for s in report.specialists if s.selected] == [CI]
