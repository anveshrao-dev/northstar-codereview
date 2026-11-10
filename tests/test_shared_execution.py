"""A shared budget applies inside existing specialist graphs, including recovery."""

import asyncio
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from northstar_codereview.agents.code_quality import CodeQualityAgent, RunLimits
from northstar_codereview.evaluation_data import load_dataset, load_scenario
from northstar_codereview.execution_budget import ExecutionBudget, GlobalLimits
from northstar_codereview.orchestrator import ReviewOrchestrator


class ToolModel:
    def __init__(self, snapshot, *, invalid=None):
        self.snapshot, self.invalid, self.calls = snapshot, invalid, 0

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        await asyncio.sleep(0)
        if self.calls > 1:
            return AIMessage(content='{"findings": []}')
        file = self.snapshot.manifest.files[0]
        end = len(self.snapshot.sources[file.side, file.file_path].splitlines()) + 1
        calls = [dict(name='read_source_lines', args=dict(file_path=file.file_path, start_line=1, end_line=end), id='read')]
        if self.invalid == 'unknown':
            calls[0]['name'] = 'not_a_tool'
        if self.invalid == 'duplicate':
            calls *= 3
        if self.invalid == 'malformed':
            return AIMessage(content='', invalid_tool_calls=[dict(name='read_source_lines', args='{bad', id='bad', error='invalid')]*3)
        return AIMessage(content='', tool_calls=calls)


@pytest.mark.parametrize('cap',[2,3])
def test_global_request_cap_across_parallel_specialists(cap):
    snapshot = load_scenario(load_dataset().scenarios[0])
    models = []
    def factory(name):
        model = ToolModel(snapshot)
        models.append(model)
        return model
    ledger = ExecutionBudget(GlobalLimits(max_model_requests=cap))
    graph = ReviewOrchestrator(snapshot, model_factory=factory, approved_synthetic=True,
        specialist_limits=RunLimits(max_provider_retries=0, max_output_repairs=0),
        execution_budget=ledger)
    if cap < 3:
        with pytest.raises(ValueError): asyncio.run(graph.run(review_id=uuid4()))
        assert not models and ledger.usage().model_attempts == 0
        return
    result = asyncio.run(graph.run(review_id=uuid4()))
    assert sum(m.calls for m in models) == ledger.usage().model_attempts == 3
    assert result.status == 'partial'
    assert sum(e.usage.model_calls for e in result.specialist_results if e.usage) == 3


@pytest.mark.parametrize('cap,recovered', [(1, 0), (2, 1)])
def test_recovery_consumes_another_shared_tool_attempt(cap, recovered):
    snapshot = load_scenario(load_dataset().scenarios[0])
    ledger = ExecutionBudget(GlobalLimits(max_tool_attempts=cap))
    agent = CodeQualityAgent(snapshot, model=ToolModel(snapshot), approved_synthetic=True,
                             execution_budget=ledger, limits=RunLimits(max_provider_retries=0, max_output_repairs=0))
    result = asyncio.run(agent.run(review_id=uuid4()))
    assert result.result.status == 'partial'
    assert result.usage.tool_attempts == ledger.usage().tool_attempts == cap
    assert result.result.inspection.recovered_errors == recovered


@pytest.mark.parametrize('invalid,count', [('unknown', 1), ('duplicate', 2), ('malformed', 2)])
def test_invalid_tool_calls_cannot_bypass_global_accounting(invalid, count):
    snapshot = load_scenario(load_dataset().scenarios[0])
    ledger = ExecutionBudget(GlobalLimits(max_tool_attempts=2))
    agent = CodeQualityAgent(snapshot, model=ToolModel(snapshot, invalid=invalid), approved_synthetic=True,
                             execution_budget=ledger)
    result = asyncio.run(agent.run(review_id=uuid4()))
    assert result.usage.tool_attempts == ledger.usage().tool_attempts == count
    assert result.result.inspection.successful_attempts == 0
