"""Offline characterization of the installed SDK/adapter and safe boundary."""

import asyncio
import json
import logging
import traceback

import httpx
import pytest
from langchain_core.messages import HumanMessage
from langchain_openai.chat_models.base import _convert_message_to_dict
from openai.types.chat import ChatCompletion
from pydantic import ValidationError

from northstar_codereview.providers.nebius import NebiusModel, ProviderRequestError, SafeDiagnostic
from .test_nebius import completion, settings
from .test_provider_diagnostics import run, tool_message


PRIVATE_NAME = "private-source-and-Authorization-field-name"
PRIVATE_VALUE = "private-reasoning-and-tool-argument-content"


@pytest.mark.parametrize("location,extras,expected", [
    ("assistant", {"reasoning_content": None}, ("assistant.reasoning_content",)),
    ("assistant", {"reasoning_content": PRIVATE_VALUE}, ("assistant.reasoning_content",)),
    ("assistant", {"reasoning": None}, ()),
    ("assistant", {"reasoning": PRIVATE_VALUE}, ("assistant.reasoning",)),
    ("assistant", {"reasoning_details": None}, ("assistant.reasoning_details",)),
    ("assistant", {"reasoning_details": [PRIVATE_VALUE]}, ("assistant.reasoning_details",)),
    ("assistant", {"annotations": [PRIVATE_VALUE]}, ("assistant.annotations",)),
    ("assistant", {"audio": {"id": PRIVATE_VALUE}}, ("assistant.audio",)),
    ("assistant", {"function_call": {"name": PRIVATE_VALUE, "arguments": "{}"}}, ("assistant.function_call",)),
    ("assistant", {"name": PRIVATE_VALUE}, ("assistant.name",)),
    ("assistant", {"id": PRIVATE_VALUE}, ("assistant.id",)),
    ("assistant", {PRIVATE_NAME: PRIVATE_VALUE, "another-private-name": None,
                   "reasoning": None}, ("assistant.unknown_field",)),
    ("tool_call", {"index": 0}, ("tool_call.index",)),
    ("tool_call", {PRIVATE_NAME: PRIVATE_VALUE}, ("tool_call.unknown_field",)),
    ("function", {PRIVATE_NAME: PRIVATE_VALUE}, ("function.unknown_field",)),
])
def test_safe_field_diagnostics_through_capability_checker(location, extras, expected, caplog):
    message = tool_message('{"file_path":"probe.py","start_line":1,"end_line":1}')
    target = {"assistant": message, "tool_call": message["tool_calls"][0],
              "function": message["tool_calls"][0]["function"]}[location]
    target.update(extras)
    attempts = []

    def respond(request):
        attempts.append(True)
        if len(attempts) == 2:
            return httpx.Response(200, json=completion({"content": json.dumps({"observed": "answer = 7\n"})}))
        return httpx.Response(200, json=completion(message))

    with caplog.at_level(logging.DEBUG):
        report = run(respond)
    if not expected:
        assert report.status == "PASS" and report.diagnostic is None
        assert report.model_calls == len(attempts) == 2 and report.tool_calls == 1
        assert report.usage_unknown_calls == 0 and all(report.checks.values())
    else:
        assert report.status == "FAIL" and report.diagnostic.category == "unsupported_protocol_fields"
        assert report.diagnostic.http_status == 200
        assert report.diagnostic.unexpected_fields == expected
        assert report.failure_stage == "model_request"
        assert report.model_calls == len(attempts) == 1
        assert report.tool_calls == 0 and report.usage_unknown_calls == 1
    for output in (report.model_dump_json(), caplog.text):
        for private in (PRIVATE_NAME, PRIVATE_VALUE, "another-private-name", "synthetic-test-secret",
                        "probe.py", "start_line"):
            assert private not in output


def test_field_diagnostics_do_not_retain_raw_exception_or_payload():
    async def exercise():
        def respond(request):
            return httpx.Response(200, json=completion({"content": PRIVATE_VALUE, PRIVATE_NAME: None}))
        async with NebiusModel(settings(), allow_live=True, transport=httpx.MockTransport(respond)) as model:
            with pytest.raises(ProviderRequestError) as caught:
                await model.bind_tools(()).ainvoke([HumanMessage(content="Fictional probe")], max_tokens=64)
            error = caught.value
            assert error.diagnostic.unexpected_fields == ("assistant.unknown_field",)
            assert error.__context__ is None and error.__cause__ is None
            output = repr(vars(error)) + "".join(traceback.format_exception(error))
            assert PRIVATE_NAME not in output and PRIVATE_VALUE not in output
            assert "synthetic-test-secret" not in output
    asyncio.run(exercise())


def test_diagnostic_contract_rejects_arbitrary_field_names():
    with pytest.raises(ValidationError):
        SafeDiagnostic(category="unsupported_protocol_fields", unexpected_fields=(PRIVATE_NAME,))


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning", "reasoning_details", "vendor_extension"])
@pytest.mark.parametrize("value", [None, PRIVATE_VALUE])
def test_sdk_retains_but_chatopenai_drops_extensions_on_conversion_and_replay(field, value):
    """Characterize installed dependency code without bypassing a production guard."""
    payload = completion(tool_message('{"file_path":"probe.py","start_line":1,"end_line":1}'))
    payload["choices"][0]["message"][field] = value
    sdk_response = ChatCompletion.model_validate(payload)
    assert sdk_response.model_dump()["choices"][0]["message"][field] == value

    async def exercise():
        def forbidden(request):
            pytest.fail("Serializer characterization must not invoke a transport")
        async with NebiusModel(settings(), allow_live=True, transport=httpx.MockTransport(forbidden)) as model:
            message = model._chat._create_chat_result(sdk_response).generations[0].message
            assert field not in message.additional_kwargs
            assert PRIVATE_VALUE not in message.model_dump_json()
            replay = _convert_message_to_dict(message)
            assert field not in replay
            assert replay["tool_calls"][0]["id"] == "call-1"
            assert json.loads(replay["tool_calls"][0]["function"]["arguments"]) == {
                "file_path": "probe.py", "start_line": 1, "end_line": 1}
            # Simply storing an extension in additional_kwargs also cannot fix replay.
            message.additional_kwargs[field] = value
            assert field not in _convert_message_to_dict(message)
    asyncio.run(exercise())


def test_optional_metadata_and_standard_tool_observation_roundtrip():
    attempts = []
    call_id = "call_preserve_42"

    def respond(request):
        payload = json.loads(request.content)
        attempts.append(True)
        if len(attempts) == 1:
            message = tool_message('{ "end_line": 1, "file_path": "probe.py", "start_line": 1 }')
            message["tool_calls"][0]["id"] = call_id
        else:
            assistant, observation = payload["messages"][-2:]
            assert assistant["role"] == "assistant" and assistant["content"] is None
            call = assistant["tool_calls"][0]
            assert call["id"] == observation["tool_call_id"] == call_id
            assert call["type"] == "function" and call["function"]["name"] == "read_source_lines"
            assert json.loads(call["function"]["arguments"]) == {
                "file_path": "probe.py", "start_line": 1, "end_line": 1}
            assert observation["role"] == "tool"
            assert json.loads(observation["content"])["status"] == "ok"
            message = {"content": json.dumps({"observed": "answer = 7\n"})}
        message["refusal"] = None
        response = completion(message)
        response.update(system_fingerprint="synthetic-fingerprint", service_tier="default")
        response["choices"][0]["logprobs"] = None
        return httpx.Response(200, json=response)

    report = run(respond)
    assert report.status == "PASS" and report.diagnostic is None
    assert report.model_calls == len(attempts) == 2 and report.tool_calls == 1
    assert all(report.checks.values())
    assert report.usage_unknown_calls == 0
