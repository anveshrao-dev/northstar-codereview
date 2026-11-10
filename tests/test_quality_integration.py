"""Boundary regressions and the installed-provider-to-graph integration."""

import asyncio
import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import httpx
from langchain_core.messages import AIMessage

from northstar_codereview.agents.code_quality import CodeQualityAgent
from northstar_codereview.evidence_tools import get_evidence
from northstar_codereview.local_source import collect_local_snapshot
from northstar_codereview.models import ReviewRequest
from northstar_codereview.providers.nebius import NebiusModel
from .test_code_quality import ScriptedModel, call, run, READ, proposal
from .test_nebius import completion, settings


def capture(tmp_path, text="def refund(amount):\n    # full amount required\n    return amount - 1\n"):
    (tmp_path / "refund.py").write_text(text)
    return collect_local_snapshot(ReviewRequest(mode="local", local_path="."), source_root=tmp_path)


def test_guessed_exact_source_still_requires_actual_observation(tmp_path):
    snapshot = capture(tmp_path)
    ref = get_evidence(snapshot, **READ).evidence[0].model_dump(mode="json")
    item = dict(title="Guess", category="correctness", severity="medium", confidence=0.5,
                explanation="Potential error", suggested_remediation="Inspect it", evidence=[ref])
    result = run(snapshot, ScriptedModel(AIMessage(content=json.dumps({"findings": [item]}))))
    assert not result.validated_findings and result.result.status == "partial"


def test_partial_file_inspection_and_empty_file_metadata(tmp_path):
    snapshot = capture(tmp_path)
    result = run(snapshot, ScriptedModel(call(args={**READ, "end_line": 1}), AIMessage(content='{"findings": []}')))
    assert result.result.status == "partial" and result.result.reviewed_files == ("refund.py",)
    snapshot = capture(tmp_path, "")
    result = run(snapshot, ScriptedModel(call("get_source_metadata", {"file_path": "refund.py"}), AIMessage(content='{"findings": []}')))
    assert result.result.status == "completed"


def test_valid_findings_survive_rejected_sibling_without_authority_upgrade(tmp_path):
    snapshot = capture(tmp_path)
    def final(messages):
        item = proposal(messages)
        return AIMessage(content=json.dumps({"findings": [item, {**item, "category": "security"}]}))
    result = run(snapshot, ScriptedModel(call(args=READ), final))
    assert result.result.status == "partial"
    assert len(result.validated_findings) == 1
    assert result.validated_findings[0].claim_status == "unverified"
    assert any("Proposal 2 rejected" in text for text in result.result.limitations)


def test_usage_metadata_is_separate_from_reservations(tmp_path):
    snapshot = capture(tmp_path)
    usage = dict(input_tokens=100, output_tokens=10, total_tokens=110)
    first = AIMessage(content="", tool_calls=[dict(name="read_source_lines", args=READ, id="c1")], usage_metadata=usage)
    last = AIMessage(content='{"findings": []}', usage_metadata=usage)
    result = run(snapshot, ScriptedModel(first, last))
    assert result.usage.reported_input_tokens == 200 and result.usage.reported_output_tokens == 20
    assert result.usage.usage_unknown_calls == 0
    assert result.usage.output_tokens_reserved == 2000


def test_fake_demo_matches_predeclared_labels_and_preserves_fixture_bytes():
    root = Path(__file__).resolve().parents[1]
    fixture = root / "tests/fixtures/quality/refunds.py"
    before = fixture.read_bytes(), fixture.stat().st_mtime_ns
    spec = importlib.util.spec_from_file_location("offline_demo", root / "examples/review_code_quality.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = asyncio.run(module.demonstration())
    assert "SCRIPTED TEST DOUBLE" in report["mode"]
    assert report["expected_labels_defined_before_run"]["negative_control"]["expected_findings"] == 0
    result = report["run"]
    assert result["result"]["status"] == "completed"
    assert len(result["result"]["findings"]) == 1
    assert [event["tool_name"] for event in result["events"]] == ["list_snapshot_files", "get_source_metadata", "read_source_lines"]
    assert result["usage"]["model_calls"] == 4
    assert (fixture.read_bytes(), fixture.stat().st_mtime_ns) == before


def test_real_adapter_in_graph_tracing_disabled_even_if_ambient_enabled(tmp_path, monkeypatch):
    snapshot = capture(tmp_path)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "synthetic-trace-key")
    requests = []
    def respond(request):
        requests.append(request)
        body = json.loads(request.content)
        if len(requests) == 1:
            response = {"content": None, "tool_calls": [{"id": "wire-read", "type": "function",
                "function": {"name": "read_source_lines", "arguments": json.dumps(READ)}}]}
        else:
            observation = json.loads(body["messages"][-1]["content"])
            assert observation["evidence"][0]["snippet"] == snapshot.sources[("head", "refund.py")]
            response = {"content": '{"findings": []}'}
        return httpx.Response(200, json=completion(response))
    async def exercise():
        async with NebiusModel(settings(), allow_live=True, transport=httpx.MockTransport(respond)) as model:
            agent = CodeQualityAgent(snapshot, model=model, approved_synthetic=True)
            return await agent.run(review_id=uuid4())
    result = asyncio.run(exercise())
    assert result.result.status == "completed"
    assert len(requests) == 2 and result.usage.usage_unknown_calls == 0
