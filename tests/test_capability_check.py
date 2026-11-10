import asyncio
import json

import httpx
import pytest

from northstar_codereview.capability_check import run_capability_check
from .test_nebius import completion


class NoEnvironmentReads(dict):
    def get(self, *args):
        pytest.fail("Disabled check must not even read credential configuration")


def test_capability_check_disabled_by_default_before_credentials_or_client():
    result = asyncio.run(run_capability_check(environ=NoEnvironmentReads()))
    assert result.status == "NOT RUN" and result.model_calls == 0


def test_missing_configuration_is_not_run():
    result = asyncio.run(run_capability_check(allow_live=True, environ={}))
    assert result.status == "NOT RUN" and result.model_calls == 0


@pytest.mark.parametrize("failure", [None, "no_tool", "bad_args", "wrong_final", "missing_usage", "reasoning_extension"])
def test_manual_probe_checks_real_tool_roundtrip_offline(failure):
    requests = []
    def respond(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body["max_completion_tokens"] == 128
        if len(requests) == 1:
            if failure == "no_tool":
                message = {"content": "I can chat normally."}
            else:
                arguments = {"file_path": "probe.py", "start_line": 1, "end_line": 1}
                if failure == "bad_args":
                    arguments["file_path"] = "../.env"
                message = {"content": None, "tool_calls": [{"id": "probe-call", "type": "function",
                    "function": {"name": "read_source_lines", "arguments": json.dumps(arguments)}}]}
                if failure == "reasoning_extension":
                    message["reasoning_content"] = "private reasoning"
        else:
            assert body["messages"][-1]["role"] == "tool"
            observation = json.loads(body["messages"][-1]["content"])
            text = observation["data"]["text"]
            message = {"content": json.dumps({"observed": text if failure != "wrong_final" else "guessed"})}
        return httpx.Response(200, json=completion(message, usage=failure != "missing_usage"))
    result = asyncio.run(run_capability_check(allow_live=True,
        environ={"NEBIUS_API_KEY": "synthetic-test-secret", "NEBIUS_MODEL": "synthetic-model"},
        transport=httpx.MockTransport(respond)))
    assert result.status == ("PASS" if failure is None else "FAIL")
    assert result.model_calls == len(requests) <= 2
    assert "synthetic-test-secret" not in result.model_dump_json()
    assert "private reasoning" not in result.model_dump_json()
    if failure is None:
        assert all(result.checks.values())
        assert result.reported_input_tokens == 200
        assert result.reported_output_tokens == 40
