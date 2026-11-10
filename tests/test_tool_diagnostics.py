"""Safe inspection diagnostics and bounded recovery on the pinned inventory fixture."""

import asyncio
import json
import logging
from uuid import uuid4

import httpx
import pytest

from northstar_codereview import evidence_tools as tools
from northstar_codereview import live_quality as live
from northstar_codereview.agents.code_quality import CodeQualityAgent
from northstar_codereview.local_source import CollectedSnapshot, collect_local_snapshot
from northstar_codereview.models import ReviewRequest, ToolResult
from .test_code_quality import ScriptedModel, call
from .test_nebius import completion
from .test_live_quality import ENV, tool as wire_tool


PRIVATE = "fictional-private-argument-and-source"


@pytest.fixture
def inventory():
    return live.load_fixture()[0]


def read(snapshot, **changes):
    return tools.read_source_lines(snapshot, **{"file_path": "inventory.py", "start_line": 1, "end_line": 11, **changes})


@pytest.mark.parametrize("changes", [{"category": PRIVATE}, {"recovery": PRIVATE}, {"line_count": True},
                                     {"line_count": -1}, {"raw_arguments": PRIVATE}])
def test_diagnostic_contract_rejects_free_text_and_untrusted_shape(changes):
    from northstar_codereview.models import ToolDiagnostic
    with pytest.raises(ValueError):
        ToolDiagnostic(**{"category": "invalid_line_range", "recovery": "use_captured_line_bounds", **changes})


def test_success_cannot_carry_a_failure_diagnostic(inventory):
    from northstar_codereview.models import ToolDiagnostic
    with pytest.raises(ValueError):
        ToolResult(call_id=uuid4(), snapshot_id=inventory.manifest.snapshot_id, tool_name="read_source_lines",
                   status="ok", data={"safe": True},
                   diagnostic=ToolDiagnostic(category="internal_error", recovery="stop"))


def test_valid_read_keeps_exact_evidence_and_no_diagnostic(inventory):
    result = read(inventory)
    assert result.status == "ok" and result.diagnostic is None
    assert result.data.text == inventory.sources[("head", "inventory.py")]
    assert result.evidence[0].snippet == result.data.text


@pytest.mark.parametrize("function", [tools.read_source_lines, tools.get_evidence])
def test_out_of_file_read_exposes_trusted_bounds_without_source_or_raw_arguments(inventory, function):
    result = function(inventory, file_path="inventory.py", start_line=1, end_line=100)
    assert result.status == "error" and result.data is None and not result.evidence
    assert result.diagnostic.model_dump() == {
        "category": "invalid_line_range", "recovery": "use_captured_line_bounds", "line_count": 11}
    assert "line_count" in result.error
    assert "return requested" not in result.model_dump_json()
    assert "start_line" not in result.model_dump_json() and "end_line" not in result.model_dump_json()


@pytest.mark.parametrize("arguments,status,category,recovery", [
    ({"file_path": PRIVATE + ".py", "start_line": 1, "end_line": 1}, "no_match", "unknown_file", "list_snapshot_files"),
    ({"file_path": "/private/" + PRIVATE, "start_line": 1, "end_line": 1}, "blocked", "invalid_arguments", "check_input_schema"),
    ({"file_path": "inventory.py", "start_line": True, "end_line": 1}, "error", "invalid_arguments", "check_input_schema"),
    ({"file_path": "inventory.py", "start_line": "1", "end_line": 1}, "error", "invalid_arguments", "check_input_schema"),
    ({"file_path": "inventory.py", "start_line": 2, "end_line": 1}, "error", "invalid_arguments", "check_input_schema"),
    ({"file_path": "inventory.py", "start_line": 1, "end_line": 101}, "blocked", "line_count_limit", "reduce_request"),
    ({"file_path": "inventory.py", "start_line": 1, "end_line": 1, "side": "base"}, "unavailable", "source_side_unavailable", "stop"),
])
def test_distinct_existing_failure_paths_keep_status_and_no_input_echo(inventory, arguments, status, category, recovery):
    result = tools.read_source_lines(inventory, **arguments)
    assert result.status == status and result.diagnostic.category == category
    assert result.diagnostic.recovery == recovery and result.diagnostic.line_count is None
    assert result.data is None and not result.evidence
    assert PRIVATE not in result.model_dump_json() and "/private/" not in result.model_dump_json()


def test_manifest_without_content_is_a_snapshot_mismatch(inventory):
    broken = CollectedSnapshot(inventory.manifest, {})
    result = read(broken)
    assert result.status == "error" and result.diagnostic.category == "snapshot_mismatch"
    assert result.diagnostic.recovery == "stop" and result.data is None


@pytest.mark.parametrize("arguments", [
    {"file_path": "inventory.py", "start_line": True, "end_line": 1},
    {"file_path": "inventory.py", "end_line": 1},
    {"file_path": "inventory.py", "start_line": 1, "end_line": 1, PRIVATE: PRIVATE},
    {"file_path": "/private/" + PRIVATE, "start_line": 1, "end_line": 1},
])
def test_langchain_wrapper_validation_is_distinguished_and_sanitized(inventory, arguments, caplog):
    wrapper = tools.build_snapshot_tools(inventory, approved_synthetic=True)[1]
    with caplog.at_level(logging.DEBUG):
        message = wrapper.invoke({"name": "read_source_lines", "id": "bad-read", "type": "tool_call", "args": arguments})
    result = ToolResult.model_validate_json(message.content)
    assert result.status in {"blocked", "error"}
    assert result.diagnostic.category == "tool_wrapper_validation_failure"
    assert result.diagnostic.recovery == "check_input_schema"
    assert message.tool_call_id == "bad-read"
    assert PRIVATE not in message.content + caplog.text and "/private/" not in message.content + caplog.text


def test_unexpected_tool_exception_is_classified_without_message_or_traceback(inventory, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError(PRIVATE + " synthetic-test-secret")
    monkeypatch.setattr(tools, "_lookup", broken)
    wrapper = tools.build_snapshot_tools(inventory, approved_synthetic=True)[1]
    with caplog.at_level(logging.DEBUG):
        text = wrapper.invoke({"file_path": "inventory.py", "start_line": 1, "end_line": 1})
    result = ToolResult.model_validate_json(text)
    assert result.status == "error" and result.diagnostic.category == "internal_error"
    assert result.diagnostic.recovery == "stop"
    for output in (text, caplog.text):
        assert PRIVATE not in output and "synthetic-test-secret" not in output and "Traceback" not in output


@pytest.mark.parametrize("size", [9000, 17000])
def test_both_slice_and_envelope_output_limits_remain_enforced(tmp_path, size):
    (tmp_path / "large.py").write_text("x" * size)
    snapshot = collect_local_snapshot(ReviewRequest(mode="local", local_path="."), source_root=tmp_path)
    result = tools.read_source_lines(snapshot, file_path="large.py", start_line=1, end_line=1)
    assert result.status == "blocked" and result.diagnostic.category == "output_size_limit"
    assert result.diagnostic.recovery == "reduce_request"
    assert result.data is None and not result.evidence
    assert len(result.model_dump_json().encode()) <= tools.MAX_OUTPUT_BYTES
    assert "x" * 100 not in result.model_dump_json()


def test_empty_file_has_safe_metadata_recovery(tmp_path):
    (tmp_path / "empty.py").write_text("")
    snapshot = collect_local_snapshot(ReviewRequest(mode="local", local_path="."), source_root=tmp_path)
    result = tools.read_source_lines(snapshot, file_path="empty.py", start_line=1, end_line=1)
    assert result.status == "no_match" and result.error is None
    assert result.diagnostic.category == "empty_file" and result.diagnostic.line_count == 0
    assert result.diagnostic.recovery == "get_source_metadata"


def recovering_model(*, forged=False):
    def choose_read(messages):
        page = json.loads(messages[-1].content)
        return call(args={"file_path": page["data"]["files"][0]["file_path"], "start_line": 1, "end_line": 100}, ident="bad-read")

    def correct_read(messages):
        result = json.loads(messages[-1].content)
        if result["recovery"] is not None:
            assert result["status"] == "ok" and result["recovery"]["original_category"] == "invalid_line_range"
            count = result["recovery"]["line_count"]
        else:
            assert result["status"] == "error" and result["diagnostic"]["category"] == "invalid_line_range"
            count = result["diagnostic"]["line_count"]
        return call(args={"file_path": "inventory.py", "start_line": 1,
                          "end_line": count}, ident="corrected-read")

    def final(messages):
        from langchain_core.messages import AIMessage
        observed = json.loads(messages[-1].content)
        assert observed["status"] == "ok"
        evidence = observed["evidence"]
        if forged:
            evidence[0]["snippet"] = "fabricated source\n"
        item = dict(title="Exact-stock orders are rejected", category="correctness", severity="medium", confidence=0.8,
                    explanation="For equal positive stock and request quantities, the strict comparison rejects the order.",
                    suggested_remediation="Include equality in the stock comparison.", evidence=evidence)
        return AIMessage(content=json.dumps({"findings": [item]}))

    return ScriptedModel(call("list_snapshot_files", ident="list"), choose_read, correct_read, final)


@pytest.mark.parametrize("forged", [False, True])
def test_model_corrects_read_within_existing_budgets_and_cannot_bypass_evidence_validation(inventory, forged):
    model = recovering_model(forged=forged)
    agent = CodeQualityAgent(inventory, model=model, approved_synthetic=True, limits=live.EvaluationLimits())
    result = asyncio.run(agent.run(review_id=uuid4()))
    assert result.usage.model_calls == len(model.histories) == 4
    assert result.usage.tool_attempts == 4 and result.usage.tool_rounds == 3
    assert result.usage.retries == result.usage.repairs == 0
    assert [event.observation.status for event in result.events] == ["ok", "error", "ok", "ok"]
    assert result.result.inspection.recovered_errors == 1 and result.result.inspection.unresolved_errors == 0
    assert result.events[1].observation.diagnostic.line_count == 11
    assert result.result.status == "partial"  # Existing error history is intentionally retained.
    assert not any("not inspected" in item for item in result.result.limitations)
    assert len(result.validated_findings) == (0 if forged else 1)
    if not forged:
        assert result.validated_findings[0].claim_status == "unverified"
    else:
        assert any("Proposal 1 rejected" in item for item in result.result.limitations)


@pytest.mark.parametrize("limits,calls", [({"max_model_calls": 2}, 2), ({"max_tool_rounds": 2}, 3), ({"max_tool_attempts": 2}, 3)])
def test_failed_reads_consume_budgets_and_recovery_cannot_overrun_them(inventory, limits, calls):
    model = recovering_model()
    agent = CodeQualityAgent(inventory, model=model, approved_synthetic=True, limits=live.EvaluationLimits(**limits))
    result = asyncio.run(agent.run(review_id=uuid4()))
    assert result.usage.model_calls == len(model.histories) == calls
    assert result.usage.tool_attempts == (1 if "max_model_calls" in limits else 2 if "max_tool_attempts" in limits else 3) and not result.validated_findings
    assert result.result.status == "partial"
    if "max_model_calls" not in limits:
        assert result.events[1].observation.diagnostic.category == "invalid_line_range"


def test_old_live_sequence_reproduces_offline_with_safe_category_and_unchanged_limits(caplog):
    attempts = []

    def respond(request):
        attempts.append(True)
        if len(attempts) == 1:
            message = wire_tool("list_snapshot_files", "list")
        elif len(attempts) == 2:
            message = wire_tool(file_path="inventory.py", start_line=1, end_line=100)
        elif len(attempts) == 3:
            observation = json.loads(request.content)["messages"][-1]
            body = json.loads(observation["content"])
            assert body["status"] == "ok" and body["recovery"]["original_category"] == "invalid_line_range"
            message = wire_tool("get_source_metadata", "metadata", file_path="inventory.py")
        else:
            assert len(attempts) == 4
            message = wire_tool(identity="late-read", file_path="inventory.py", start_line=1, end_line=11)
        return httpx.Response(200, json=completion(message))

    with caplog.at_level(logging.DEBUG):
        report = asyncio.run(live.run_evaluation(allow_live=True, environ=ENV, transport=httpx.MockTransport(respond)))
    assert report.status == "PARTIAL" and report.usage.model_calls == len(attempts) == 4
    assert report.usage.tool_attempts == 4 and not report.findings
    assert [event.tool_name for event in report.tools] == ["list_snapshot_files", "read_source_lines", "read_source_lines", "get_source_metadata"]
    assert [event.status for event in report.tools] == ["ok", "error", "ok", "ok"]
    assert report.tools[1].diagnostic.model_dump() == {
        "category": "invalid_line_range", "recovery": "use_captured_line_bounds", "line_count": 11}
    assert set(report.limitations) == {"inspection_recovered", "tool_round_budget", "invalid_tool_protocol"}
    assert report.inspection.recovered_errors == 1 and report.inspection.unresolved_errors == 0
    for output in (report.model_dump_json(), caplog.text):
        assert "return requested" not in output and "synthetic-test-secret" not in output
        # Closed action label clamp_end_line is safe; raw argument keys are not.
        assert '"start_line":' not in output and '"end_line":' not in output and '"arguments":' not in output
