"""Live entry point exercised only with fictional data and in-memory transport."""

import asyncio
import builtins
import json
import logging
import sys
from pathlib import Path

import httpx
import pytest

from northstar_codereview import live_quality as live
from northstar_codereview.local_source import CollectedSnapshot
from .test_capability_check import NoEnvironmentReads
from .test_nebius import completion


ENV = {"NEBIUS_API_KEY": "synthetic-test-secret", "NEBIUS_MODEL": "Qwen/Qwen3-30B-A3B-Instruct-2507"}
PRIVATE = "PRIVATE_MODEL_PROSE_DO_NOT_EMIT"


def run(handler, **kwargs):
    return asyncio.run(live.run_evaluation(allow_live=True, environ=ENV,
                                          transport=httpx.MockTransport(handler), **kwargs))


def tool(name="read_source_lines", identity="read-1", **arguments):
    return {"content": None, "reasoning": None, "tool_calls": [{"id": identity, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}]}


@pytest.mark.parametrize("flag", [False, None, 1, "true"])
def test_disabled_runner_never_loads_configuration_fixture_or_provider(flag, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Disabled runner crossed its authorization boundary")
    monkeypatch.setattr(live, "load_fixture", forbidden)
    monkeypatch.setattr(live, "NebiusModel", forbidden)
    report = asyncio.run(live.run_evaluation(allow_live=flag, environ=NoEnvironmentReads()))
    assert report.status == "NOT RUN" and not report.started and report.usage is None


@pytest.mark.parametrize("config", [{}, {**ENV, "NEBIUS_MODEL": "different-model"},
    {**ENV, "NEBIUS_BASE_URL": "https://example.invalid/"}])
def test_missing_or_wrong_configuration_stops_without_provider(config, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid configuration started a provider")
    monkeypatch.setattr(live, "NebiusModel", forbidden)
    report = asyncio.run(live.run_evaluation(allow_live=True, environ=config))
    assert report.status == "NOT RUN" and report.reason == "configuration_error"
    assert not report.started and report.usage is None
    assert "synthetic-test-secret" not in report.model_dump_json()


def test_wrong_working_directory_stops_before_configuration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    report = asyncio.run(live.run_evaluation(allow_live=True, environ=NoEnvironmentReads()))
    assert report.reason == "configuration_error" and not report.started


@pytest.mark.parametrize("attribute,value", [("version_info", (3, 11, 0)), ("prefix", "/not-the-project-venv")])
def test_wrong_python_environment_stops_before_configuration(attribute, value, monkeypatch):
    monkeypatch.setattr(live.sys, attribute, value)
    report = asyncio.run(live.run_evaluation(allow_live=True, environ=NoEnvironmentReads()))
    assert report.reason == "configuration_error" and not report.started


def test_symlinked_fixture_rejected_without_reading_target(tmp_path, monkeypatch):
    folder = tmp_path / "tests/fixtures/quality_inventory"
    folder.mkdir(parents=True)
    (folder / "inventory.py").symlink_to(live.PROJECT_ROOT / "tests/fixtures/quality_inventory/inventory.py")
    monkeypatch.setattr(live, "PROJECT_ROOT", tmp_path)
    with pytest.raises(ValueError):
        live.load_fixture()


def test_missing_environment_does_not_autoload_dotenv(monkeypatch):
    original = Path.read_text

    def no_dotenv(path, *args, **kwargs):
        assert not path.name.startswith(".env"), "Runner must not read dotenv files"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", no_dotenv)
    report = asyncio.run(live.run_evaluation(allow_live=True, environ={}))
    assert report.reason == "configuration_error" and not report.started


@pytest.mark.parametrize("target", ["source", "labels"])
def test_altered_fixture_or_labels_fail_before_configuration(target, monkeypatch):
    original = live.collect_local_snapshot

    def changed(*args, **kwargs):
        bundle = original(*args, **kwargs)
        sources = dict(bundle.sources)
        for key in sources:
            if (key[1].endswith(".json")) == (target == "labels"):
                sources[key] += " "
        return CollectedSnapshot(bundle.manifest, sources)

    monkeypatch.setattr(live, "collect_local_snapshot", changed)
    report = asyncio.run(live.run_evaluation(allow_live=True, environ=NoEnvironmentReads()))
    assert report.status == "NOT RUN" and report.reason == "fixture_rejected"
    assert not report.started


def test_fixture_is_fixed_independent_and_labels_are_separate():
    snapshot, labels = live.load_fixture()
    assert tuple(path for _, path in snapshot.sources) == ("inventory.py",)
    assert labels["expected_findings"][0]["function"] == "can_fulfill_order"
    assert labels["expected_findings"][0]["evidence_lines"] == [4, 6]
    assert labels["expected_findings"][0]["example"] == {
        "available": 5, "requested": 5, "expected": True, "actual_by_source_inspection": False}
    assert labels["negative_control"]["function"] == "stock_after_fulfillment"
    assert labels["negative_control"]["evidence_lines"] == [9, 11]
    assert labels["negative_control"]["expected_findings"] == 0
    source = next(iter(snapshot.sources.values())).splitlines()
    assert source[3].startswith("def can_fulfill_order(")
    assert source[8].startswith("def stock_after_fulfillment(")


def test_real_graph_and_sdk_complete_without_executing_modifying_or_leaking_source(monkeypatch, caplog):
    source = live.PROJECT_ROOT / "tests/fixtures/quality_inventory/inventory.py"
    labels_path = live.PROJECT_ROOT / "tests/fixtures/quality_inventory_expected.json"
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (source, labels_path)]
    compile_attempts = []
    original_compile = builtins.compile

    def guarded_compile(code, filename, *args, **kwargs):
        if str(filename) == str(source) or (isinstance(code, (str, bytes)) and
                "def can_fulfill_order(" in (code.decode() if isinstance(code, bytes) else code)):
            compile_attempts.append(True)
            raise AssertionError("Reviewed source must never execute")
        return original_compile(code, filename, *args, **kwargs)

    monkeypatch.setattr(builtins, "compile", guarded_compile)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert str(request.url) == "https://api.tokenfactory.nebius.com/v1/chat/completions"
        assert body["model"] == ENV["NEBIUS_MODEL"] and body["max_completion_tokens"] == 1000
        # Predefined answer labels are never included in prompts, schemas, or tools.
        assert "actual_by_source_inspection" not in request.content.decode()
        assert "An order equal to available stock is incorrectly rejected" not in request.content.decode()
        if len(requests) == 1:
            assert "can_fulfill_order" not in json.dumps(body["messages"])
            message = tool("list_snapshot_files", "discover")
        elif len(requests) == 2:
            message = tool(file_path="inventory.py", start_line=1, end_line=11)
        elif len(requests) == 3:
            message = tool("get_evidence", "cite", file_path="inventory.py", start_line=4, end_line=6)
        else:
            assert len(requests) == 4
            observation = json.loads(body["messages"][-1]["content"])
            item = dict(title=PRIVATE, category="correctness", severity="medium", confidence=0.9,
                        explanation=PRIVATE, suggested_remediation=PRIVATE, evidence=observation["evidence"])
            message = {"content": json.dumps({"findings": [item]}), "reasoning": None}
        return httpx.Response(200, json=completion(message))

    with caplog.at_level(logging.DEBUG):
        report = run(respond)
    assert report.status == "COMPLETED" and report.specialist_status == "completed"
    assert report.usage.model_calls == len(requests) == 4 and report.usage.tool_attempts == 3
    assert report.usage.retries == report.usage.repairs == report.usage.usage_unknown_calls == 0
    assert report.usage.reported_input_tokens == 400 and report.usage.reported_output_tokens == 80
    assert [event.tool_name for event in report.tools] == ["list_snapshot_files", "read_source_lines", "get_evidence"]
    assert report.findings[0].evidence_status == "validated" and report.findings[0].claim_status == "unverified"
    assert report.expected_region_citations == 1 and report.control_region_citations == 0
    assert report.elapsed_seconds >= 0 and not compile_attempts
    assert before == [(p.read_bytes(), p.stat().st_mtime_ns) for p in (source, labels_path)]
    for output in (report.model_dump_json(), caplog.text):
        for private in (PRIVATE, "synthetic-test-secret", "return requested < available", "snippet", "arguments"):
            assert private not in output


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
def test_failed_model_attempts_are_counted_without_retry_or_raw_error(status, caplog):
    attempts = []

    def respond(request):
        attempts.append(True)
        return httpx.Response(status, json={"error": {"message": PRIVATE + " synthetic-test-secret"}})

    with caplog.at_level(logging.DEBUG):
        report = run(respond)
    assert report.status == "PARTIAL" and report.specialist_status == "partial"
    assert report.usage.model_calls == len(attempts) == 1
    assert report.usage.retries == report.usage.tool_attempts == 0
    assert report.usage.usage_unknown_calls == 1
    assert "provider_failure" in report.limitations
    for output in (report.model_dump_json(), caplog.text):
        assert PRIVATE not in output and "synthetic-test-secret" not in output


@pytest.mark.parametrize("limit,settings,want_calls,want_tools", [
    ("model_call_budget", {"max_model_calls": 1}, 1, 0),
    ("tool_round_budget", {"max_tool_rounds": 1}, 2, 1),
    ("tool_attempt_budget", {"max_tool_attempts": 1}, 2, 1),
    ("token_budget", {"max_input_tokens": 1}, 0, 0),
    ("token_budget", {"max_output_tokens": 1000}, 1, 1),
])
def test_lowered_budgets_stop_real_graph(limit, settings, want_calls, want_tools):
    attempts = []

    def respond(request):
        attempts.append(True)
        message = tool(identity=f"call-{len(attempts)}", file_path="inventory.py", start_line=1, end_line=11)
        if limit == "tool_attempt_budget":
            message["tool_calls"].extend(tool(identity="extra", file_path="inventory.py", start_line=1, end_line=11)["tool_calls"])
        return httpx.Response(200, json=completion(message))

    report = run(respond, limits=live.EvaluationLimits(**settings))
    assert report.status == "PARTIAL" and limit in report.limitations
    assert report.usage.model_calls == len(attempts) == want_calls
    assert report.usage.tool_attempts == want_tools


@pytest.mark.parametrize("field,value", [("max_model_calls", 5), ("max_tool_rounds", 4),
    ("max_tool_attempts", 7), ("request_timeout_seconds", 31), ("run_timeout_seconds", 91),
    ("max_input_tokens", 100001), ("max_output_tokens", 4001), ("output_tokens_per_call", 1001),
    ("max_provider_retries", 1), ("max_output_repairs", 1)])
def test_live_limits_cannot_be_raised(field, value):
    with pytest.raises(ValueError):
        live.EvaluationLimits(**{field: value})


@pytest.mark.parametrize("field", ["request_timeout_seconds", "run_timeout_seconds"])
def test_timeout_has_no_retry(field):
    attempts = []

    async def respond(request):
        attempts.append(True)
        await asyncio.sleep(1)
        return httpx.Response(200, json=completion({"content": '{"findings": []}'}))

    report = run(respond, limits=live.EvaluationLimits(**{field: 0.25}))
    assert "timeout" in report.limitations and len(attempts) == 1
    if field == "run_timeout_seconds":
        assert report.status == "FAIL" and report.reason == "run_timeout"
        assert report.started and report.usage is None  # Cancelled graph has no complete usage record.
    else:
        assert report.status == "PARTIAL"
        assert report.usage.model_calls == 1 and report.usage.retries == 0


@pytest.mark.parametrize("hang_request", [False, True])
def test_run_deadline_also_bounds_provider_cleanup(hang_request, monkeypatch):
    original_exit = live.NebiusModel.__aexit__
    attempts = []

    async def slow_close(model, *args):
        await original_exit(model, *args)
        await asyncio.sleep(1)

    async def respond(request):
        attempts.append(True)
        if hang_request:
            await asyncio.sleep(1)
        return httpx.Response(200, json=completion({"content": '{"findings": []}'}))

    monkeypatch.setattr(live.NebiusModel, "__aexit__", slow_close)
    report = run(respond, limits=live.EvaluationLimits(run_timeout_seconds=0.25))
    assert report.status == "FAIL" and report.reason == "run_timeout"
    assert report.started and report.elapsed_seconds < 0.75 and len(attempts) == 1
    if hang_request:
        assert report.usage is None
    else:
        assert report.usage.model_calls == 1 and report.usage.retries == 0


def test_malformed_final_is_not_repaired():
    report = run(lambda request: httpx.Response(200, json=completion({"content": PRIVATE})))
    assert report.usage.model_calls == 1 and report.usage.repairs == 0
    assert "malformed_final" in report.limitations and PRIVATE not in report.model_dump_json()


def test_unexpected_startup_error_is_sanitized(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError(PRIVATE + " synthetic-test-secret")
    monkeypatch.setattr(live, "NebiusModel", broken)
    report = run(lambda request: pytest.fail("Unexpected error should stop before HTTP"))
    assert report.status == "FAIL" and report.reason == "unexpected_error"
    assert not report.started and report.usage is None
    assert PRIVATE not in report.model_dump_json() and "synthetic-test-secret" not in report.model_dump_json()


def test_proposal_rejection_does_not_echo_model_prose():
    attempts = []

    def respond(request):
        attempts.append(True)
        message = (tool(file_path="inventory.py", start_line=1, end_line=11) if len(attempts) == 1 else
                   {"content": json.dumps({"findings": [{"title": PRIVATE}]})})
        return httpx.Response(200, json=completion(message))

    report = run(respond)
    assert report.status == "PARTIAL" and "proposal_rejected" in report.limitations
    assert report.usage.model_calls == len(attempts) == 2 and not report.findings
    assert PRIVATE not in report.model_dump_json()


def test_cli_without_optin_is_safe(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["review_code_quality_live.py"])
    live.main()
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "NOT RUN" and report["reason"] == "disabled"


@pytest.mark.parametrize("args", [["--path", "/private/" + PRIVATE], ["--model", PRIVATE],
                                  ["--max-model-calls", PRIVATE], ["--max-model-calls", "5"]])
def test_invalid_cli_cannot_echo_arguments_or_enable_provider(args, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["review_code_quality_live.py", *args])
    with pytest.raises(SystemExit) as caught:
        live.main()
    assert caught.value.code == 2
    output = capsys.readouterr()
    assert PRIVATE not in output.out + output.err
