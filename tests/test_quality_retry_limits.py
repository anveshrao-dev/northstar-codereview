"""Graph-level retry controls must agree with the zero-retry provider boundary."""

import asyncio
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage

from northstar_codereview.agents.code_quality import CodeQualityAgent, RunLimits, TransientModelError
from northstar_codereview.local_source import collect_local_snapshot
from northstar_codereview.models import ReviewRequest
from northstar_codereview.providers.nebius import NebiusModel
from .test_nebius import settings
from .test_code_quality import ScriptedModel, call, run


@pytest.fixture
def snapshot(tmp_path):
    (tmp_path / "synthetic.py").write_text("value = 7\n")
    return collect_local_snapshot(ReviewRequest(mode="local", local_path="."), source_root=tmp_path)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_zero_retries_makes_one_physical_attempt_and_charges_failure(snapshot, status):
    attempts = []

    def respond(request):
        attempts.append(True)
        return httpx.Response(status, json={"error": {"message": "synthetic-test-secret"}})

    async def exercise():
        async with NebiusModel(settings(), allow_live=True, transport=httpx.MockTransport(respond)) as model:
            agent = CodeQualityAgent(snapshot, model=model, approved_synthetic=True,
                                     limits=RunLimits(max_provider_retries=0))
            return await agent.run(review_id=uuid4())

    result = asyncio.run(exercise())
    assert result.result.status == "partial"
    assert result.usage.model_calls == len(attempts) == 1
    assert result.usage.retries == 0 and result.usage.usage_unknown_calls == 1
    assert result.usage.input_tokens_reserved > 0 and result.usage.output_tokens_reserved == 1000
    assert result.usage.tool_attempts == 0
    assert "synthetic-test-secret" not in result.model_dump_json()


def test_zero_retry_does_not_reenter_graph_model_node(snapshot):
    model = ScriptedModel(TransientModelError(), AIMessage(content='{"findings": []}'))
    result = run(snapshot, model, max_provider_retries=0)
    assert len(model.histories) == result.usage.model_calls == 1
    assert result.usage.retries == 0


def test_zero_repairs_stops_after_malformed_final_output(snapshot):
    model = ScriptedModel(call(args={"file_path": "synthetic.py", "start_line": 1, "end_line": 1}),
                          AIMessage(content="malformed"), AIMessage(content='{"findings": []}'))
    result = run(snapshot, model, max_output_repairs=0)
    assert result.result.status == "partial"
    assert len(model.histories) == result.usage.model_calls == 2
    assert result.usage.repairs == 0


@pytest.mark.parametrize("field", ["max_provider_retries", "max_output_repairs"])
@pytest.mark.parametrize("value", [-1, 2, True, "0"])
def test_retry_and_repair_limits_are_strict_bounded_integers(field, value):
    with pytest.raises(ValueError):
        RunLimits(**{field: value})
