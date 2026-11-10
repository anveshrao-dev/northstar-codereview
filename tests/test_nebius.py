"""Actual ChatOpenAI wire serialization over an in-memory HTTP transport."""

import asyncio
import json

import httpx
import pytest
from langchain_core.messages import HumanMessage

from northstar_codereview.providers.nebius import (NebiusSettings, NebiusModel, ProviderConfigurationError,
                                                  ProviderRequestError, load_nebius_settings)
from northstar_codereview.agents.code_quality import TransientModelError
from northstar_codereview.evidence_tools import build_snapshot_tools
from northstar_codereview.local_source import collect_local_snapshot
from northstar_codereview.models import ReviewRequest


def settings():
    return load_nebius_settings({"NEBIUS_API_KEY": "synthetic-test-secret", "NEBIUS_MODEL": "synthetic-model"})


def completion(message, *, usage=True):
    data = {"id": "offline-response", "object": "chat.completion", "created": 0, "model": "synthetic-model",
            "choices": [{"index": 0, "message": {"role": "assistant", **message},
                         "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}]}
    if usage:
        data["usage"] = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    return data


@pytest.mark.parametrize("mapping", [{}, {"NEBIUS_API_KEY": "secret"}, {"NEBIUS_MODEL": "x"},
    {"NEBIUS_API_KEY": " ", "NEBIUS_MODEL": "x"},
    {"NEBIUS_API_KEY": "secret", "NEBIUS_MODEL": "x", "NEBIUS_BASE_URL": "https://attacker.invalid/v1/"},
    {"NEBIUS_API_KEY": "secret", "NEBIUS_MODEL": "x", "NEBIUS_BASE_URL": "http://api.tokenfactory.nebius.com/v1/"},
    {"NEBIUS_API_KEY": "secret", "NEBIUS_MODEL": "x", "NEBIUS_BASE_URL": "https://secret@api.tokenfactory.nebius.com/v1/"}])
def test_configuration_fails_safely_without_defaults(mapping):
    with pytest.raises(ProviderConfigurationError) as caught:
        load_nebius_settings(mapping)
    assert "secret" not in str(caught.value)
    assert "attacker" not in str(caught.value)


def test_secret_excluded_from_representation_and_serialization():
    config = settings()
    assert "synthetic-test-secret" not in repr(config)
    assert "api_key" not in config.model_dump()
    assert "synthetic-test-secret" not in config.model_dump_json()
    assert config.base_url == "https://api.tokenfactory.nebius.com/v1/"
    with pytest.raises(ProviderConfigurationError):
        NebiusModel(config)


def test_actual_chatopenai_tool_roundtrip_and_no_sdk_retries(tmp_path, monkeypatch):
    (tmp_path / "synthetic.py").write_text("return_value = 7\n")
    bundle = collect_local_snapshot(ReviewRequest(mode="local", local_path="."), source_root=tmp_path)
    tools = build_snapshot_tools(bundle, approved_synthetic=True)
    requests = []
    monkeypatch.setenv("OPENAI_API_KEY", "other-provider-secret")
    monkeypatch.setenv("OPENAI_ORG_ID", "private-org")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "private-project")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://attacker.invalid/")

    def respond(request):
        requests.append(request)
        data = json.loads(request.content)
        assert data["model"] == "synthetic-model"
        assert data["max_completion_tokens"] == 128
        assert str(request.url) == "https://api.tokenfactory.nebius.com/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer synthetic-test-secret"
        assert "private-org" not in str(request.headers)
        assert "private-project" not in str(request.headers)
        if len(requests) == 1:
            return httpx.Response(200, json=completion({"content": None, "tool_calls": [{"id": "call-1", "type": "function",
                "function": {"name": "list_snapshot_files", "arguments": '{"limit": 1}'}}]}))
        assert data["messages"][-2]["tool_calls"][0]["id"] == "call-1"
        assert data["messages"][-1]["tool_call_id"] == "call-1"
        assert json.loads(data["messages"][-1]["content"])["status"] == "ok"
        return httpx.Response(200, json=completion({"content": "OK"}))

    async def exercise():
        async with NebiusModel(settings(), allow_live=True, transport=httpx.MockTransport(respond)) as model:
            bound = model.bind_tools(tools)
            history = [HumanMessage(content="List the synthetic files.")]
            first = await bound.ainvoke(history, max_tokens=128)
            assert first.usage_metadata["input_tokens"] == 100
            observation = tools[0].invoke({**first.tool_calls[0], "type": "tool_call"})
            second = await bound.ainvoke([*history, first, observation], max_tokens=128)
            assert second.content == "OK"
            assert model.protocol_checks == 2
            assert model.sdk_max_retries == 0
    asyncio.run(exercise())
    assert len(requests) == 2


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning", "reasoning_details", "unknown_protocol_field"])
def test_nonstandard_fields_fail_closed_before_silent_loss(field):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=completion({"content": "OK", field: "hidden content never reported"}))
    async def exercise():
        async with NebiusModel(settings(), allow_live=True, transport=httpx.MockTransport(respond)) as model:
            with pytest.raises(ProviderRequestError) as error:
                await model.bind_tools(()).ainvoke([HumanMessage(content="Synthetic probe")], max_tokens=64)
            assert "hidden content" not in str(error.value)
            assert "synthetic-test-secret" not in str(error.value)
    asyncio.run(exercise())
    assert len(requests) == 1


@pytest.mark.parametrize("status,error_type", [(400, ProviderRequestError), (401, ProviderRequestError),
                                            (429, TransientModelError), (503, TransientModelError)])
def test_http_errors_are_sanitized_and_sdk_does_not_retry(status, error_type):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "synthetic-test-secret and private request text"}})
    async def exercise():
        async with NebiusModel(settings(), allow_live=True, transport=httpx.MockTransport(respond)) as model:
            with pytest.raises(error_type) as error:
                await model.bind_tools(()).ainvoke([HumanMessage(content="Synthetic probe")], max_tokens=64)
            assert "synthetic-test-secret" not in str(error.value)
            assert error.value.__cause__ is None
    asyncio.run(exercise())
    assert len(requests) == 1


@pytest.mark.parametrize("location", ["content", "nested_final_json", "nested_tool_arguments", "object_key"])
def test_json_escaped_credentials_cannot_enter_messages_or_reports(location):
    secret = "synthetic-test-secret"
    escaped = r"\u0073ynthetic-test-secret"
    payload = completion({"content": secret})
    if location == "nested_final_json":
        payload = completion({"content": json.dumps({"findings": [{"explanation": secret}]}).replace(secret, escaped)})
    elif location == "nested_tool_arguments":
        payload = completion({"content": None, "tool_calls": [{"id": "secret-args", "type": "function",
            "function": {"name": "get_source_metadata", "arguments": json.dumps({"file_path": secret}).replace(secret, escaped)}}]})
    elif location == "object_key":
        payload = completion({"content": "OK"})
        payload["usage"][secret] = 1
    wire = json.dumps(payload).replace(secret, escaped).encode()
    assert secret.encode() not in wire
    def respond(request):
        return httpx.Response(200, content=wire, headers={"content-type": "application/json"})
    async def exercise():
        async with NebiusModel(settings(), allow_live=True, transport=httpx.MockTransport(respond)) as model:
            with pytest.raises(ProviderRequestError) as caught:
                await model.bind_tools(()).ainvoke([HumanMessage(content="Synthetic probe")], max_tokens=64)
            assert secret not in str(caught.value)
            assert caught.value.__context__ is None
    asyncio.run(exercise())


def test_json_escaped_credential_is_rejected_before_request_send():
    secret = 'synthetic-"quoted-secret'
    config = load_nebius_settings({"NEBIUS_API_KEY": secret, "NEBIUS_MODEL": "synthetic-model"})
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=completion({"content": "OK"}))
    async def exercise():
        async with NebiusModel(config, allow_live=True, transport=httpx.MockTransport(respond)) as model:
            with pytest.raises(ProviderRequestError) as caught:
                await model.bind_tools(()).ainvoke([HumanMessage(content=secret)], max_tokens=64)
            assert secret not in str(caught.value)
            assert caught.value.__context__ is None
    asyncio.run(exercise())
    assert requests == []
