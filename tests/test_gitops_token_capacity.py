"""Offline policy experiment, not a production budget or context change.

Fake models exercise the real graph/tools/validator. Estimates are UTF-8 byte
reservations; no fake provider usage is supplied. The hold and compaction below
are test-only alternatives, deliberately absent from the live runner.
"""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from northstar_codereview.agents.code_quality import input_reservation
from northstar_codereview.agents.gitops import GitOpsAgent
from northstar_codereview.execution_budget import (
    BudgetDenied, ExecutionBudget, GlobalLimits, InspectionReserved,
)
from northstar_codereview.gitops_slice import SliceLimits, load_fixture, specialist_limits
from .test_code_quality import call
from .test_gitops_slice import READ
from .test_synthesis_gate import BoundModel


class HeldInputExperiment(ExecutionBudget):
    """Serialize hold-check + existing admission for this isolated experiment.

    A fixed hold protects at most that many units, not arbitrary future history.
    Production adoption needs an explicit estimate-over-hold policy and local
    budget integration; this is not exposed through the application CLI.
    """
    def __init__(self, limits, hold=30_000):
        super().__init__(limits)
        self.hold = hold
        self.holds = {}
        self.admission = Lock()

    def reserve_synthesis(self, owners):
        with self.admission:
            if len(owners) * self.hold > self.limits.max_input_tokens:
                raise ValueError('Holds exceed capacity')
            permits = super().reserve_synthesis(owners)
            self.holds = {permit: self.hold for permit in permits.values()}
            return permits

    def reserve_model(self, input_units, output_tokens, *, owner=None, synthesis=None):
        with self.admission:
            own = 0
            if synthesis is not None:
                if self._owned(owner, synthesis) != 'reserved':
                    raise ValueError('Closed hold')
                own = self.holds[synthesis]
                if input_units > own:
                    raise BudgetDenied('input_reservation')
            unavailable = sum(self.holds.values()) - own
            if self.usage().input_reserved + input_units + unavailable > self.limits.max_input_tokens:
                if synthesis is None:
                    raise InspectionReserved('input_reservation')
                raise BudgetDenied('input_reservation')
            ticket = super().reserve_model(input_units, output_tokens, owner=owner, synthesis=synthesis)
            if synthesis is not None:
                del self.holds[synthesis]
            return ticket

    def release_synthesis(self, owner, permit):
        with self.admission:
            released = super().release_synthesis(owner, permit)
            if released:
                del self.holds[permit]
            return released


def without_discovery_pairs(messages):
    """Hypothesis only: remove whole, empty-assistant, no-evidence exchanges.

    Preserve source observations, IDs from retained exchanges, instructions and
    assistant text in these synthetic histories. Not provider-compatible: the
    real adapter requires every previous tool-calling assistant to be replayed.
    No summarizer or label access.
    """
    kept = []
    index = 0
    while index < len(messages):
        message = messages[index]
        following = messages[index + 1] if index + 1 < len(messages) else None
        if (isinstance(message, AIMessage) and message.content == ''
                and not message.additional_kwargs and not message.invalid_tool_calls
                and len(message.tool_calls) == 1 and isinstance(following, ToolMessage)):
            tool = message.tool_calls[0]
            observation = json.loads(following.content)
            if (tool['name'] in ('list_snapshot_files', 'get_source_metadata')
                    and tool['id'] == following.tool_call_id
                    and observation['status'] == 'ok' and not observation['evidence']):
                index += 2
                continue
        kept.append(message)
        index += 1
    return kept


class MeasuredAgent(GitOpsAgent):
    compact = False

    async def _model(self, state):
        session = state['session']
        if session.synthesizing:
            self.before_synthesis = list(session.messages)
            if self.compact:
                session.messages = without_discovery_pairs(session.messages)
            self.after_synthesis = list(session.messages)
        before = session.model_calls
        amount = input_reservation(session.messages, [] if session.synthesizing else self.schemas)
        result = await super()._model(state)
        admitted = session.model_calls > before
        if admitted or session.synthesis == 'budget_denied' or session.route == 'synthesize':
            self.estimates.append(('synthesis' if session.synthesizing else 'inspection', amount, admitted))
        return result


def final_finding(messages):
    # Construct only from actual synthetic tool observations, not dataset labels.
    observation = next(json.loads(m.content) for m in reversed(messages)
                       if isinstance(m, ToolMessage) and json.loads(m.content)['evidence'])
    proposal = {
        'title': 'Service does not select the captured pods', 'category': 'gitops',
        'severity': 'medium', 'confidence': .9,
        'explanation': 'The Service selector differs from the captured pod template labels.',
        'suggested_remediation': 'Confirm the intended workload and align its selector.',
        'assumptions': ['Only the captured resources are assessed.'],
        'evidence': observation['evidence'],
    }
    return AIMessage(content=json.dumps({'findings': [proposal]}))


class Investigate(BoundModel):
    def __init__(self, sequence, *, proposals=False):
        super().__init__()
        self.sequence = iter(sequence)
        self.proposals = proposals

    async def ainvoke(self, messages, **kwargs):
        self.histories.append(list(messages))
        if not self.bound:
            return final_finding(messages) if self.proposals else AIMessage(content='{"findings":[]}')
        name = next(self.sequence)
        args = READ if name == 'read_source_lines' else ({'file_path': 'resources.yaml'} if name == 'get_source_metadata' else {})
        return call(name, args=args, ident=f'call-{len(self.histories)-1}')


READS = ('read_source_lines',) * 3
DISCOVERY = ('list_snapshot_files', 'read_source_lines', 'get_source_metadata')
METADATA = ('get_source_metadata', 'read_source_lines', 'read_source_lines')


def simulate(sequence=READS, *, cap=80_000, hold=None, compact=False, calls=4, proposals=False):
    # Only test objects use larger caps; SliceLimits and live runner stay fixed.
    global_limits = GlobalLimits(max_model_requests=calls, max_tool_attempts=6,
                                 max_input_tokens=cap, max_output_tokens=4000)
    ledger = ExecutionBudget(global_limits) if hold is None else HeldInputExperiment(global_limits, hold)
    owner = uuid4()
    permit = ledger.reserve_synthesis((owner,))[owner]
    model = Investigate(sequence, proposals=proposals)
    limits = specialist_limits(SliceLimits()).model_copy(update={'max_input_tokens': cap, 'max_model_calls': calls})
    agent = MeasuredAgent(load_fixture()[0], model=model, approved_synthetic=True,
                          execution_budget=ledger, limits=limits)
    agent.estimates = []
    agent.compact = compact
    agent.synthesis_reservation = permit
    result = asyncio.run(agent.run(review_id=uuid4(), job_id=owner))
    return agent, model, ledger, result


@pytest.mark.parametrize('sequence', [READS, DISCOVERY, METADATA])
def test_three_inspections_exhaust_input_before_synthesis(sequence):
    agent, model, ledger, result = simulate(sequence)
    assert result.result.synthesis == 'budget_denied' and result.result.status == 'partial'
    assert ledger.usage().model_attempts == len(model.histories) == 3
    assert result.usage.tool_attempts == 3 and result.result.reviewed_files == ('resources.yaml',)
    assert not any('source lines were not inspected' in note for note in result.result.limitations)
    projected = next(amount for phase, amount, admitted in agent.estimates if phase == 'synthesis' and not admitted)
    assert ledger.usage().input_reserved + projected > 80_000
    assert ledger.usage().reported_input_tokens == ledger.usage().reported_output_tokens == 0
    assert ledger.usage().unknown_usage_attempts == 3  # No invented provider usage.


@pytest.mark.parametrize('sequence', [READS, DISCOVERY, METADATA])
def test_hypothetical_larger_cap_completes_scripted_four_request_flow(sequence):
    _, model, ledger, result = simulate(sequence, cap=100_000, proposals=True)
    assert result.result.synthesis == 'succeeded' and len(result.validated_findings) == 1
    assert ledger.usage().model_attempts == len(model.histories) == 4
    assert 80_000 < ledger.usage().input_reserved <= 100_000
    assert SliceLimits().max_input_tokens == 80_000


@pytest.mark.parametrize('sequence', [READS, DISCOVERY, METADATA])
def test_hold_yields_early_with_replay_and_source_evidence_preserved(sequence):
    agent, model, ledger, result = simulate(sequence, hold=30_000, proposals=True)
    assert result.result.synthesis == 'succeeded' and result.result.status == 'completed'
    assert ledger.usage().model_attempts == 3 and ledger.usage().tool_attempts == 2
    assert ledger.usage().input_reserved <= 80_000 and not ledger.holds
    assert len(result.validated_findings) == 1
    # Same message objects, evidence, call IDs and observation IDs: no compaction.
    assert all(a is b for a, b in zip(agent.before_synthesis, agent.after_synthesis, strict=True))
    assert sum(len(m.tool_calls) for m in model.histories[-1] if isinstance(m, AIMessage)) == 2
    assert [m.tool_calls[0]['id'] for m in model.histories[-1] if isinstance(m, AIMessage)] == [
        m.tool_call_id for m in model.histories[-1] if isinstance(m, ToolMessage)]


def test_efficient_read_and_synthesis_fit_existing_ceiling():
    _, _, ledger, result = simulate(('read_source_lines',), calls=2, proposals=True)
    assert result.result.synthesis == 'succeeded' and len(result.validated_findings) == 1
    assert ledger.usage().input_reserved < 40_000


def test_discovery_only_hold_does_not_fabricate_source_coverage():
    _, _, ledger, result = simulate(('list_snapshot_files',)*3, hold=30_000)
    assert result.result.synthesis == 'succeeded' and result.result.status == 'partial'
    assert not result.result.reviewed_files and not result.validated_findings
    assert ledger.usage().input_reserved <= 80_000


@pytest.mark.parametrize('sequence,expected', [(READS,'budget_denied'), (DISCOVERY,'succeeded')])
def test_experimental_pair_compaction_is_not_guaranteed_to_fit(sequence,expected):
    agent, _, ledger, result = simulate(sequence, compact=True)
    before = input_reservation(agent.before_synthesis, [])
    after = input_reservation(agent.after_synthesis, [])
    assert after <= before
    source_before = [m for m in agent.before_synthesis if isinstance(m, ToolMessage) and json.loads(m.content)['evidence']]
    source_after = [m for m in agent.after_synthesis if isinstance(m, ToolMessage) and json.loads(m.content)['evidence']]
    assert all(a is b for a, b in zip(source_before, source_after, strict=True))
    # The current lossless duplicate-text reduction runs before this old,
    # test-only pair-removal experiment. Combined, DISCOVERY now fits, while
    # READS still cannot fit. Pair removal remains incompatible with Nebius's
    # assistant replay guard and is never used in the production workflow.
    assert result.result.synthesis == expected
    if expected == 'budget_denied':
        assert ledger.usage().input_reserved + after > 80_000
        assert ledger.usage().model_attempts == 3
    else:
        assert ledger.usage().input_reserved <= 80_000
        assert ledger.usage().model_attempts == 4


def test_holds_are_atomic_and_other_jobs_cannot_spend_them():
    ledger = HeldInputExperiment(GlobalLimits(max_input_tokens=80_000), hold=30_000)
    owners = (uuid4(), uuid4())
    permits = ledger.reserve_synthesis(owners)
    def inspect(_):
        try:
            ledger.reserve_model(10_000, 1000)
            return True
        except BudgetDenied:
            return False
    with ThreadPoolExecutor(max_workers=12) as pool:
        assert sum(pool.map(inspect, range(30))) == 2
    assert ledger.usage().input_reserved == 20_000
    with pytest.raises(ValueError):
        ledger.reserve_model(30_000, 1000, owner=owners[1], synthesis=permits[owners[0]])
    with ThreadPoolExecutor(max_workers=2) as pool:
        tickets = list(pool.map(lambda owner: ledger.reserve_model(30_000, 1000, owner=owner, synthesis=permits[owner]), owners))
    assert len(set(tickets)) == 2 and ledger.usage().input_reserved == 80_000
    assert not ledger.holds


def test_hold_does_not_guarantee_unbounded_future_context():
    ledger = HeldInputExperiment(GlobalLimits(max_input_tokens=80_000))
    owner = uuid4()
    permit = ledger.reserve_synthesis((owner,))[owner]
    ledger.reserve_model(20_000, 1000)
    with pytest.raises(BudgetDenied):
        ledger.reserve_model(30_001, 1000, owner=owner, synthesis=permit)
    assert ledger.usage().input_reserved == 20_000 and ledger.usage().model_attempts == 1
    assert ledger.release_synthesis(owner, permit)
    assert ledger.usage().input_reserved == 20_000


@pytest.mark.parametrize('mode', ['failure', 'cancel'])
def test_failure_and_cancellation_release_only_unspent_hold(mode):
    async def exercise():
        ledger = HeldInputExperiment(GlobalLimits(max_input_tokens=80_000))
        owner = uuid4()
        permit = ledger.reserve_synthesis((owner,))[owner]
        entered = asyncio.Event()
        class Failing(BoundModel):
            async def ainvoke(self, messages, **kwargs):
                entered.set()
                if mode == 'failure':
                    raise RuntimeError('PRIVATE synthetic failure')
                await asyncio.Event().wait()
        agent = GitOpsAgent(load_fixture()[0], model=Failing(), approved_synthetic=True,
                            execution_budget=ledger, limits=specialist_limits(SliceLimits()))
        agent.synthesis_reservation = permit
        task = asyncio.create_task(agent.run(review_id=uuid4(), job_id=owner))
        await asyncio.wait_for(entered.wait(), 5)
        if mode == 'cancel':
            ledger.stop('cancelled')
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert 'PRIVATE' not in result.result.model_dump_json()
            assert result.usage.retries == result.usage.repairs == 0
        assert not ledger.holds
        assert ledger.usage().model_attempts == ledger.usage().unknown_usage_attempts == 1
        assert ledger.usage().input_reserved > 0
        if mode == 'cancel':
            with pytest.raises(BudgetDenied):
                ledger.reserve_model(1, 1)
    asyncio.run(exercise())


def test_estimate_counts_entire_history_and_definitions_each_time():
    agent, model, _, _ = simulate(READS)
    estimates = []
    for messages in model.histories:
        payload = {'messages': [m.model_dump(exclude={'artifact'}) for m in messages], 'tools': agent.schemas}
        expected = len(json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8')) + 1024
        assert input_reservation(messages, agent.schemas) == expected
        estimates.append(expected)
    assert estimates[0] < estimates[1] < estimates[2]
    assert agent.estimates[0][1] == estimates[0]


def test_lower_reported_usage_releases_no_admitted_input():
    ledger = HeldInputExperiment(GlobalLimits(max_input_tokens=80_000))
    owner = uuid4()
    permit = ledger.reserve_synthesis((owner,))[owner]
    ticket = ledger.reserve_model(50_000, 1000)
    ledger.record_usage(ticket, 1, 1)  # Synthetic accounting input, never provider usage measurement.
    with pytest.raises(InspectionReserved):
        ledger.reserve_model(1, 1)
    ledger.reserve_model(30_000, 1000, owner=owner, synthesis=permit)
    assert ledger.usage().input_reserved == 80_000


def test_provider_replay_guard_rejects_removed_discovery_exchange():
    import httpx
    from langchain_core.messages import HumanMessage
    from northstar_codereview.evidence_tools import build_snapshot_tools
    from northstar_codereview.providers.nebius import NebiusModel, ProviderRequestError, load_nebius_settings
    from .test_nebius import completion
    from .test_gitops_slice import ENV

    requests = []
    def respond(request):
        requests.append(True)
        return httpx.Response(200, json=completion({'content': '', 'tool_calls': [
            {'id': 'discovery', 'type': 'function',
             'function': {'name': 'list_snapshot_files', 'arguments': '{}'}}]}, usage=False))

    async def exercise():
        tools = build_snapshot_tools(load_fixture()[0], approved_synthetic=True)
        async with NebiusModel(load_nebius_settings(ENV), allow_live=True,
                               transport=httpx.MockTransport(respond)) as model:
            model.bind_tools(tools)
            history = [HumanMessage(content='Inspect the fictional snapshot.')]
            first = await model.ainvoke(history, max_tokens=1000)
            tool = next(t for t in tools if t.name == 'list_snapshot_files')
            observation = tool.invoke({**first.tool_calls[0], 'type': 'tool_call'}, config={'callbacks': []})
            history.extend((first, observation))
            model.bind_tools(())
            with pytest.raises(ProviderRequestError) as caught:
                await model.ainvoke(history[:1], max_tokens=1000, synthesis_json=True)
            assert caught.value.diagnostic.category == 'protocol_replay_mismatch'
        assert len(requests) == 1  # Compacted request rejected before even in-memory transport.
    asyncio.run(exercise())
