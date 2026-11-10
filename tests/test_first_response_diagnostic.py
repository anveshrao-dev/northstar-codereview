"""Single-response mode uses the real adapter but only an offline transport."""

import asyncio
import json
import logging
import sys

import httpx
import pytest
from langchain_core.tools import StructuredTool

from northstar_codereview import capability_check as probe
from .test_capability_check import NoEnvironmentReads
from .test_nebius import completion
from .test_provider_diagnostics import ENV, tool_message


PRIVATE = "fictional-private-response-and-arguments"


@pytest.fixture
def no_tools(monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise RuntimeError("Tool execution forbidden in first-response mode")

    for method in ("invoke", "ainvoke", "run", "arun"):
        monkeypatch.setattr(StructuredTool, method, forbidden)
    yield
    assert not attempts, "A tool was invoked, even if its error was caught"


def run(handler, **kwargs):
    return asyncio.run(probe.run_capability_check(allow_live=True, diagnose_first_response=True,
        environ=ENV, transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.parametrize("kind", ["tool", "text", "unvalidated_arguments", "missing_usage"])
def test_first_success_stops_before_tools_validation_or_second_request(kind, no_tools, caplog):
    attempts = []

    def respond(request):
        attempts.append(True)
        arguments = ('{"file_path":"probe.py","start_line":1,"end_line":1}'
                     if kind != "unvalidated_arguments" else json.dumps({"file_path": PRIVATE}))
        message = {"content": PRIVATE} if kind == "text" else tool_message(arguments)
        return httpx.Response(200, json=completion(message, usage=kind != "missing_usage"))

    with caplog.at_level(logging.DEBUG):
        report = run(respond)
    assert report.mode == "first_response" and report.status == "PASS"
    assert report.model_calls == len(attempts) == 1 and report.tool_calls == 0
    assert report.maximum_output_tokens == 128 and report.diagnostic is None
    assert report.checks["model_available"] is True and report.checks["protocol_fields"] is True
    for check in ("structured_tool_call", "validated_arguments", "tool_response_accepted", "final_response"):
        assert report.checks[check] is None
    assert report.usage_unknown_calls == (1 if kind == "missing_usage" else 0)
    assert report.reported_input_tokens == (0 if kind == "missing_usage" else 100)
    assert report.reported_output_tokens == (0 if kind == "missing_usage" else 20)
    assert PRIVATE not in report.model_dump_json() and PRIVATE not in caplog.text


@pytest.mark.parametrize("status,category", [(400, "invalid_request"), (401, "authentication_failure"),
                                           (429, "rate_limit"), (503, "server_error")])
def test_provider_error_stops_after_one_attempt_without_retries(status, category, no_tools, caplog):
    attempts = []

    def respond(request):
        attempts.append(True)
        return httpx.Response(status, json={"error": {"message": PRIVATE + " synthetic-test-secret"}})

    with caplog.at_level(logging.DEBUG):
        report = run(respond)
    assert report.status == "FAIL" and report.mode == "first_response"
    assert report.diagnostic.category == category and report.diagnostic.http_status == status
    assert report.failure_stage == "model_request"
    assert report.model_calls == len(attempts) == 1 and report.tool_calls == 0
    assert report.usage_unknown_calls == 1 and report.maximum_output_tokens == 128
    for output in (report.model_dump_json(), caplog.text):
        assert PRIVATE not in output and "synthetic-test-secret" not in output


@pytest.mark.parametrize("error,category", [(httpx.ConnectError, "connection_failure"),
                                          (httpx.ReadTimeout, "timeout")])
def test_transport_error_is_not_retried(error, category, no_tools):
    attempts = []

    def respond(request):
        attempts.append(True)
        raise error(PRIVATE)

    report = run(respond)
    assert report.diagnostic.category == category and report.status == "FAIL"
    assert report.model_calls == len(attempts) == 1 and report.tool_calls == 0
    assert PRIVATE not in report.model_dump_json()


@pytest.mark.parametrize("value,label", [pytest.param(None, "null", id="null"),
                                        pytest.param(PRIVATE, "string_nonempty", id="nonempty")])
def test_reasoning_type_is_sanitized_in_first_response_mode(value, label, no_tools, caplog):
    attempts = []

    def respond(request):
        attempts.append(True)
        return httpx.Response(200, json=completion({"content": PRIVATE, "reasoning": value}))

    with caplog.at_level(logging.DEBUG):
        report = run(respond)
    if value is None:
        assert report.status == "PASS" and report.diagnostic is None
        assert report.checks["protocol_fields"] is True and report.usage_unknown_calls == 0
    else:
        assert report.status == "FAIL" and report.diagnostic.category == "unsupported_protocol_fields"
        assert report.diagnostic.unexpected_fields == ("assistant.reasoning",)
        assert report.diagnostic.reasoning_type == label
    assert report.model_calls == len(attempts) == 1 and report.tool_calls == 0
    assert PRIVATE not in report.model_dump_json() and PRIVATE not in caplog.text


def test_escaped_credential_is_rejected_without_leakage(no_tools, caplog):
    wire = json.dumps(completion({"content": "OK", "reasoning": {"private": "synthetic-test-secret"}}))
    wire = wire.replace("synthetic-test-secret", r"\u0073ynthetic-test-secret")
    with caplog.at_level(logging.DEBUG):
        report = run(lambda request: httpx.Response(200, content=wire,
                                                   headers={"content-type": "application/json"}))
    assert report.diagnostic.category == "credential_guard_rejection"
    assert report.model_calls == 1 and report.tool_calls == 0
    for output in (report.model_dump_json(), caplog.text):
        assert "synthetic-test-secret" not in output and r"\u0073ynthetic-test-secret" not in output


def test_invalid_response_is_not_echoed(no_tools):
    report = run(lambda request: httpx.Response(200, content=PRIVATE))
    assert report.diagnostic.category == "malformed_response"
    assert report.model_calls == 1 and report.tool_calls == 0
    assert PRIVATE not in report.model_dump_json()


def test_diagnostic_flag_alone_does_not_enable_live_calls(no_tools):
    report = asyncio.run(probe.run_capability_check(diagnose_first_response=True, environ=NoEnvironmentReads()))
    assert report.status == "NOT RUN" and report.mode == "first_response"
    assert report.model_calls == report.tool_calls == 0 and report.maximum_output_tokens == 128


def test_diagnostic_missing_credentials_cannot_invoke(no_tools):
    report = asyncio.run(probe.run_capability_check(allow_live=True, diagnose_first_response=True, environ={}))
    assert report.status == "NOT RUN" and report.mode == "first_response"
    assert report.model_calls == report.tool_calls == 0


@pytest.mark.parametrize("invalid_flag", [1, "true", None], ids=["integer", "string", "none"])
def test_invalid_mode_flag_cannot_fall_back_to_full_execution(invalid_flag, no_tools):
    report = asyncio.run(probe.run_capability_check(allow_live=True, diagnose_first_response=invalid_flag,
                                                  environ=NoEnvironmentReads()))
    assert report.status == "NOT RUN" and report.diagnostic.category == "configuration_error"
    assert report.model_calls == report.tool_calls == 0


def test_local_budget_can_reject_before_the_single_request(monkeypatch, no_tools):
    monkeypatch.setattr(probe, "MAX_INPUT_RESERVATION", 1)
    attempts = []

    def respond(request):
        attempts.append(True)
        return httpx.Response(200, json=completion({"content": "OK"}))

    report = run(respond)
    assert report.diagnostic.category == "local_budget_rejection"
    assert report.model_calls == len(attempts) == report.tool_calls == 0


def test_full_mode_unchanged_and_first_wire_request_identical():
    first_requests = []
    reports = []
    for diagnostic_mode in (False, True):
        attempts = []

        def respond(request):
            attempts.append(True)
            if len(attempts) == 1:
                first_requests.append(json.loads(request.content))
                message = tool_message('{"file_path":"probe.py","start_line":1,"end_line":1}')
            else:
                message = {"content": json.dumps({"observed": "answer = 7\n"})}
            return httpx.Response(200, json=completion(message))

        reports.append(asyncio.run(probe.run_capability_check(allow_live=True,
            diagnose_first_response=diagnostic_mode, environ=ENV, transport=httpx.MockTransport(respond))))
        assert len(attempts) == (1 if diagnostic_mode else 2)
    full, first = reports
    assert full.mode == "full" and full.status == "PASS" and all(full.checks.values())
    assert full.tool_calls == 1 and full.maximum_output_tokens == 256
    assert first.status == "PASS" and first.tool_calls == 0 and first.maximum_output_tokens == 128
    assert first_requests[0] == first_requests[1]
    assert first_requests[0]["model"] == "synthetic-model"
    assert first_requests[0]["max_completion_tokens"] == 128


@pytest.mark.parametrize("approved", [False, True])
def test_cli_selects_single_response_mode_without_implying_approval(monkeypatch, capsys, no_tools, approved):
    attempts = []
    original_model = probe.NebiusModel

    def respond(request):
        attempts.append(True)
        return httpx.Response(200, json=completion(tool_message(
            '{"file_path":"probe.py","start_line":1,"end_line":1}')))

    def offline_model(settings, **kwargs):
        kwargs["transport"] = httpx.MockTransport(respond)
        return original_model(settings, **kwargs)

    monkeypatch.setattr(probe, "NebiusModel", offline_model)
    if approved:
        for name, value in {**ENV, "NEBIUS_BASE_URL": "https://api.tokenfactory.nebius.com/v1/"}.items():
            monkeypatch.setenv(name, value)
    else:
        def forbidden_settings(*args, **kwargs):
            pytest.fail("Disabled CLI must not load credential configuration")
        monkeypatch.setattr(probe, "load_nebius_settings", forbidden_settings)
    monkeypatch.setattr(sys, "argv", ["check_nebius.py", "--diagnose-first-response"] +
                        (["--allow-live"] if approved else []))
    probe.main()
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "first_response"
    assert report["status"] == ("PASS" if approved else "NOT RUN")
    assert report["model_calls"] == len(attempts) == (1 if approved else 0)
    assert report["tool_calls"] == 0
