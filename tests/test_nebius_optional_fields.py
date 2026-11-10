"""Accept only SDK-defined optional fields with explicitly stateless values."""

import json
import logging

import httpx
import pytest
from openai.types.chat import ChatCompletionMessage

from .test_nebius import completion
from .test_provider_diagnostics import run, tool_message


PRIVATE = "fictional-private-content-not-for-diagnostics"
ANNOTATIONS = [{"type": "url_citation", "url_citation": {
    "start_index": 0, "end_index": 1, "title": PRIVATE, "url": "https://example.invalid/"}}]
AUDIO = {"id": "synthetic-audio", "data": PRIVATE, "expires_at": 1, "transcript": PRIVATE}
LEGACY_CALL = {"name": "read_source_lines", "arguments": json.dumps({"private": PRIVATE})}


def test_sdk_distinguishes_absent_null_and_empty_annotations_without_defining_reasoning():
    absent = ChatCompletionMessage.model_validate({"role": "assistant", "content": "OK"})
    optional = {"annotations", "audio", "function_call"}
    assert optional.isdisjoint(absent.model_fields_set)
    assert all(absent.model_dump()[name] is None for name in optional)
    assert optional.isdisjoint(absent.model_dump(exclude_unset=True))
    assert "reasoning" not in absent.model_dump()

    explicit = ChatCompletionMessage.model_validate({"role": "assistant", "content": "OK",
        "annotations": None, "audio": None, "function_call": None, "reasoning": None})
    assert optional | {"reasoning"} <= explicit.model_fields_set
    assert "reasoning" not in ChatCompletionMessage.model_fields
    assert explicit.model_extra == {"reasoning": None}
    assert explicit.model_dump(exclude_unset=True)["annotations"] is None

    empty = ChatCompletionMessage.model_validate({"role": "assistant", "annotations": []})
    assert empty.annotations == [] and "annotations" in empty.model_fields_set


@pytest.mark.parametrize("extras", [
    {}, {"annotations": None}, {"audio": None}, {"function_call": None},
    {"annotations": None, "audio": None, "function_call": None},
    {"annotations": []}, {"annotations": [], "audio": None, "function_call": None},
])
def test_stateless_optional_fields_allow_standard_tool_roundtrip(extras):
    attempts = []

    def respond(request):
        payload = json.loads(request.content)
        attempts.append(True)
        if len(attempts) == 1:
            message = tool_message('{"file_path":"probe.py","start_line":1,"end_line":1}')
            message["tool_calls"][0]["id"] = "call_optional_42"
        else:
            assistant, observation = payload["messages"][-2:]
            assert assistant["role"] == "assistant" and assistant["content"] is None
            call = assistant["tool_calls"][0]
            assert call["id"] == observation["tool_call_id"] == "call_optional_42"
            assert call["function"]["name"] == "read_source_lines"
            assert json.loads(call["function"]["arguments"]) == {
                "file_path": "probe.py", "start_line": 1, "end_line": 1}
            assert observation["role"] == "tool"
            assert json.loads(observation["content"])["status"] == "ok"
            assert {"annotations", "audio", "function_call", "reasoning"}.isdisjoint(assistant)
            message = {"content": json.dumps({"observed": "answer = 7\n"})}
        return httpx.Response(200, json=completion({**message, **extras}))

    report = run(respond)
    assert report.status == "PASS" and report.diagnostic is None
    assert report.model_calls == len(attempts) == 2 and report.tool_calls == 1
    assert all(report.checks.values()) and report.usage_unknown_calls == 0


@pytest.mark.parametrize("field,value", [
    ("annotations", ANNOTATIONS), ("audio", AUDIO), ("function_call", LEGACY_CALL),
    *[(field, value) for field in ("annotations", "audio", "function_call")
      for value in ("", {}, False, 0)],
    ("audio", []), ("function_call", []),
    *[("reasoning", value) for value in (None, "", [], {}, False, 0, PRIVATE)],
    *[("unknown_private_field", value) for value in (None, "", [], {}, PRIVATE)],
])
def test_optional_extensions_preserve_null_exception_and_other_guards(field, value, caplog):
    attempts = []

    def respond(request):
        attempts.append(True)
        # Permitted optional fields must not hide an unsupported sibling.
        message = {"content": "OK", "annotations": None, "audio": None, "function_call": None,
                   field: value}
        return httpx.Response(200, json=completion(message))

    with caplog.at_level(logging.DEBUG):
        report = run(respond)
    assert report.status == "FAIL"
    if field == "reasoning" and value is None:
        # Null is accepted, but the full probe still requires a tool call.
        assert report.checks["model_available"] is True
        assert report.failure_stage == "tool_validation"
        assert report.diagnostic.category == "malformed_tool_call"
        assert report.diagnostic.http_status is None and report.diagnostic.unexpected_fields == ()
    else:
        assert report.failure_stage == "model_request"
        assert report.diagnostic.category == "unsupported_protocol_fields"
        assert report.diagnostic.http_status == 200
        expected = "assistant.unknown_field" if field == "unknown_private_field" else "assistant." + field
        assert report.diagnostic.unexpected_fields == (expected,)
    assert report.model_calls == len(attempts) == 1 and report.tool_calls == 0
    for output in (report.model_dump_json(), caplog.text):
        assert PRIVATE not in output and "unknown_private_field" not in output
        assert "synthetic-test-secret" not in output


@pytest.mark.parametrize("field", ["annotations", "audio", "function_call"])
def test_credential_filter_precedes_optional_field_handling(field, caplog):
    attempts = []

    def respond(request):
        attempts.append(True)
        message = {"content": "OK", "annotations": None, "audio": None, "function_call": None,
                   field: {"private": "synthetic-test-secret"}}
        wire = json.dumps(completion(message)).replace("synthetic-test-secret", r"\u0073ynthetic-test-secret")
        return httpx.Response(200, content=wire, headers={"content-type": "application/json"})

    with caplog.at_level(logging.DEBUG):
        report = run(respond)
    assert report.diagnostic.category == "credential_guard_rejection"
    assert report.diagnostic.unexpected_fields == ()
    assert report.model_calls == len(attempts) == 1 and report.tool_calls == 0
    for output in (report.model_dump_json(), caplog.text):
        assert "synthetic-test-secret" not in output and r"\u0073ynthetic-test-secret" not in output
