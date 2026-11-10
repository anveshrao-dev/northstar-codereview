"""Safe failure categories through real ChatOpenAI and the capability checker."""

import asyncio
import json
import logging
import traceback

import httpx
import pytest
from langchain_core.messages import HumanMessage

from northstar_codereview import capability_check as probe
from northstar_codereview.providers.nebius import NebiusModel, ProviderRequestError
from .test_nebius import completion, settings


ENV = {"NEBIUS_API_KEY": "synthetic-test-secret", "NEBIUS_MODEL": "synthetic-model"}
PRIVATE = "synthetic-test-secret PRIVATE_BODY Authorization: Bearer hidden"


def run(handler):
    return asyncio.run(probe.run_capability_check(allow_live=True, environ=ENV,
                                                 transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("status,category", [(400, "invalid_request"), (401, "authentication_failure"),
    (403, "authorization_failure"), (404, "model_unavailable"), (422, "invalid_request"),
    (429, "rate_limit"), (500, "server_error"), (503, "server_error"), (418, "unknown_provider_error")])
def test_http_classification_no_body_or_header_leakage_or_retries(status, category, caplog):
    attempts = []
    def respond(request):
        attempts.append(True)
        return httpx.Response(status, json={"error": {"message": PRIVATE, "code": PRIVATE, "param": PRIVATE}},
                              headers={"x-request-id": "public-test-id", "x-private": PRIVATE})
    with caplog.at_level(logging.DEBUG):
        report = run(respond)
    assert report.status == "FAIL"
    assert report.diagnostic.category == category
    assert report.diagnostic.http_status == status
    assert report.failure_stage == "model_request"
    assert report.model_calls == len(attempts) == 1
    assert report.tool_calls == 0 and report.usage_unknown_calls == 1
    for text in (report.model_dump_json(), caplog.text):
        assert "PRIVATE_BODY" not in text and "synthetic-test-secret" not in text
        assert "Authorization: Bearer hidden" not in text


@pytest.mark.parametrize("exception,category", [(httpx.ConnectError, "connection_failure"),
    (httpx.ReadTimeout, "timeout"), (RuntimeError, "unknown_provider_error")])
def test_transport_failure_classification_and_safe_exception_chains(exception, category):
    attempts = []
    def respond(request):
        attempts.append(True)
        raise exception(PRIVATE)
    async def exercise():
        async with NebiusModel(settings(), allow_live=True, transport=httpx.MockTransport(respond)) as model:
            with pytest.raises(ProviderRequestError) as caught:
                await model.bind_tools(()).ainvoke([HumanMessage(content="Synthetic probe")], max_tokens=64)
            error = caught.value
            assert error.diagnostic.category == category
            assert error.__context__ is None and error.__cause__ is None
            assert "PRIVATE_BODY" not in ''.join(traceback.format_exception(error))
            assert "synthetic-test-secret" not in str(vars(error))
    asyncio.run(exercise())
    assert len(attempts) == 1


def tool_message(arguments):
    return {"content": None, "tool_calls": [{"id": "call-1", "type": "function",
        "function": {"name": "read_source_lines", "arguments": arguments}}]}


@pytest.mark.parametrize("arguments", ['{', '[]', '{"file_path":"../.env","start_line":1,"end_line":1}',
    '{"file_path":"probe.py","start_line":true,"end_line":1}'])
def test_malformed_arguments_classified_without_echoing_payload(arguments):
    report = run(lambda request: httpx.Response(200, json=completion(tool_message(arguments))))
    assert report.diagnostic.category == "malformed_tool_call"
    assert report.tool_calls == 0 and report.model_calls == 1
    assert "../.env" not in report.model_dump_json()


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning", "reasoning_details", "unknown_field"])
def test_null_reasoning_reaches_tool_validation_but_other_null_extensions_reject(field):
    report = run(lambda request: httpx.Response(200, json=completion({"content": "OK", field: None})))
    if field == "reasoning":
        assert report.checks["model_available"] is True
        assert report.failure_stage == "tool_validation"
        assert report.diagnostic.category == "malformed_tool_call"
        assert report.diagnostic.http_status is None
    else:
        assert report.diagnostic.category == "unsupported_protocol_fields"
        assert report.diagnostic.http_status == 200
    assert report.model_calls == 1 and report.tool_calls == 0


def test_local_budget_rejection_prevents_request(monkeypatch):
    monkeypatch.setattr(probe, "MAX_INPUT_RESERVATION", 1)
    def forbidden(request):
        pytest.fail("Local budget must stop before HTTP transport")
    report = run(forbidden)
    assert report.diagnostic.category == "local_budget_rejection"
    assert report.model_calls == 0 and report.usage_unknown_calls == 0


@pytest.mark.parametrize("content,category", [(b'not-json', "malformed_response"),
    (b'{}', "malformed_response"), (b'x' * 131073, "response_size_limit")])
def test_response_shape_and_size_classification(content, category):
    report = run(lambda request: httpx.Response(200, content=content))
    assert report.diagnostic.category == category
    assert report.diagnostic.http_status == 200


def test_final_validation_failure_keeps_successful_tool_roundtrip_visible():
    attempts = []
    def respond(request):
        attempts.append(True)
        message = (tool_message(json.dumps(dict(file_path="probe.py", start_line=1, end_line=1)))
                   if len(attempts) == 1 else {"content": "not JSON " + "PRIVATE_BODY"})
        return httpx.Response(200, json=completion(message))
    report = run(respond)
    assert report.diagnostic.category == "invalid_final_response"
    assert report.failure_stage == "final_validation"
    assert report.tool_calls == 1 and report.model_calls == 2
    assert report.checks["tool_response_accepted"] is True
    assert report.checks["final_response"] is False
    assert report.usage_unknown_calls == 0
    assert "PRIVATE_BODY" not in report.model_dump_json()


@pytest.mark.parametrize("status", [400, 200])
def test_transport_debug_and_success_header_cannot_leak_before_classification(status, caplog):
    def respond(request):
        # The real httpcore trace emits header/exception repr before response hooks.
        # Exercise that logging boundary without opening a socket.
        logging.getLogger("httpcore.http11").debug("receive_response_headers.complete %r", PRIVATE)
        logging.getLogger("httpcore.connection").debug("connect_tcp.failed exception=%r", RuntimeError(PRIVATE))
        payload = ({"error": {"message": PRIVATE}} if status == 400 else
                   completion(tool_message('{"file_path":"probe.py","start_line":true,"end_line":1}')))
        return httpx.Response(status, json=payload, headers={"x-request-id": PRIVATE})
    with caplog.at_level(logging.DEBUG):
        report = run(respond)
        logging.getLogger("httpcore.http11").debug("logging-restored-after-call")
    assert report.status == "FAIL"
    assert "PRIVATE_BODY" not in caplog.text and "synthetic-test-secret" not in caplog.text
    assert "logging-restored-after-call" in caplog.text


def test_logging_suppression_does_not_hide_unrelated_concurrent_context(caplog):
    async def exercise():
        entered, released = asyncio.Event(), asyncio.Event()
        async def respond(request):
            logging.getLogger("httpcore.http11").debug(PRIVATE)
            entered.set()
            await released.wait()
            return httpx.Response(200, json=completion({"content": "OK"}))
        async def unrelated():
            await entered.wait()
            logging.getLogger("httpcore.http11").debug("unrelated-context-visible")
            released.set()
        async with NebiusModel(settings(), allow_live=True, transport=httpx.MockTransport(respond)) as model:
            await asyncio.gather(model.bind_tools(()).ainvoke([HumanMessage(content="Synthetic probe")], max_tokens=64),
                                 unrelated())
    with caplog.at_level(logging.DEBUG):
        asyncio.run(exercise())
    assert "PRIVATE_BODY" not in caplog.text
    assert "unrelated-context-visible" in caplog.text
