"""Type-only diagnostics at the real response boundary; no live transport."""

import asyncio
import json
import logging
import traceback

import httpx
import pytest
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from northstar_codereview.providers.nebius import NebiusModel, ProviderRequestError, SafeDiagnostic
from .test_nebius import completion, settings
from .test_provider_diagnostics import run, tool_message


PRIVATE = "fictional-private-reasoning-content"
PRIVATE_KEY = "fictional-private-nested-key"


@pytest.mark.parametrize("value,expected", [
    pytest.param(None, "null", id="null"),
    pytest.param("", "string_empty", id="empty-string"),
    pytest.param(" \t\n", "string_nonempty", id="whitespace-string"),
    pytest.param(PRIVATE, "string_nonempty", id="nonempty-string"),
    pytest.param([], "array", id="empty-array"),
    pytest.param([PRIVATE], "array", id="nonempty-array"),
    pytest.param({}, "object", id="empty-object"),
    pytest.param({PRIVATE_KEY: PRIVATE}, "object", id="nonempty-object"),
    pytest.param(True, "boolean", id="true"),
    pytest.param(False, "boolean", id="false"),
    pytest.param(0, "number", id="zero"),
    pytest.param(-4, "number", id="negative-integer"),
    pytest.param(1.25, "number", id="fraction"),
])
def test_only_null_reasoning_is_accepted_and_other_types_keep_safe_diagnostics(value, expected, caplog):
    attempts = []

    def respond(request):
        attempts.append(True)
        message = tool_message('{"file_path":"probe.py","start_line":1,"end_line":1}')
        message.update(content=PRIVATE, reasoning=value, annotations=[], audio=None, function_call=None)
        if len(attempts) == 2:
            message = {"content": json.dumps({"observed": "answer = 7\n"}), "reasoning": value}
        return httpx.Response(200, json=completion(message))

    with caplog.at_level(logging.DEBUG):
        report = run(respond)
    if value is None:
        assert report.status == "PASS" and report.diagnostic is None
        assert report.model_calls == len(attempts) == 2 and report.tool_calls == 1
        assert report.usage_unknown_calls == 0 and all(report.checks.values())
    else:
        assert report.status == "FAIL" and report.failure_stage == "model_request"
        assert report.diagnostic.model_dump(mode="json") == {
            "category": "unsupported_protocol_fields", "http_status": 200,
            "unexpected_fields": ["assistant.reasoning"], "reasoning_type": expected}
        assert report.model_calls == len(attempts) == 1
        assert report.tool_calls == 0 and report.usage_unknown_calls == 1
        assert all(value is None for value in report.checks.values())
    for output in (report.model_dump_json(), caplog.text):
        for private in (PRIVATE, PRIVATE_KEY, "synthetic-test-secret", "probe.py", "start_line"):
            assert private not in output


@pytest.mark.parametrize("extras,labels", [
    ({PRIVATE_KEY: PRIVATE}, ("assistant.unknown_field",)),
    ({"reasoning_content": PRIVATE}, ("assistant.reasoning_content",)),
    ({"reasoning_details": [PRIVATE]}, ("assistant.reasoning_details",)),
])
def test_absent_means_exact_reasoning_key_is_missing(extras, labels):
    report = run(lambda request: httpx.Response(200, json=completion({"content": "OK", **extras})))
    assert report.diagnostic.category == "unsupported_protocol_fields"
    assert report.diagnostic.unexpected_fields == labels
    assert report.diagnostic.reasoning_type == "absent"
    assert PRIVATE not in report.model_dump_json() and PRIVATE_KEY not in report.model_dump_json()


@pytest.mark.parametrize("annotations", [None, []])
def test_absent_reasoning_preserves_optional_fields_and_tool_roundtrip(annotations):
    attempts = []

    def respond(request):
        attempts.append(True)
        if len(attempts) == 1:
            message = tool_message('{"file_path":"probe.py","start_line":1,"end_line":1}')
            message["tool_calls"][0]["id"] = "preserved_call_9"
        else:
            assistant, observation = json.loads(request.content)["messages"][-2:]
            call = assistant["tool_calls"][0]
            assert call["id"] == observation["tool_call_id"] == "preserved_call_9"
            assert call["function"]["name"] == "read_source_lines"
            assert json.loads(call["function"]["arguments"]) == {
                "file_path": "probe.py", "start_line": 1, "end_line": 1}
            assert observation["role"] == "tool" and json.loads(observation["content"])["status"] == "ok"
            assert "reasoning" not in assistant
            message = {"content": json.dumps({"observed": "answer = 7\n"})}
        message.update(annotations=annotations, audio=None, function_call=None)
        return httpx.Response(200, json=completion(message))

    report = run(respond)
    assert report.status == "PASS" and report.diagnostic is None
    assert all(report.checks.values())
    assert report.model_calls == len(attempts) == 2 and report.tool_calls == 1


@pytest.mark.parametrize("nested", [False, True])
def test_credential_guard_precedes_reasoning_classification(nested, caplog):
    secret = "synthetic-test-secret"
    value = json.dumps({PRIVATE_KEY: secret}) if nested else {PRIVATE_KEY: secret}
    wire = json.dumps(completion({"content": "OK", "reasoning": value})).replace(
        secret, r"\u0073ynthetic-test-secret")
    with caplog.at_level(logging.DEBUG):
        report = run(lambda request: httpx.Response(200, content=wire,
                                                    headers={"content-type": "application/json"}))
    assert report.diagnostic.category == "credential_guard_rejection"
    assert report.diagnostic.reasoning_type is None
    assert report.model_calls == 1 and report.tool_calls == 0
    for output in (report.model_dump_json(), caplog.text):
        assert secret not in output and PRIVATE_KEY not in output
        assert r"\u0073ynthetic-test-secret" not in output


def test_type_diagnostic_public_exception_has_no_raw_data_or_chain():
    async def exercise():
        def respond(request):
            return httpx.Response(200, json=completion({"content": PRIVATE,
                                                       "reasoning": {PRIVATE_KEY: PRIVATE}}))
        async with NebiusModel(settings(), allow_live=True, transport=httpx.MockTransport(respond)) as model:
            with pytest.raises(ProviderRequestError) as caught:
                await model.bind_tools(()).ainvoke([HumanMessage(content="Fictional probe")], max_tokens=64)
            error = caught.value
            assert error.diagnostic.reasoning_type == "object"
            assert error.__cause__ is None and error.__context__ is None
            output = repr(vars(error)) + "".join(traceback.format_exception(error))
            assert PRIVATE not in output and PRIVATE_KEY not in output
            assert "synthetic-test-secret" not in output
    asyncio.run(exercise())


@pytest.mark.parametrize("label", ["string", "NULL", PRIVATE], ids=["generic-string", "wrong-case", "free-text"])
def test_reasoning_type_contract_rejects_non_allowlisted_labels(label):
    with pytest.raises(ValidationError):
        SafeDiagnostic(category="unsupported_protocol_fields", reasoning_type=label)


def test_uninspected_error_body_does_not_falsely_report_absent():
    report = run(lambda request: httpx.Response(401, json={"reasoning": PRIVATE}))
    assert report.diagnostic.category == "authentication_failure"
    assert report.diagnostic.reasoning_type is None
    assert PRIVATE not in report.model_dump_json()


def test_nonobject_message_still_rejects_without_claiming_a_field_type():
    payload = completion({"content": "OK"})
    payload["choices"][0]["message"] = ["reasoning"]
    report = run(lambda request: httpx.Response(200, json=payload))
    assert report.diagnostic.category == "unsupported_protocol_fields"
    assert report.diagnostic.reasoning_type is None
    assert report.model_calls == 1 and report.tool_calls == 0
