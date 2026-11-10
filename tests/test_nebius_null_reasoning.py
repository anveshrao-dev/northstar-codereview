"""Null-only compatibility through the real SDK and an offline HTTP transport."""

import json
import logging

import httpx
import pytest

from .test_nebius import completion
from .test_nebius_optional_fields import ANNOTATIONS, AUDIO, LEGACY_CALL
from .test_provider_diagnostics import run, tool_message


PRIVATE = "fictional-private-assistant-text"
ARGUMENTS = '{"file_path":"probe.py","start_line":1,"end_line":1}'


@pytest.mark.parametrize("null_on", ["neither", "first", "final", "both"])
@pytest.mark.parametrize("content", [None, PRIVATE], ids=["no-content", "text-content"])
def test_absent_or_null_reasoning_preserves_standard_roundtrip(null_on, content, caplog):
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == "synthetic-model"
        assert body["max_completion_tokens"] == 128
        if len(requests) == 1:
            message = tool_message(ARGUMENTS)
            message["content"] = content
            message["tool_calls"][0]["id"] = "null_reasoning_call_42"
            if null_on in {"first", "both"}:
                message["reasoning"] = None
        else:
            assert len(requests) == 2
            history = body["messages"]
            assert [item["role"] for item in history] == ["system", "user", "assistant", "tool"]
            assert history[:2] == requests[0]["messages"]
            assistant, observation = history[-2:]
            assert set(assistant) == {"role", "content", "tool_calls"}
            assert assistant["content"] == content
            assert len(assistant["tool_calls"]) == 1
            call = assistant["tool_calls"][0]
            assert call["id"] == observation["tool_call_id"] == "null_reasoning_call_42"
            assert call["type"] == "function"
            assert call["function"]["name"] == "read_source_lines"
            assert json.loads(call["function"]["arguments"]) == {
                "file_path": "probe.py", "start_line": 1, "end_line": 1}
            result = json.loads(observation["content"])
            assert result["status"] == "ok" and result["data"]["text"] == "answer = 7\n"
            message = {"content": json.dumps({"observed": "answer = 7\n"})}
            if null_on in {"final", "both"}:
                message["reasoning"] = None
        message.update(annotations=[], audio=None, function_call=None)
        return httpx.Response(200, json=completion(message))

    with caplog.at_level(logging.DEBUG):
        report = run(respond)
    assert report.status == "PASS" and report.diagnostic is None
    assert all(report.checks.values())
    assert report.model_calls == len(requests) == 2 and report.tool_calls == 1
    assert report.usage_unknown_calls == 0
    for output in (report.model_dump_json(), caplog.text):
        assert PRIVATE not in output and "synthetic-test-secret" not in output


@pytest.mark.parametrize("field,value,label", [
    ("reasoning_content", None, "assistant.reasoning_content"),
    ("reasoning_content", PRIVATE, "assistant.reasoning_content"),
    ("reasoning_details", None, "assistant.reasoning_details"),
    ("reasoning_details", [], "assistant.reasoning_details"),
    ("private_unknown_field", None, "assistant.unknown_field"),
    ("annotations", ANNOTATIONS, "assistant.annotations"),
    ("audio", AUDIO, "assistant.audio"),
    ("function_call", LEGACY_CALL, "assistant.function_call"),
])
def test_null_reasoning_does_not_hide_unsupported_sibling(field, value, label, caplog):
    attempts = []

    def respond(request):
        attempts.append(True)
        message = tool_message(ARGUMENTS)
        message.update(reasoning=None, annotations=[], audio=None, function_call=None)
        message[field] = value
        return httpx.Response(200, json=completion(message))

    with caplog.at_level(logging.DEBUG):
        report = run(respond)
    assert report.status == "FAIL" and report.failure_stage == "model_request"
    assert report.diagnostic.model_dump(mode="json") == {
        "category": "unsupported_protocol_fields", "http_status": 200,
        "unexpected_fields": [label], "reasoning_type": "null"}
    assert report.model_calls == len(attempts) == 1 and report.tool_calls == 0
    for output in (report.model_dump_json(), caplog.text):
        for private in (PRIVATE, "private_unknown_field", "fictional-private-content-not-for-diagnostics",
                        "synthetic-test-secret", "probe.py", "start_line"):
            assert private not in output


@pytest.mark.parametrize("arguments", ["{", "[]"], ids=["invalid-json", "not-an-object"])
def test_null_reasoning_does_not_bypass_tool_argument_guard(arguments):
    attempts = []

    def respond(request):
        attempts.append(True)
        return httpx.Response(200, json=completion({**tool_message(arguments), "reasoning": None}))

    report = run(respond)
    assert report.diagnostic.category == "malformed_tool_call"
    assert report.diagnostic.http_status == 200
    assert report.model_calls == len(attempts) == 1 and report.tool_calls == 0


@pytest.mark.parametrize("location", ["content", "arguments"])
def test_escaped_credential_filter_still_precedes_null_acceptance(location, caplog):
    message = {**tool_message(ARGUMENTS), "reasoning": None}
    secret = "synthetic-test-secret"
    if location == "content":
        message["content"] = secret
    else:
        message["tool_calls"][0]["function"]["arguments"] = json.dumps({"private": secret})
    wire = json.dumps(completion(message)).replace(secret, r"\u0073ynthetic-test-secret")
    with caplog.at_level(logging.DEBUG):
        report = run(lambda request: httpx.Response(200, content=wire,
                                                    headers={"content-type": "application/json"}))
    assert report.diagnostic.category == "credential_guard_rejection"
    assert report.diagnostic.unexpected_fields == () and report.diagnostic.reasoning_type is None
    assert report.model_calls == 1 and report.tool_calls == 0
    for output in (report.model_dump_json(), caplog.text):
        assert secret not in output and r"\u0073ynthetic-test-secret" not in output


@pytest.mark.parametrize("value,label", [(PRIVATE, "string_nonempty"), ({"private": PRIVATE}, "object")])
def test_nonnull_reasoning_on_followup_is_rejected_before_normalization(value, label, caplog):
    attempts = []

    def respond(request):
        attempts.append(True)
        message = ({**tool_message(ARGUMENTS), "reasoning": None} if len(attempts) == 1 else
                   {"content": json.dumps({"observed": "answer = 7\n"}), "reasoning": value})
        return httpx.Response(200, json=completion(message))

    with caplog.at_level(logging.DEBUG):
        report = run(respond)
    assert report.status == "FAIL" and report.failure_stage == "model_request"
    assert report.diagnostic.category == "unsupported_protocol_fields"
    assert report.diagnostic.unexpected_fields == ("assistant.reasoning",)
    assert report.diagnostic.reasoning_type == label
    assert report.model_calls == len(attempts) == 2 and report.tool_calls == 1
    assert report.usage_unknown_calls == 1 and report.checks["final_response"] is None
    for output in (report.model_dump_json(), caplog.text):
        assert PRIVATE not in output and "synthetic-test-secret" not in output
