"""Production input holds: real graph and ledger, offline fake providers only."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from northstar_codereview import gitops_slice as live
from northstar_codereview.agents.gitops import GitOpsAgent
from northstar_codereview.execution_budget import ExecutionBudget, GlobalLimits, BudgetDenied, InspectionReserved
from .test_gitops_token_capacity import Investigate, READS, DISCOVERY, METADATA
from .test_synthesis_gate import BoundModel
from .test_code_quality import call
from .test_gitops_slice import ENV, READ


def held(*, cap=80_000, amount=30_000, owners=1):
    ledger = ExecutionBudget(GlobalLimits(max_input_tokens=cap))
    ids = tuple(uuid4() for _ in range(owners))
    permits = ledger.reserve_synthesis(ids, input_units=amount)
    return ledger, ids, permits


def test_input_hold_initialization_spend_release_and_no_refunds():
    ledger, (owner,), permits = held()
    permit = permits[owner]
    assert ledger.synthesis_input_allowance(owner, permit) == 30_000
    assert ledger.usage().input_reserved == 0  # Holds are not admitted requests.
    ticket = ledger.reserve_model(50_000, 1000)
    ledger.record_usage(ticket, 1, 1)
    with pytest.raises(InspectionReserved):
        ledger.reserve_model(1, 1000)
    synthesis = ledger.reserve_model(20_000, 1000, owner=owner, synthesis=permit)
    assert synthesis != ticket and ledger.usage().input_reserved == 70_000
    assert ledger.synthesis_input_allowance(owner, permit) == 0
    assert not ledger.release_synthesis(owner, permit)
    with pytest.raises(ValueError):
        ledger.reserve_model(1, 1, owner=owner, synthesis=permit)
    ledger.reserve_model(10_000, 1000)  # Only the unspent 10k became available.
    assert ledger.usage().input_reserved == 80_000


@pytest.mark.parametrize('amount', [-1, True, 1.5, '30000', 80_001])
def test_invalid_input_hold_is_rejected_atomically(amount):
    ledger = ExecutionBudget(GlobalLimits(max_input_tokens=80_000))
    owner = uuid4()
    with pytest.raises(ValueError):
        ledger.reserve_synthesis((owner,), input_units=amount)
    assert ledger.usage().model_attempts == 0
    assert ledger.reserve_synthesis((owner,), input_units=30_000)


def test_aggregate_holds_cannot_exceed_total():
    ledger = ExecutionBudget(GlobalLimits(max_input_tokens=80_000))
    with pytest.raises(ValueError):
        ledger.reserve_synthesis(tuple(uuid4() for _ in range(3)), input_units=30_000)
    assert ledger.usage().input_reserved == 0


def test_concurrent_admission_protects_every_owner():
    ledger, owners, permits = held(owners=2)
    def inspect(_):
        try:
            return ledger.reserve_model(10_000, 1000)
        except BudgetDenied:
            return None
    with ThreadPoolExecutor(max_workers=12) as pool:
        assert sum(t is not None for t in pool.map(inspect, range(40))) == 2
    with pytest.raises(ValueError):
        ledger.reserve_model(1, 1, owner=owners[0], synthesis=permits[owners[1]])
    with ThreadPoolExecutor(max_workers=2) as pool:
        tickets = list(pool.map(lambda o: ledger.reserve_model(30_000, 1000, owner=o, synthesis=permits[o]), owners))
    assert len(set(tickets)) == 2 and ledger.usage().input_reserved == 80_000


def test_oversized_synthesis_cannot_expand_its_hold_even_with_free_capacity():
    ledger, (owner,), permits = held()
    with pytest.raises(BudgetDenied) as error:
        ledger.reserve_model(30_001, 1000, owner=owner, synthesis=permits[owner])
    assert error.value.reason == 'synthesis_input_allowance'
    assert ledger.usage().model_attempts == 0
    assert ledger.release_synthesis(owner, permits[owner])
    assert not ledger.release_synthesis(owner, permits[owner])


@pytest.mark.parametrize('sequence', [READS, DISCOVERY, METADATA])
def test_live_slice_yields_to_synthesis_with_exact_evidence_and_full_replay(sequence):
    model = Investigate(sequence, proposals=True)
    async def close(*args): pass
    model.__aexit__ = close
    output = asyncio.run(live.run_slice(allow_live=True, environ=ENV, provider_factory=lambda *_: model))
    assert output.reason == 'completed'
    assert output.report.specialists[0].synthesis == 'succeeded'
    assert output.budget_usage.model_attempts == 3 and output.budget_usage.tool_attempts == 2
    assert output.budget_usage.input_reserved < 80_000
    assert len(output.report.findings) == output.evaluation.quality.pending == 1
    assert output.configuration['synthesis_input_hold'] == 30_000
    history = model.histories[-1]
    assistants = [m for m in history if isinstance(m, AIMessage)]
    observations = [m for m in history if isinstance(m, ToolMessage)]
    assert [m.tool_calls[0]['id'] for m in assistants] == [m.tool_call_id for m in observations]
    prior = model.histories[-2]
    assert all(any(item is m for item in history) for m in prior)


@pytest.mark.parametrize('large', [20_000, 70_000])
def test_oversized_conversation_fails_closed_without_followup_or_compaction(large):
    class Verbose(Investigate):
        async def ainvoke(self, messages, **kwargs):
            reply = await super().ainvoke(messages, **kwargs)
            return reply.model_copy(update={'content': 'x' * large})
    model = Verbose(READS)
    output = asyncio.run(live.run_slice(allow_live=True, environ=ENV, provider_factory=lambda *_: model))
    assert output.reason == 'partial' and output.budget_usage.model_attempts == 1
    assert output.report.specialists[0].synthesis == 'budget_denied'
    assert any('synthesis_input_allowance' in item for item in output.report.limitations)
    assert not output.report.findings and len(model.histories) == 1
    assert 'x' * 50 not in live.render_slice(output)


@pytest.mark.parametrize('mode', ['early', 'failure', 'timeout', 'cancel'])
def test_terminal_paths_release_unspent_hold_without_refunding_admission(mode):
    async def exercise():
        ledger, (owner,), permits = held()
        permit = permits[owner]
        entered = asyncio.Event()
        class Model(BoundModel):
            async def ainvoke(self, messages, **kwargs):
                entered.set()
                if mode == 'early': return AIMessage(content='{"findings":[]}')
                if mode == 'failure': raise RuntimeError('PRIVATE synthetic')
                await asyncio.Event().wait()
        limits = live.specialist_limits(live.SliceLimits()).model_copy(update={'request_timeout_seconds': .01 if mode == 'timeout' else 20})
        agent = GitOpsAgent(live.load_fixture()[0], model=Model(), approved_synthetic=True, limits=limits, execution_budget=ledger)
        agent.synthesis_reservation = permit
        task = asyncio.create_task(agent.run(review_id=uuid4(), job_id=owner))
        await asyncio.wait_for(entered.wait(), 5)
        if mode == 'cancel':
            ledger.stop('cancelled')
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        else:
            result = await task
            assert result.result.status == 'partial' and 'PRIVATE' not in result.result.model_dump_json()
        assert ledger.synthesis_input_allowance(owner, permit) == 0
        assert not ledger.release_synthesis(owner, permit)
        assert ledger.usage().model_attempts == ledger.usage().unknown_usage_attempts == 1
        assert ledger.usage().input_reserved > 0
    asyncio.run(exercise())


def test_default_slot_only_behavior_and_other_profiles_unchanged():
    from northstar_codereview.execution_budget import JavaGlobalLimits
    ledger = ExecutionBudget(GlobalLimits())
    owner = uuid4()
    permit = ledger.reserve_synthesis((owner,))[owner]
    assert ledger.synthesis_input_allowance(owner, permit) == 0
    ledger.reserve_model(90_000, 1000)
    ledger.reserve_model(30_000, 1000, owner=owner, synthesis=permit)
    assert GlobalLimits().max_input_tokens == 120_000
    assert JavaGlobalLimits().max_input_tokens == 180_000
    assert live.SliceLimits().max_input_tokens == 80_000


def test_invalid_hold_configuration_stops_before_provider(monkeypatch):
    def reject(self, owners, *, input_units=0):
        assert input_units == 30_000
        raise ValueError('Invalid reservation configuration')
    monkeypatch.setattr(live.ExecutionBudget, 'reserve_synthesis', reject)
    calls = []
    output = asyncio.run(live.run_slice(allow_live=True, environ=ENV, provider_factory=lambda *_: calls.append(True)))
    assert not calls and not output.started and output.reason == 'configuration_error'


def test_concurrent_synthesis_cannot_spend_same_hold_twice():
    ledger, (owner,), permits = held()
    def spend(_):
        try:
            return ledger.reserve_model(30_000, 1000, owner=owner, synthesis=permits[owner])
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(t is not None for t in pool.map(spend, range(20))) == 1
    assert ledger.usage().model_attempts == 1 and ledger.usage().input_reserved == 30_000


def test_deadline_rejects_hold_spending_but_cleanup_can_release():
    now = [0.0]
    ledger = ExecutionBudget(GlobalLimits(evaluation_timeout_seconds=1), clock=lambda: now[0])
    owner = uuid4()
    permit = ledger.reserve_synthesis((owner,), input_units=30_000)[owner]
    ledger.reserve_model(100, 1000)
    now[0] = 1.0
    with pytest.raises(BudgetDenied) as caught:
        ledger.reserve_model(10_000, 1000, owner=owner, synthesis=permit)
    assert caught.value.reason == 'deadline'
    assert ledger.release_synthesis(owner, permit)
    assert ledger.usage().input_reserved == 100


def test_local_limit_yields_before_global_limit_without_discarding_history():
    ledger, (owner,), permits = held()
    model = Investigate(READS)
    # Local capacity is smaller than global: hold must affect both checks.
    limits = live.specialist_limits(live.SliceLimits()).model_copy(update={'max_input_tokens': 40_000})
    agent = GitOpsAgent(live.load_fixture()[0], model=model, approved_synthetic=True, limits=limits, execution_budget=ledger)
    agent.synthesis_reservation = permits[owner]
    result = asyncio.run(agent.run(review_id=uuid4(), job_id=owner))
    assert result.result.synthesis == 'succeeded' and result.result.status == 'partial'
    assert ledger.usage().model_attempts == 1 and ledger.usage().tool_attempts == 0
    assert not result.result.reviewed_files


def test_input_hold_does_not_bypass_output_ceiling():
    ledger = ExecutionBudget(GlobalLimits(max_output_tokens=1000))
    owner = uuid4()
    permit = ledger.reserve_synthesis((owner,), input_units=30_000)[owner]
    ledger.reserve_model(1, 1000)
    with pytest.raises(BudgetDenied) as caught:
        ledger.reserve_model(10_000, 1000, owner=owner, synthesis=permit)
    assert caught.value.reason == 'output_reservation'
    assert ledger.usage().model_attempts == 1 and ledger.usage().output_reserved == 1000


def test_real_adapter_accepts_full_replay_after_input_boundary_transition():
    import httpx
    from .test_nebius import completion
    requests = []
    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if 'response_format' not in payload:
            return httpx.Response(200, json=completion({'content': None, 'tool_calls': [
                {'id': f'read-{len(requests)}', 'type': 'function',
                 'function': {'name': 'read_source_lines', 'arguments': json.dumps(READ)}}]}, usage=False))
        return httpx.Response(200, json=completion({'content': '{"findings":[]}'}, usage=False))
    def factory(settings, timeout):
        return live.NebiusModel(settings, allow_live=True, timeout_seconds=timeout,
                                transport=httpx.MockTransport(respond))
    output = asyncio.run(live.run_slice(allow_live=True, environ=ENV, provider_factory=factory))
    assert output.reason == 'completed' and len(requests) == 3
    assert all(len(p['tools']) == 4 and 'response_format' not in p for p in requests[:-1])
    final = requests[-1]
    assert final['response_format'] == {'type': 'json_object'} and 'tools' not in final
    assert [m['tool_calls'][0]['id'] for m in final['messages'] if m['role'] == 'assistant'] == ['read-1', 'read-2']
    assert [m['tool_call_id'] for m in final['messages'] if m['role'] == 'tool'] == ['read-1', 'read-2']
    assert output.budget_usage.model_attempts == 3 and output.budget_usage.tool_attempts == 2
