"""Real outer/subgraph orchestration with offline, bounded scripted models."""

import asyncio
import json
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from northstar_codereview.agents.code_quality import RunLimits
from northstar_codereview.local_source import CollectedSnapshot, collect_local_snapshot
from northstar_codereview.models import AgentName, ReviewRequest, SpecialistStatus
from northstar_codereview.orchestrator import (
    ReviewOrchestrator, OrchestrationLimits, SpecialistDelivery, merge_deliveries,
    select_specialists,
)


NAMES = (AgentName.CODE_QUALITY, AgentName.SECURITY, AgentName.TEST_COVERAGE)
ALL_NAMES = (*NAMES, AgentName.GITOPS, AgentName.CICD_GROOVY)
SOURCE = 'def amount(value):\n    """Fictional review input, never executed."""\n    return value - 1\n'


@pytest.fixture
def snapshot(tmp_path):
    (tmp_path / "amount.py").write_text(SOURCE)
    return collect_local_snapshot(ReviewRequest(mode="local", local_path="."), source_root=tmp_path)


class FakeModel:
    def __init__(self, name, *, findings=False, on_request=None):
        self.name = name
        self.findings = findings
        self.on_request = on_request
        self.histories = []

    def bind_tools(self, tools):
        self.tools = tools
        return self

    async def ainvoke(self, messages, **kwargs):
        self.histories.append(list(messages))
        if self.on_request:
            await self.on_request(self, messages)
        if len(self.histories) == 1:
            return AIMessage(content="", tool_calls=[dict(name="read_source_lines", args={
                "file_path": "amount.py", "start_line": 1, "end_line": 3}, id="read")])
        observations = [json.loads(m.content) for m in messages if isinstance(m, ToolMessage)]
        proposal = dict(title="Synthetic scoped review proposal", category={
            AgentName.CODE_QUALITY: "correctness", AgentName.SECURITY: "security", AgentName.TEST_COVERAGE: "tests"}[self.name],
            severity="low", confidence=0.5, explanation="Synthetic workflow claim; semantic correctness is not established.",
            suggested_remediation="Investigate the captured behavior in context.", assumptions=["Scripted evaluation only."],
            evidence=observations[-1]["evidence"])
        return AIMessage(content=json.dumps({"findings": [proposal] if self.findings else []}))


def factory(models, **options):
    def create(name):
        model = FakeModel(name, **options)
        models[name] = model
        return model
    return create


def run(snapshot, *, model_factory=None, **kwargs):
    return asyncio.run(ReviewOrchestrator(snapshot, model_factory=model_factory or factory({}),
                                        approved_synthetic=True, **kwargs).run(review_id=uuid4()))


def result_for(output, name):
    return next(item for item in output.specialist_results if item.result.agent_name == name)


def test_real_parallel_subgraphs_rendezvous_and_out_of_order_completion(snapshot):
    async def scenario():
        arrived = set()
        ready = asyncio.Event()
        security_done = asyncio.Event()
        tests_done = asyncio.Event()
        finished = []
        models = {}

        async def coordinate(model, messages):
            if len(model.histories) == 1:
                arrived.add(model.name)
                if len(arrived) == 3:
                    ready.set()
                # A sequential dispatcher cannot pass this rendezvous.
                await asyncio.wait_for(ready.wait(), 1)
            else:
                if model.name == AgentName.CODE_QUALITY:
                    await tests_done.wait()
                elif model.name == AgentName.TEST_COVERAGE:
                    await security_done.wait()
                finished.append(model.name)
                if model.name == AgentName.SECURITY:
                    security_done.set()
                elif model.name == AgentName.TEST_COVERAGE:
                    tests_done.set()

        orchestrator = ReviewOrchestrator(snapshot, model_factory=factory(models, findings=True, on_request=coordinate),
                                          approved_synthetic=True)
        assert {"validate_context", "route", "dispatch", "code_quality", "security", "test_coverage", "collect", "finalize"} <= set(orchestrator.graph.get_graph().nodes)
        review = uuid4()
        output = await orchestrator.run(review_id=review)
        assert arrived == set(NAMES)
        assert finished == [AgentName.SECURITY, AgentName.TEST_COVERAGE, AgentName.CODE_QUALITY]
        assert output.status == "completed" and output.review_id == review
        assert output.snapshot_id == snapshot.manifest.snapshot_id and output.selected_specialists == NAMES
        assert len(output.validated_findings) == 3 and not output.failures
        assert [r.result.agent_name for r in output.specialist_results] == list(ALL_NAMES)
        assert len({r.result.job_id for r in output.specialist_results}) == 5
        for item in output.specialist_results:
            assert item.result.review_id == review and item.result.snapshot_id == snapshot.manifest.snapshot_id
            if item.result.agent_name not in NAMES:
                assert item.result.status == "skipped" and item.usage is None and not item.validated_findings
                continue
            assert item.usage.model_calls == 2 and item.usage.tool_attempts == 1
            assert item.result.status == "completed"
        assert {v.agent_name for v in output.validated_findings} == set(NAMES)
        assert all(v.claim_status == "unverified" for v in output.validated_findings)
        for model in models.values():
            assert len(model.histories[0]) == 2
            assert model.histories[1][-1].tool_call_id == "read"
        assert len({id(m.histories) for m in models.values()}) == 3
        assert len({id(m.tools[0]) for m in models.values()}) == 3
        assert dict(snapshot.sources) == {("head", "amount.py"): SOURCE}
        return output
    asyncio.run(scenario())


@pytest.mark.parametrize("extension,expected", [
    ("py", NAMES), ("java", NAMES), ("ts", NAMES), ("tsx", NAMES), ("js", NAMES),
    ("yaml", (AgentName.SECURITY,)), ("yml", (AgentName.SECURITY,)),
    ("json", (AgentName.SECURITY,)), ("tf", (AgentName.SECURITY,)), ("toml", (AgentName.SECURITY,)),
    ("unknown", ()), ("md", ()),
])
def test_deterministic_file_type_routing(tmp_path, extension, expected):
    (tmp_path / f"fictional.{extension}").write_text("fictional captured content\n")
    snapshot = collect_local_snapshot(ReviewRequest(mode="local", local_path="."), source_root=tmp_path)
    decision = select_specialists(snapshot)
    assert decision.selected_specialists == expected
    assert bool(decision.unsupported_files) == (not expected)


def test_config_runs_only_security_and_emits_explicit_skips(tmp_path):
    (tmp_path / "workload.yaml").write_text("kind: Deployment\n")
    snapshot = collect_local_snapshot(ReviewRequest(mode="local", local_path="."), source_root=tmp_path)
    called = []

    class ConfigModel(FakeModel):
        async def ainvoke(self, messages, **kwargs):
            self.histories.append(list(messages))
            if len(self.histories) == 1:
                return AIMessage(content="", tool_calls=[dict(name="read_source_lines", args={
                    "file_path": "workload.yaml", "start_line": 1, "end_line": 1}, id="yaml")])
            return AIMessage(content='{"findings": []}')

    def make(name):
        called.append(name)
        return ConfigModel(name)
    output = run(snapshot, model_factory=make)
    assert called == [AgentName.SECURITY] and output.status == "completed"
    assert result_for(output, AgentName.SECURITY).result.status == "completed"
    for name in (AgentName.CODE_QUALITY, AgentName.TEST_COVERAGE, AgentName.GITOPS, AgentName.CICD_GROOVY):
        skipped = result_for(output, name)
        assert skipped.result.status == "skipped" and skipped.result.limitations
        assert skipped.usage is None and not skipped.validated_findings


def test_unknown_only_skips_without_constructing_models(tmp_path):
    (tmp_path / "notes.unknown").write_text("fictional\n")
    snapshot = collect_local_snapshot(ReviewRequest(mode="local", local_path="."), source_root=tmp_path)
    def forbidden(name):
        pytest.fail("No models should be constructed")
    output = run(snapshot, model_factory=forbidden)
    assert output.status == "skipped" and not output.selected_specialists
    assert not output.validated_findings and all(r.result.status == "skipped" for r in output.specialist_results)
    assert any("Unsupported" in note for note in output.limitations)


def test_mixed_unknown_scope_prevents_completed_claim(snapshot, tmp_path):
    (tmp_path / "notes.unknown").write_text("fictional\n")
    mixed = collect_local_snapshot(ReviewRequest(mode="local", local_path="."), source_root=tmp_path)
    output = run(mixed)
    assert output.status == "partial" and output.selected_specialists == NAMES
    assert any("Unsupported" in note for note in output.limitations)


def test_partial_budget_result_does_not_become_completed(snapshot):
    output = run(snapshot, specialist_limits=RunLimits(max_model_calls=1))
    assert output.status == "partial"
    assert all(item.result.status == "partial" and item.usage.model_calls == 1 for item in output.specialist_results if item.result.agent_name in NAMES)
    assert all(item.result.status == "skipped" and item.usage is None for item in output.specialist_results if item.result.agent_name not in NAMES)
    assert not output.validated_findings


def test_factory_failure_isolated_and_sanitized(snapshot):
    def make(name):
        if name == AgentName.SECURITY:
            raise RuntimeError("FAKE_CREDENTIAL_SENTINEL")
        return FakeModel(name, findings=True)
    output = run(snapshot, model_factory=make)
    assert output.status == "partial" and len(output.validated_findings) == 2
    assert result_for(output, AgentName.SECURITY).result.status == "failed"
    assert [f.category for f in output.failures] == ["initialization_failed"]
    assert "FAKE_CREDENTIAL_SENTINEL" not in output.model_dump_json()


@pytest.mark.parametrize("failure", ["exception", "timeout"])
def test_specialist_failure_or_timeout_preserves_other_results(snapshot, monkeypatch, failure):
    from northstar_codereview.agents.security import SecurityAgent
    from northstar_codereview import orchestrator as module
    timers = {}
    class ControlledAsyncio:
        def __getattr__(self, name):
            return getattr(asyncio, name)
        def timeout(self, delay):
            timer = asyncio.timeout(None)
            timers[asyncio.current_task()] = timer
            return timer
    # Trigger the failing branch's real timeout after entry, rather than
    # assuming unrelated successful graphs can finish within 50 milliseconds.
    monkeypatch.setattr(module, 'asyncio', ControlledAsyncio())
    async def failing(self, **kwargs):
        if failure == "timeout":
            timers[asyncio.current_task()].reschedule(asyncio.get_running_loop().time())
            await asyncio.Event().wait()
        raise RuntimeError("SECRET_RAW_ERROR_SENTINEL")
    monkeypatch.setattr(SecurityAgent, "run", failing)
    async def scenario():
        graph = ReviewOrchestrator(snapshot, model_factory=factory({}, findings=True), approved_synthetic=True,
                                   orchestration_limits=OrchestrationLimits(specialist_timeout_seconds=0.05))
        return await asyncio.wait_for(graph.run(review_id=uuid4()), 5)
    output = asyncio.run(scenario())
    assert output.status == "partial" and len(output.validated_findings) == 2
    assert [f.category for f in output.failures] == [failure]
    assert result_for(output, AgentName.SECURITY).usage is None
    assert not result_for(output, AgentName.SECURITY).validated_findings
    assert "SECRET_RAW_ERROR_SENTINEL" not in output.model_dump_json()
    assert sum(timer.expired() for timer in timers.values()) == (failure == 'timeout')


@pytest.mark.parametrize("field", ["review_id", "snapshot_id", "job_id", "agent_name", "reviewed_files"])
def test_wrong_output_identity_rejected_before_aggregation(snapshot, monkeypatch, field):
    from northstar_codereview.agents.security import SecurityAgent
    original = SecurityAgent.run
    async def corrupt(self, **kwargs):
        output = await original(self, **kwargs)
        value = {"review_id": uuid4(), "snapshot_id": uuid4(), "job_id": uuid4(),
                 "agent_name": AgentName.CODE_QUALITY, "reviewed_files": ("missing.py",)}[field]
        return output.model_copy(update={"result": output.result.model_copy(update={field: value})})
    monkeypatch.setattr(SecurityAgent, "run", corrupt)
    output = run(snapshot)
    assert output.status == "partial" and [f.category for f in output.failures] == ["invalid_output"]
    assert result_for(output, AgentName.SECURITY).result.snapshot_id == snapshot.manifest.snapshot_id
    assert all(item.result.review_id == output.review_id for item in output.specialist_results)


def test_invalid_return_type_and_forged_validated_record_rejected(snapshot, monkeypatch):
    from northstar_codereview.agents.security import SecurityAgent
    from northstar_codereview.agents.test_coverage import TestCoverageAgent
    original = TestCoverageAgent.run
    async def invalid(self, **kwargs):
        return {"payload": "RAW_OUTPUT_SENTINEL"}
    async def forged(self, **kwargs):
        output = await original(self, **kwargs)
        item = output.validated_findings[0].model_copy(update={"finding_id": uuid4()})
        return output.model_copy(update={"validated_findings": (item,)})
    monkeypatch.setattr(SecurityAgent, "run", invalid)
    monkeypatch.setattr(TestCoverageAgent, "run", forged)
    output = run(snapshot, model_factory=factory({}, findings=True))
    assert len(output.failures) == 2 and len(output.validated_findings) == 1
    assert all(f.category == "invalid_output" for f in output.failures)
    assert "RAW_OUTPUT_SENTINEL" not in output.model_dump_json()


def test_shared_model_context_rejected_before_any_request(snapshot):
    shared = FakeModel(AgentName.CODE_QUALITY)
    output = run(snapshot, model_factory=lambda name: shared)
    assert output.status == "failed" and not shared.histories
    assert len(output.failures) == 3 and {f.category for f in output.failures} == {"shared_model_context"}


def test_shared_bound_model_rejected_even_if_factories_return_new_wrappers(snapshot):
    shared = FakeModel(AgentName.CODE_QUALITY)
    class Wrapper:
        def bind_tools(self, tools):
            return shared
    output = run(snapshot, model_factory=lambda name: Wrapper())
    assert output.status == "failed" and not shared.histories
    assert {f.category for f in output.failures} == {"shared_model_context"}


def test_same_snapshot_object_and_independent_limits(snapshot, monkeypatch):
    from northstar_codereview.agents.code_quality import CodeQualityAgent
    original = CodeQualityAgent.__init__
    received = []
    def record(self, source, **kwargs):
        received.append((source, kwargs["limits"]))
        original(self, source, **kwargs)
    monkeypatch.setattr(CodeQualityAgent, "__init__", record)
    limits = RunLimits(max_model_calls=2, max_provider_retries=0, max_output_repairs=0)
    output = run(snapshot, specialist_limits=limits)
    assert len(received) == 3 and all(source is snapshot and cap is limits for source, cap in received)
    assert all(item.usage.model_calls == 2 and item.usage.retries == 0 and item.usage.repairs == 0 for item in output.specialist_results if item.result.agent_name in NAMES)
    assert all(item.usage is None for item in output.specialist_results if item.result.agent_name not in NAMES)


def test_reducer_is_append_only_and_does_not_mutate_inputs(snapshot):
    output = run(snapshot)
    one = SpecialistDelivery(agent_name=AgentName.CODE_QUALITY, execution=output.specialist_results[0])
    two = SpecialistDelivery(agent_name=AgentName.SECURITY, execution=output.specialist_results[1])
    left, right = (one,), (two, one)
    merged = merge_deliveries(left, right)
    assert merged == (one, two, one) and left == (one,) and right == (two, one)


@pytest.mark.parametrize("conflicting", [False, True])
def test_duplicate_delivery_is_idempotent_or_fails_only_its_specialist(snapshot, monkeypatch, conflicting):
    original = ReviewOrchestrator._execute
    async def duplicate(self, name, state, runtime):
        update = await original(self, name, state, runtime)
        if name == AgentName.SECURITY:
            delivery = update["deliveries"][0]
            if conflicting:
                changed_result = delivery.execution.result.model_copy(update={"limitations": ("Different delivery",)})
                other = delivery.model_copy(update={"execution": delivery.execution.model_copy(update={"result": changed_result})})
            else:
                other = delivery
            update["deliveries"] = (delivery, other)
        return update
    monkeypatch.setattr(ReviewOrchestrator, "_execute", duplicate)
    output = run(snapshot, model_factory=factory({}, findings=True))
    assert len(output.specialist_results) == 5
    assert len(output.validated_findings) == (2 if conflicting else 3)
    assert output.status == ("partial" if conflicting else "completed")
    assert [f.category for f in output.failures] == (["conflicting_duplicate"] if conflicting else [])


def test_missing_delivery_is_not_silent_success(snapshot, monkeypatch):
    original = ReviewOrchestrator._execute
    async def missing(self, name, state, runtime):
        if name == AgentName.SECURITY:
            return {"deliveries": ()}
        return await original(self, name, state, runtime)
    monkeypatch.setattr(ReviewOrchestrator, "_execute", missing)
    output = run(snapshot)
    assert output.status == "partial" and [f.category for f in output.failures] == ["missing_result"]


def test_snapshot_digest_mismatch_fails_before_models(snapshot):
    mismatched = CollectedSnapshot(snapshot.manifest, {("head", "amount.py"): "changed source\n"})
    called = []
    orchestrator = ReviewOrchestrator(mismatched, model_factory=lambda name: called.append(name), approved_synthetic=True)
    with pytest.raises(ValueError, match="context"):
        asyncio.run(orchestrator.run(review_id=uuid4()))
    assert not called


def test_synthetic_gate_and_review_id_are_checked_before_models(snapshot):
    with pytest.raises(ValueError):
        ReviewOrchestrator(snapshot, model_factory=factory({}))
    orchestrator = ReviewOrchestrator(snapshot, model_factory=factory({}), approved_synthetic=True)
    with pytest.raises(ValueError):
        asyncio.run(orchestrator.run(review_id="MODEL_SUPPLIED_ID_SENTINEL"))


def test_reusing_orchestrator_gets_new_contexts_and_jobs(snapshot):
    created = []
    def create(name):
        model = FakeModel(name)
        created.append(model)
        return model
    orchestrator = ReviewOrchestrator(snapshot, model_factory=create, approved_synthetic=True)
    first = asyncio.run(orchestrator.run(review_id=uuid4()))
    second = asyncio.run(orchestrator.run(review_id=uuid4()))
    assert len(created) == 6 and all(len(m.histories) == 2 for m in created)
    assert not ({r.result.job_id for r in first.specialist_results} & {r.result.job_id for r in second.specialist_results})


def test_tracing_and_source_execution_disabled(snapshot, monkeypatch):
    import subprocess
    from langsmith import get_tracing_context
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    attempted = []
    def forbid(*args, **kwargs):
        attempted.append(True)
        raise AssertionError("Process execution forbidden")
    monkeypatch.setattr(subprocess, "Popen", forbid)
    async def check(model, messages):
        assert get_tracing_context()["enabled"] is False
    output = run(snapshot, model_factory=factory({}, on_request=check))
    assert output.status == "completed" and not attempted


def test_maximum_valid_limitation_does_not_break_other_results(snapshot, monkeypatch):
    from northstar_codereview.agents.security import SecurityAgent
    original = SecurityAgent.run
    note = "L" * 8000
    async def limited(self, **kwargs):
        output = await original(self, **kwargs)
        return output.model_copy(update={"result": output.result.model_copy(update={"status": SpecialistStatus.PARTIAL, "limitations": (note,)})})
    monkeypatch.setattr(SecurityAgent, "run", limited)
    output = run(snapshot, model_factory=factory({}, findings=True))
    assert output.status == "partial" and len(output.validated_findings) == 3
    assert note in output.limitations


def test_independent_specialist_cancellation_is_isolated(snapshot, monkeypatch):
    from northstar_codereview.agents.security import SecurityAgent
    async def cancelled(self, **kwargs):
        raise asyncio.CancelledError
    monkeypatch.setattr(SecurityAgent, "run", cancelled)
    output = run(snapshot, model_factory=factory({}, findings=True))
    assert output.status == "partial" and len(output.validated_findings) == 2
    assert [failure.category for failure in output.failures] == ["exception"]


def test_caller_cancellation_propagates_and_stops_children(snapshot):
    async def scenario():
        entered = asyncio.Event()
        stopped = []
        async def wait(model, messages):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(model.name)
        orchestrator = ReviewOrchestrator(snapshot, model_factory=factory({}, on_request=wait), approved_synthetic=True)
        task = asyncio.create_task(orchestrator.run(review_id=uuid4()))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped
    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["failed", "skipped", "partial"])
def test_explicit_incomplete_status_never_upgrades_review(snapshot, monkeypatch, status):
    from northstar_codereview.agents.security import SecurityAgent
    original = SecurityAgent.run
    async def incomplete(self, **kwargs):
        output = await original(self, **kwargs)
        return output.model_copy(update={"result": output.result.model_copy(update={"status": SpecialistStatus(status), "limitations": ("Captured context is insufficient.",)})})
    monkeypatch.setattr(SecurityAgent, "run", incomplete)
    output = run(snapshot)
    assert output.status == "partial" and result_for(output, AgentName.SECURITY).result.status == status
    assert [failure.category for failure in output.failures] == (["specialist_failed"] if status == "failed" else [])


def test_invalid_budget_counters_are_rejected(snapshot, monkeypatch):
    from northstar_codereview.agents.security import SecurityAgent
    original = SecurityAgent.run
    async def invalid(self, **kwargs):
        output = await original(self, **kwargs)
        return output.model_copy(update={"usage": output.usage.model_copy(update={"model_calls": 99})})
    monkeypatch.setattr(SecurityAgent, "run", invalid)
    output = run(snapshot)
    assert output.status == "partial" and [f.category for f in output.failures] == ["invalid_output"]
