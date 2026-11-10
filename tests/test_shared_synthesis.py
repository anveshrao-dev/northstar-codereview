"""Atomic owner-bound opportunities; fake execution only."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import asyncio

import pytest
from langchain_core.messages import AIMessage
from northstar_codereview.execution_budget import ExecutionBudget,JavaGlobalLimits,GlobalLimits,BudgetDenied
from northstar_codereview.agents.code_quality import CodeQualityAgent,RunLimits
from .test_code_quality import snapshot,call,READ
from .test_synthesis_gate import BoundModel


def budget():
    return ExecutionBudget(JavaGlobalLimits(),profile='java',scenario_id='java_backend')


def reserve(ledger,owners):
    return ledger.reserve_synthesis(owners)


def test_six_inspections_leave_three_protected_synthesis_requests():
    ledger=budget();owners=tuple(uuid4() for _ in range(3));permits=reserve(ledger,owners)
    for _ in range(6): ledger.reserve_model(1,1000)
    with pytest.raises(BudgetDenied): ledger.reserve_model(1,1000)
    assert ledger.reason is None
    for owner in reversed(owners): ledger.reserve_model(1,1000,owner=owner,synthesis=permits[owner])
    assert ledger.usage().model_attempts==9 and ledger.usage().unknown_usage_attempts==9


def test_concurrent_inspection_cannot_take_reserved_slots():
    ledger=budget();owners=tuple(uuid4() for _ in range(3));permits=reserve(ledger,owners)
    def attempt(_):
        try: ledger.reserve_model(1,1000);return True
        except BudgetDenied:return False
    with ThreadPoolExecutor(max_workers=12) as pool:
        assert sum(pool.map(attempt,range(30)))==6
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert len(set(pool.map(lambda owner:ledger.reserve_model(1,1000,owner=owner,synthesis=permits[owner]),owners)))==3
    assert ledger.usage().model_attempts==9


def test_owner_checks_and_double_spend_release():
    ledger=budget();a,b=uuid4(),uuid4();permits=reserve(ledger,(a,b))
    with pytest.raises(ValueError):ledger.reserve_model(1,1000,owner=b,synthesis=permits[a])
    with pytest.raises(ValueError):ledger.release_synthesis(b,permits[a])
    ledger.reserve_model(1,1000,owner=a,synthesis=permits[a])
    with pytest.raises(ValueError):ledger.reserve_model(1,1000,owner=a,synthesis=permits[a])
    assert not ledger.release_synthesis(a,permits[a])
    assert ledger.release_synthesis(b,permits[b])
    assert not ledger.release_synthesis(b,permits[b])
    with pytest.raises(ValueError):ledger.reserve_model(1,1000,owner=b,synthesis=permits[b])
    assert ledger.usage().model_attempts==1


@pytest.mark.parametrize('owners',[(),(1,),(1,1)])
def test_invalid_owners_rejected(owners):
    with pytest.raises(ValueError): reserve(budget(),owners)


def test_capacity_and_repeated_setup_rejected_without_spending():
    ledger=ExecutionBudget(GlobalLimits(max_model_requests=2))
    with pytest.raises(ValueError):reserve(ledger,tuple(uuid4() for _ in range(3)))
    reserve(ledger,(uuid4(),))
    with pytest.raises(ValueError):reserve(ledger,(uuid4(),))
    assert ledger.usage().model_attempts==0


@pytest.mark.parametrize('field,reason',[('max_input_tokens','input_reservation'),('max_output_tokens','output_reservation')])
def test_request_slot_does_not_override_token_capacity(field,reason):
    ledger=ExecutionBudget(GlobalLimits(**{field:1000}))
    owner=uuid4();permit=reserve(ledger,(owner,))[owner]
    ledger.reserve_model(1000 if field=='max_input_tokens' else 1,1000)
    with pytest.raises(BudgetDenied) as error:ledger.reserve_model(1,1000,owner=owner,synthesis=permit)
    assert error.value.reason==reason and ledger.usage().model_attempts==1
    assert ledger.release_synthesis(owner,permit)


def test_released_capacity_is_reusable_without_refunding_admitted_requests():
    ledger=budget();owners=tuple(uuid4() for _ in range(3));permits=reserve(ledger,owners)
    assert ledger.release_synthesis(owners[0],permits[owners[0]])
    for _ in range(7):ledger.reserve_model(1,1000)
    for owner in owners[1:]:ledger.reserve_model(1,1000,owner=owner,synthesis=permits[owner])
    assert ledger.usage().model_attempts==9


@pytest.mark.parametrize('finish_order',[(0,1,2),(2,0,1)])
def test_real_parallel_agents_transition_to_their_own_reserved_slots(snapshot,finish_order):
    async def scenario():
        ledger=budget();owners=tuple(uuid4() for _ in range(3));permits=reserve(ledger,owners)
        finished=[];events=[asyncio.Event() for _ in owners]
        class Investigate(BoundModel):
            def __init__(self,index):
                super().__init__()
                self.index=index
            async def ainvoke(self,messages,**kwargs):
                await asyncio.sleep(0)
                self.histories.append(list(messages))
                if self.bound:
                    return call(args=READ,ident=f'read-{len(self.histories)}')
                position=finish_order.index(self.index)
                if position:await events[finish_order[position-1]].wait()
                finished.append(self.index)
                events[self.index].set()
                return AIMessage(content='{"findings":[]}')
        agents=[]
        for index,owner in enumerate(owners):
            agent=CodeQualityAgent(snapshot,model=Investigate(index),approved_synthetic=True,
                execution_budget=ledger,limits=RunLimits(max_provider_retries=0,max_output_repairs=0))
            agent.synthesis_reservation=permits[owner]
            agents.append(agent)
        results=await asyncio.wait_for(asyncio.gather(*(a.run(review_id=uuid4(),job_id=o) for a,o in zip(agents,owners))),5)
        assert finished==list(finish_order)
        assert ledger.usage().model_attempts==9 and sum(r.usage.model_calls for r in results)==9
        assert all(r.result.synthesis=='succeeded' for r in results)
        assert sum(r.usage.tool_attempts for r in results)==6
    asyncio.run(scenario())


@pytest.mark.parametrize('mode',['natural','error','timeout','cancel'])
def test_safe_termination_releases_unused_slot_and_keeps_admitted_attempt(snapshot,mode):
    async def scenario():
        ledger=ExecutionBudget(GlobalLimits(max_model_requests=3))
        owner,other=uuid4(),uuid4();permits=reserve(ledger,(owner,other))
        entered=asyncio.Event()
        class Model(BoundModel):
            async def ainvoke(self,messages,**kwargs):
                entered.set()
                if mode=='natural':return AIMessage(content='{"findings":[]}')
                if mode=='error':raise RuntimeError('PRIVATE_SYNTHETIC_SENTINEL')
                await asyncio.Event().wait()
        agent=CodeQualityAgent(snapshot,model=Model(),approved_synthetic=True,execution_budget=ledger,
            limits=RunLimits(max_provider_retries=0,max_output_repairs=0,request_timeout_seconds=.01 if mode=='timeout' else 30))
        agent.synthesis_reservation=permits[owner]
        task=asyncio.create_task(agent.run(review_id=uuid4(),job_id=owner))
        if mode=='cancel':
            await asyncio.wait_for(entered.wait(),5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
        else:
            result=await asyncio.wait_for(task,5)
            assert result.result.status=='partial' and 'PRIVATE_SYNTHETIC_SENTINEL' not in result.result.model_dump_json()
        assert ledger.usage().model_attempts==ledger.usage().unknown_usage_attempts==1
        assert not ledger.release_synthesis(owner,permits[owner])
        ledger.reserve_model(1,1)  # Released slot is available, the other owner is still protected.
        ledger.reserve_model(1,1,owner=other,synthesis=permits[other])
        assert ledger.usage().model_attempts==3
    asyncio.run(scenario())


@pytest.mark.parametrize('response',[AIMessage(content='invalid'),RuntimeError('PRIVATE_SYNTHETIC_SENTINEL')])
def test_failed_synthesis_remains_charged_without_retry(snapshot,response):
    ledger=ExecutionBudget(GlobalLimits(max_model_requests=2));owner=uuid4();permit=reserve(ledger,(owner,))[owner]
    agent=CodeQualityAgent(snapshot,model=BoundModel(call(args=READ),response),approved_synthetic=True,execution_budget=ledger)
    agent.synthesis_reservation=permit
    result=asyncio.run(agent.run(review_id=uuid4(),job_id=owner))
    assert result.result.synthesis in ('malformed_output','request_failed') and result.result.status=='partial'
    assert result.usage.model_calls==ledger.usage().model_attempts==2
    assert result.usage.retries==result.usage.repairs==0
    assert not ledger.release_synthesis(owner,permit)
    assert 'PRIVATE_SYNTHETIC_SENTINEL' not in result.result.model_dump_json()


def test_live_configuration_rejected_before_credentials_or_provider():
    from northstar_codereview import live_evaluation as live
    from .test_live_evaluation import NoEnvironmentReads
    result=asyncio.run(live.run_live_evaluation(allow_live=True,limits=GlobalLimits(max_model_requests=2),
        environ=NoEnvironmentReads()))
    assert result.reason=='configuration_error' and not result.started and result.budget_usage.model_attempts==0


def test_orchestrator_reserves_before_factories_and_releases_initialization_failure():
    from northstar_codereview.orchestrator import ReviewOrchestrator
    from northstar_codereview.evaluation_data import load_dataset,load_scenario
    snapshot=load_scenario(next(s for s in load_dataset().scenarios if s.id=='java_backend'))
    ledger=budget();seen=[]
    def factory(name):
        seen.append(name)
        if name=='security':raise RuntimeError('PRIVATE_FACTORY_SENTINEL')
        return BoundModel(AIMessage(content='{"findings":[]}'))
    result=asyncio.run(ReviewOrchestrator(snapshot,model_factory=factory,approved_synthetic=True,execution_budget=ledger).run(review_id=uuid4()))
    assert len(seen)==3 and result.status=='partial' and ledger.usage().model_attempts==2
    # All unused holds closed, including the branch which never constructed an agent.
    for _ in range(7):ledger.reserve_model(1,1)
    assert ledger.usage().model_attempts==9
    assert 'PRIVATE_FACTORY_SENTINEL' not in result.model_dump_json()


def test_orchestrator_rejects_insufficient_slots_before_factory():
    from northstar_codereview.orchestrator import ReviewOrchestrator
    from northstar_codereview.evaluation_data import load_dataset,load_scenario
    snapshot=load_scenario(next(s for s in load_dataset().scenarios if s.id=='java_backend'))
    ledger=ExecutionBudget(GlobalLimits(max_model_requests=2))
    def factory(name):pytest.fail('Factory ran before reservation validation')
    with pytest.raises(ValueError):
        asyncio.run(ReviewOrchestrator(snapshot,model_factory=factory,approved_synthetic=True,execution_budget=ledger).run(review_id=uuid4()))
    assert ledger.usage().model_attempts==0


@pytest.mark.parametrize('mode',['cancel_dispatch','timeout_branch','shared_model'])
def test_orchestrator_cleanup_closes_all_holds(mode,monkeypatch):
    from northstar_codereview.orchestrator import ReviewOrchestrator
    from northstar_codereview.agents.security import SecurityAgent
    from northstar_codereview.evaluation_data import load_dataset,load_scenario
    snapshot=load_scenario(next(s for s in load_dataset().scenarios if s.id=='java_backend'))
    ledger=budget()
    shared=BoundModel(AIMessage(content='{"findings":[]}'))
    if mode=='timeout_branch':
        async def timeout(self,**kwargs):raise TimeoutError
        monkeypatch.setattr(SecurityAgent,'run',timeout)
    def factory(name):
        if mode=='cancel_dispatch' and name=='security':raise asyncio.CancelledError
        return shared if mode=='shared_model' else BoundModel(AIMessage(content='{"findings":[]}'))
    graph=ReviewOrchestrator(snapshot,model_factory=factory,approved_synthetic=True,execution_budget=ledger)
    if mode=='cancel_dispatch':
        from langgraph.errors import NodeCancelledError
        with pytest.raises(NodeCancelledError):asyncio.run(graph.run(review_id=uuid4()))
    else:
        result=asyncio.run(graph.run(review_id=uuid4()))
        assert result.status in ('partial','failed')
    charged=ledger.usage().model_attempts
    for _ in range(9-charged):ledger.reserve_model(1,1)
    assert ledger.usage().model_attempts==9


def test_natural_complete_review_releases_unspent_synthesis(snapshot):
    ledger=budget();owner=uuid4();permit=reserve(ledger,(owner,))[owner]
    agent=CodeQualityAgent(snapshot,model=BoundModel(call(args=READ),AIMessage(content='{"findings":[]}')),
        approved_synthetic=True,execution_budget=ledger)
    agent.synthesis_reservation=permit
    result=asyncio.run(agent.run(review_id=uuid4(),job_id=owner))
    assert result.result.status=='completed' and result.result.synthesis=='not_started'
    assert ledger.usage().model_attempts==2 and not ledger.release_synthesis(owner,permit)
    for _ in range(7):ledger.reserve_model(1,1)


def test_cancellation_after_synthesis_admission_never_refunds(snapshot):
    async def scenario():
        ledger=ExecutionBudget(GlobalLimits(max_model_requests=2));owner=uuid4();permit=reserve(ledger,(owner,))[owner]
        entered=asyncio.Event()
        class Model(BoundModel):
            async def ainvoke(self,messages,**kwargs):
                if self.bound:return call(args=READ)
                entered.set()
                await asyncio.Event().wait()
        agent=CodeQualityAgent(snapshot,model=Model(),approved_synthetic=True,execution_budget=ledger)
        agent.synthesis_reservation=permit
        task=asyncio.create_task(agent.run(review_id=uuid4(),job_id=owner))
        await asyncio.wait_for(entered.wait(),5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert ledger.usage().model_attempts==ledger.usage().unknown_usage_attempts==2
        assert not ledger.release_synthesis(owner,permit)
        with pytest.raises(BudgetDenied):ledger.reserve_model(1,1)
    asyncio.run(scenario())


def test_concurrent_duplicate_spending_admits_once():
    ledger=budget();owner=uuid4();permit=reserve(ledger,(owner,))[owner]
    def spend(_):
        try:ledger.reserve_model(1,1,owner=owner,synthesis=permit);return True
        except ValueError:return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(spend,range(32)))==1
    assert ledger.usage().model_attempts==1


def test_foreign_ledger_permit_is_rejected():
    first,second=budget(),budget();owner=uuid4()
    foreign=reserve(first,(owner,))[owner]
    reserve(second,(owner,))
    with pytest.raises(ValueError):second.reserve_model(1,1,owner=owner,synthesis=foreign)
    assert second.usage().model_attempts==0


def test_outer_caller_cancellation_releases_unspent_holds():
    from northstar_codereview.orchestrator import ReviewOrchestrator
    from northstar_codereview.evaluation_data import load_dataset,load_scenario
    snapshot=load_scenario(next(s for s in load_dataset().scenarios if s.id=='java_backend'))
    async def scenario():
        ledger=budget();entered=asyncio.Event()
        class Model(BoundModel):
            async def ainvoke(self,messages,**kwargs):
                entered.set()
                await asyncio.Event().wait()
        graph=ReviewOrchestrator(snapshot,model_factory=lambda name:Model(),approved_synthetic=True,execution_budget=ledger)
        task=asyncio.create_task(graph.run(review_id=uuid4()))
        await asyncio.wait_for(entered.wait(),5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        charged=ledger.usage().model_attempts
        assert 1<=charged<=3 and ledger.usage().unknown_usage_attempts==charged
        for _ in range(9-charged):ledger.reserve_model(1,1)
        assert ledger.usage().model_attempts==9
    asyncio.run(scenario())
