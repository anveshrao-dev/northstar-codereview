"""Concurrent admission is atomic, bounded and independent of provider usage."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from northstar_codereview.execution_budget import ExecutionBudget, GlobalLimits, BudgetDenied


def test_parallel_requests_cannot_exceed_shared_cap():
    ledger = ExecutionBudget(GlobalLimits(max_model_requests=3))
    def reserve(_):
        try:
            return ledger.reserve_model(10, 10)
        except BudgetDenied:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        tickets = list(pool.map(reserve, range(30)))
    assert sum(t is not None for t in tickets) == 3
    assert ledger.usage().model_attempts == ledger.usage().unknown_usage_attempts == 3
    assert ledger.reason == 'model_requests'
    with pytest.raises(BudgetDenied):
        ledger.reserve_tool()


def test_failed_or_cancelled_calls_do_not_refund_reservations():
    ledger = ExecutionBudget(GlobalLimits(max_model_requests=2))
    first = ledger.reserve_model(500, 100)
    ledger.reserve_model(600, 100)
    ledger.record_usage(first, 20, 10)
    assert ledger.usage().input_reserved == 1100
    assert ledger.usage().output_reserved == 200
    assert ledger.usage().unknown_usage_attempts == 1
    assert ledger.usage().reported_input_tokens == 20
    with pytest.raises(BudgetDenied):
        ledger.reserve_model(1, 1)


@pytest.mark.parametrize('field,value', [('max_model_requests', 7), ('max_tool_attempts', 13),
    ('max_input_tokens', 120001), ('max_output_tokens', 6001), ('output_tokens_per_call', 1001),
    ('request_timeout_seconds', 21), ('evaluation_timeout_seconds', 61), ('max_model_requests', True)])
def test_limits_cannot_be_increased(field, value):
    with pytest.raises(ValidationError):
        GlobalLimits(**{field: value})


@pytest.mark.parametrize('limits,reservation,reason', [
    ({'max_input_tokens': 10}, (11, 1), 'input_reservation'),
    ({'max_output_tokens': 10}, (1, 11), 'output_reservation'),
    ({'output_tokens_per_call': 10}, (1, 11), 'output_reservation')])
def test_reservation_rejected_before_any_request(limits, reservation, reason):
    ledger = ExecutionBudget(GlobalLimits(**limits))
    with pytest.raises(BudgetDenied):
        ledger.reserve_model(*reservation)
    assert ledger.usage().model_attempts == 0 and ledger.reason == reason


def test_tool_attempts_and_recovery_share_same_counter():
    ledger = ExecutionBudget(GlobalLimits(max_tool_attempts=2))
    ledger.reserve_tool()
    ledger.reserve_tool()  # physical recovery is another attempt
    with pytest.raises(BudgetDenied):
        ledger.reserve_tool()
    assert ledger.usage().tool_attempts == 2 and ledger.reason == 'tool_attempts'


def test_deadline_blocks_new_work_without_refunding_inflight():
    time = [0.0]
    ledger = ExecutionBudget(GlobalLimits(evaluation_timeout_seconds=1), clock=lambda: time[0])
    ledger.reserve_model(10, 10)
    time[0] = 1
    with pytest.raises(BudgetDenied):
        ledger.reserve_tool()
    assert ledger.reason == 'deadline' and ledger.usage().unknown_usage_attempts == 1


def test_reported_token_overrun_stops_future_admissions():
    ledger = ExecutionBudget(GlobalLimits())
    ticket = ledger.reserve_model(10, 10)
    ledger.record_usage(ticket, 11, 10)
    assert ledger.reason == 'reported_token_overrun'
    assert ledger.usage().input_reserved == 10  # ceilings were not increased
    with pytest.raises(BudgetDenied):
        ledger.reserve_model(10, 10)


def test_copied_limits_are_revalidated_and_duplicate_usage_rejected():
    with pytest.raises(ValidationError):
        ExecutionBudget(GlobalLimits().model_copy(update={'max_model_requests': 50}))
    ledger = ExecutionBudget(GlobalLimits())
    ticket = ledger.reserve_model(10, 10)
    ledger.record_usage(ticket, 1, 1)
    with pytest.raises(ValueError):
        ledger.record_usage(ticket, 1, 1)
