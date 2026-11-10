"""Deterministic recovery uses real bounded tools and fictional captured text."""

import asyncio
import json
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from northstar_codereview.agents.code_quality import CodeQualityAgent, RunLimits
from northstar_codereview.evidence_tools import read_source_lines
from northstar_codereview.local_source import CollectedSnapshot, collect_local_snapshot
from northstar_codereview.models import ReviewRequest, ToolDiagnostic
from .test_code_quality import ScriptedModel, call


@pytest.fixture
def snapshot(tmp_path):
    (tmp_path / "inventory.py").write_text("# Fictional source, never executed\n" * 11)
    return collect_local_snapshot(ReviewRequest(mode="local", local_path="."), source_root=tmp_path)


ARGS = dict(file_path="inventory.py", start_line=1, end_line=100)
EMPTY = AIMessage(content='{"findings": []}')


def run(snapshot, args=ARGS, final=EMPTY, **limits):
    model = ScriptedModel(call(args=args, ident="read"), final)
    output = asyncio.run(CodeQualityAgent(snapshot, model=model, approved_synthetic=True,
                         limits=RunLimits(**limits)).run(review_id=uuid4()))
    return output, model


def test_corrected_read_keeps_original_error_evidence_and_one_protocol_reply(snapshot):
    before = dict(snapshot.sources)
    def final(messages):
        replies = [message for message in messages if isinstance(message, ToolMessage)]
        assert len(replies) == 1 and replies[0].tool_call_id == "read"
        result = json.loads(replies[0].content)
        assert result["status"] == "ok" and result["recovery"]["outcome"] == "recovered"
        assert result["recovery"]["original_category"] == "invalid_line_range"
        item = dict(title="Synthetic inspection claim", category="correctness", severity="low", confidence=.5,
                    explanation="Scripted plumbing observation, not a verified defect.",
                    suggested_remediation="Check independently.", evidence=result["evidence"])
        return AIMessage(content=json.dumps({"findings": [item]}))
    result, model = run(snapshot, final=final)
    assert len(model.histories) == result.usage.model_calls == 2
    assert result.usage.tool_attempts == 2 and result.usage.tool_rounds == 1
    assert [event.observation.status for event in result.events] == ["error", "ok"]
    original, corrected = [event.observation for event in result.events]
    assert original.diagnostic.category == "invalid_line_range" and original.diagnostic.line_count == 11
    assert corrected.recovery.original_call_id == original.call_id
    assert result.events[1].arguments == {**ARGS, "end_line": 11, "side": "head"}
    assert len(result.validated_findings) == 1
    assert corrected.evidence[0].snippet == snapshot.sources[("head", "inventory.py")]
    assert corrected.evidence[0].snapshot_id == snapshot.manifest.snapshot_id
    assert result.result.inspection.model_dump() == dict(successful_attempts=1, recovery_attempts=1, recovered_errors=1, unresolved_errors=0,
        failures=({'category':'invalid_line_range','recovered':1,'unresolved':0},))
    assert result.result.status == "partial" and any("recovered" in note for note in result.result.limitations)
    assert not any("not inspected" in note for note in result.result.limitations)
    assert dict(snapshot.sources) == before


@pytest.mark.parametrize("args,attempts,recovered", [
    ({**ARGS, "end_line": 11}, 1, 0),
    ({**ARGS, "start_line": 7}, 2, 1),
    ({**ARGS, "start_line": 12}, 1, 0),
    ({**ARGS, "start_line": 0}, 1, 0),
    ({**ARGS, "start_line": True}, 1, 0),
    ({**ARGS, "start_line": "1"}, 1, 0),
    ({**ARGS, "start_line": 3, "end_line": 2}, 1, 0),
    ({**ARGS, "end_line": 101}, 1, 0),
    ({**ARGS, "file_path": "unknown.py"}, 1, 0),
    ({**ARGS, "file_path": "../outside.py"}, 1, 0),
    ({**ARGS, "side": "base"}, 1, 0),
])
def test_recovery_only_for_trusted_positive_overlap(snapshot, args, attempts, recovered):
    result, _ = run(snapshot, args)
    assert result.usage.tool_attempts == attempts
    assert result.result.inspection.recovered_errors == recovered
    if recovered:
        ref = result.events[-1].observation.evidence[0]
        assert ref.start_line == args["start_line"] and ref.end_line == 11
        assert ref.file_path == args["file_path"] and ref.side == "head"
    if args["start_line"] != 1 or args.get("side") == "base" or args["file_path"] != "inventory.py":
        assert result.result.status == "partial"


@pytest.mark.parametrize("limit,value,attempts", [("max_tool_attempts", 1, 1), ("max_model_calls", 1, 0), ("max_tool_rounds", 1, 2)])
def test_recovery_counts_physical_attempt_but_not_new_model_round(snapshot, limit, value, attempts):
    result, _ = run(snapshot, **{limit: value})
    assert result.usage.tool_attempts == attempts
    assert result.usage.tool_rounds == (0 if limit == "max_model_calls" else 1)
    assert result.usage.model_calls <= (value if limit == "max_model_calls" else 2)
    assert result.result.status == "partial"
    assert result.result.inspection.recovered_errors == (attempts == 2)
    assert result.result.inspection.unresolved_errors == (attempts == 1)


@pytest.mark.parametrize("corruption", ["missing", "changed", "snapshot_id", "line_count", "category", "guidance"])
def test_recovery_rejects_mismatched_snapshot_or_diagnostic(snapshot, corruption):
    from northstar_codereview.read_recovery import corrected_read_arguments
    failure = read_source_lines(snapshot, **ARGS)
    if corruption in {"missing", "changed"}:
        sources = {} if corruption == "missing" else {("head", "inventory.py"): "changed\n" * 11}
        snapshot = CollectedSnapshot(snapshot.manifest, sources)
    elif corruption == "snapshot_id":
        failure = failure.model_copy(update={"snapshot_id": uuid4()})
    else:
        changes = {"line_count": 12} if corruption == "line_count" else {"category": "internal_error"} if corruption == "category" else {"recovery": "stop"}
        failure = failure.model_copy(update={"diagnostic": ToolDiagnostic(**{**failure.diagnostic.model_dump(), **changes})})
    assert corrected_read_arguments(snapshot, ARGS, failure) is None


def test_corrected_read_keeps_output_limit_and_never_retries_it(tmp_path):
    (tmp_path / "inventory.py").write_text("x" * 15000 + "\n")
    snapshot = collect_local_snapshot(ReviewRequest(mode="local", local_path="."), source_root=tmp_path)
    result, _ = run(snapshot)
    assert result.usage.tool_attempts == 2 and len(result.events) == 2
    assert result.events[-1].observation.status == "blocked"
    assert result.events[-1].observation.diagnostic.category == "output_size_limit"
    assert result.result.inspection.recovery_attempts == 1
    assert result.result.inspection.recovered_errors == 0
    assert result.result.inspection.unresolved_errors == 2
    assert not result.validated_findings and result.result.status == "partial"


def test_recovery_does_not_expand_prefix_coverage(snapshot):
    result, _ = run(snapshot, {**ARGS, "start_line": 7})
    assert result.result.inspection.recovered_errors == 1
    assert any("not inspected" in note for note in result.result.limitations)


def test_get_evidence_is_not_silently_corrected(snapshot):
    model = ScriptedModel(call("get_evidence", ARGS), EMPTY)
    result = asyncio.run(CodeQualityAgent(snapshot, model=model, approved_synthetic=True).run(review_id=uuid4()))
    assert result.usage.tool_attempts == 1 and result.result.inspection.recovery_attempts == 0


def test_no_correction_starts_after_deadline(snapshot, monkeypatch):
    import northstar_codereview.agents.code_quality as module
    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    model = ScriptedModel(call(args=ARGS), EMPTY)
    agent = CodeQualityAgent(snapshot, model=model, approved_synthetic=True)
    tool = agent.tools["read_source_lines"]
    invoke = tool.func
    def expire(**kwargs):
        result = invoke(**kwargs)
        clock[0] = 121.0
        return result
    tool.func = expire
    result = asyncio.run(agent.run(review_id=uuid4()))
    assert result.usage.tool_attempts == 1 and result.result.inspection.recovery_attempts == 0
    assert result.result.inspection.unresolved_errors == 1
    assert any("timeout" in note for note in result.result.limitations)


def test_original_line_limit_cannot_be_bypassed_with_shortened_result(snapshot):
    result, _ = run(snapshot, {**ARGS, "end_line": 101})
    assert result.events[0].observation.diagnostic.category == "line_count_limit"
    assert result.usage.tool_attempts == 1 and result.result.inspection.recovered_errors == 0


def test_failed_correction_is_not_retried_again(snapshot):
    agent = CodeQualityAgent(snapshot, model=ScriptedModel(call(args=ARGS), EMPTY), approved_synthetic=True)
    tool = agent.tools["read_source_lines"]
    original = tool.func
    attempts = []
    def stubborn(**kwargs):
        attempts.append(kwargs)
        return original(**{**kwargs, "end_line": 100})
    tool.func = stubborn
    result = asyncio.run(agent.run(review_id=uuid4()))
    assert len(attempts) == result.usage.tool_attempts == 2
    assert result.events[-1].observation.recovery.outcome == "unresolved"
    assert result.result.inspection.recovery_attempts == 1
    assert result.result.inspection.unresolved_errors == 2 and result.result.inspection.recovered_errors == 0


def test_recovery_preserves_base_side_identity(tmp_path):
    from northstar_codereview.local_source import _assemble
    snapshot = _assemble({("base", "inventory.py"): "# old fictional line\n", ("head", "inventory.py"): "# new fictional line\n"})
    result, _ = run(snapshot, {**ARGS, "side": "base"})
    assert result.events[-1].observation.evidence[0].side == "base"
    assert result.events[-1].observation.evidence[0].snippet == "# old fictional line\n"
    assert result.result.status == "partial"  # HEAD remains uninspected.


def test_five_parallel_specialists_isolate_recovery_and_report_counts(monkeypatch):
    from pathlib import Path
    from northstar_codereview.models import AgentName
    from northstar_codereview.orchestrator import ReviewOrchestrator
    from northstar_codereview.reporting import build_report, render_markdown
    from .test_platform_integration import CATEGORIES
    root = Path(__file__).parent / "fixtures/integrated_review"
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    snapshot = collect_local_snapshot(ReviewRequest(mode="local", local_path="."), source_root=root)
    async def scenario():
        arrived, ready = set(), asyncio.Event()
        class Model:
            def __init__(self, name):
                self.name, self.calls = name, 0
            def bind_tools(self, tools):
                return self
            async def ainvoke(self, messages, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    arrived.add(self.name)
                    if len(arrived) == 5:
                        ready.set()
                    await asyncio.wait_for(ready.wait(), 2)
                    calls = [dict(name="read_source_lines", id=f"read-{i}", args=dict(file_path=path, start_line=1,
                             end_line=100 if i == 0 and self.name == AgentName.CODE_QUALITY else len(text.splitlines())))
                             for i, ((side, path), text) in enumerate(sorted(snapshot.sources.items()))]
                    return AIMessage(content="", tool_calls=calls)
                observation = json.loads(messages[-1].content)
                item = dict(title="Synthetic bounded review", category=CATEGORIES[self.name], severity="low", confidence=.5,
                            explanation="Source observation only.", suggested_remediation="Inspect independently.", evidence=observation["evidence"])
                return AIMessage(content=json.dumps({"findings": [item]}))
        output = await ReviewOrchestrator(snapshot, model_factory=Model, approved_synthetic=True).run(review_id=uuid4())
        assert arrived == set(AgentName) and output.status == "partial"
        assert len(output.validated_findings) == 5 and not output.failures
        report = build_report(output, snapshot=snapshot.manifest)
        for summary in report.specialists:
            recovered = summary.agent_name == AgentName.CODE_QUALITY
            assert summary.inspection.recovered_errors == int(recovered)
            assert summary.inspection.unresolved_errors == 0
            assert summary.usage.tool_attempts == (8 if recovered else 7)
            assert summary.usage.model_calls == 2
            assert summary.status == ("partial" if recovered else "completed")
        assert "1 recovered errors; 0 unresolved errors" in render_markdown(report)
    asyncio.run(scenario())
    assert all(path.read_bytes() == text for path, text in before.items())
