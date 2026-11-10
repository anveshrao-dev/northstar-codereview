"""Shared admission ledger: reserve before work, never refund or enlarge caps.

The lock covers checks and increments with no await between them. Reservations
bound application admission, not provider billing or remote cancellation.
"""

from threading import Lock
from time import monotonic
from typing import Literal
from uuid import UUID

from pydantic import Field

from .models import Contract


BudgetReason = Literal['model_requests', 'tool_attempts', 'input_reservation', 'output_reservation',
                       'reported_token_overrun', 'deadline', 'cancelled', 'synthesis_input_allowance']


class GlobalLimits(Contract):
    max_model_requests: int = Field(default=6, strict=True, ge=1, le=6)
    max_tool_attempts: int = Field(default=12, strict=True, ge=1, le=12)
    max_input_tokens: int = Field(default=120_000, strict=True, ge=1, le=120_000)
    max_output_tokens: int = Field(default=6_000, strict=True, ge=1, le=6_000)
    output_tokens_per_call: int = Field(default=1_000, strict=True, ge=1, le=1_000)
    request_timeout_seconds: float = Field(default=20, gt=0, le=20, allow_inf_nan=False)
    evaluation_timeout_seconds: float = Field(default=60, gt=0, le=60, allow_inf_nan=False)


class ProfileGlobalLimits(GlobalLimits):
    """Named real-review opt-in only; GlobalLimits defaults stay unchanged."""
    evaluation_timeout_seconds: float = Field(default=60, gt=0, le=90, allow_inf_nan=False)


class JavaGlobalLimits(GlobalLimits):
    """Explicit Java-only policy; does not enlarge GlobalLimits defaults."""
    max_model_requests: int = Field(default=9, strict=True, ge=1, le=9)
    max_tool_attempts: int = Field(default=15, strict=True, ge=1, le=15)
    max_input_tokens: int = Field(default=180_000, strict=True, ge=1, le=180_000)
    max_output_tokens: int = Field(default=9_000, strict=True, ge=1, le=9_000)


BudgetProfile = Literal['default', 'java']


def select_limits(profile: BudgetProfile, scenario_id: str | None, limits=None):
    """Never infer permission from a larger limits object or its subclass."""
    if profile == 'default':
        schema = GlobalLimits
    elif profile == 'java' and scenario_id == 'java_backend':
        schema = JavaGlobalLimits
    else:
        raise ValueError('Invalid budget profile or scenario')
    return schema.model_validate(limits.model_dump() if limits is not None else {})


class BudgetUsage(Contract):
    model_attempts: int = Field(ge=0)
    tool_attempts: int = Field(ge=0)
    input_reserved: int = Field(ge=0)
    output_reserved: int = Field(ge=0)
    reported_input_tokens: int = Field(ge=0)
    reported_output_tokens: int = Field(ge=0)
    unknown_usage_attempts: int = Field(ge=0)


class BudgetDenied(RuntimeError):
    def __init__(self, reason: BudgetReason):
        self.reason = reason
        super().__init__(f'Global execution budget exhausted ({reason}); review incomplete.')


class InspectionReserved(BudgetDenied):
    """Normal phase transition: inspection must yield protected capacity."""


class ExecutionBudget:
    def __init__(self, limits: GlobalLimits, *, clock=monotonic,
                 profile: BudgetProfile = 'default', scenario_id: str | None = None, execution_profile=None):
        self.execution_profile = None
        if execution_profile is not None:
            from .execution_profiles import checked_profile
            bound=checked_profile(execution_profile)
            if profile!='default' or scenario_id is not None or limits.model_dump()!=bound.global_limits().model_dump():
                raise ValueError('Execution profile limits mismatch')
            self.execution_profile=bound
            self._limits=bound.global_limits()
        else:
            self._limits = select_limits(profile, scenario_id, limits)
        self._clock = clock
        self._deadline = clock() + self._limits.evaluation_timeout_seconds
        self._lock = Lock()
        self._reason: BudgetReason | None = None
        self._tickets: dict[int, tuple[int, int]] = {}
        self._settled: set[int] = set()
        self._tools = self._input = self._output = self._reported_in = self._reported_out = 0
        self._synthesis: dict[object, tuple[UUID, str]] = {}
        self._synthesis_input: dict[object, int] = {}
        self._reservation_setup = False

    @property
    def limits(self):
        return self._limits

    @property
    def reason(self):
        with self._lock:
            return self._reason

    def remaining(self):
        return max(0.0, self._deadline - self._clock())

    def stop(self, reason: BudgetReason):
        with self._lock:
            if self._reason is None:
                self._reason = reason

    def _check(self):
        if self._clock() >= self._deadline and self._reason is None:
            self._reason = 'deadline'
        if self._reason is not None:
            raise BudgetDenied(self._reason)

    def _deny(self, reason):
        self._reason = reason
        raise BudgetDenied(reason)

    def reserve_synthesis(self, owners: tuple[UUID, ...], *, input_units: int = 0) -> dict[UUID, object]:
        """Reserve slots and optional per-owner input before dispatch.

        Zero preserves the original slot-only policy. A positive hold is also
        that owner's synthesis input ceiling; it never grows implicitly.
        Holds are not charges: only admitted model requests enter usage totals.
        """
        if not owners or any(not isinstance(owner, UUID) for owner in owners) or len(set(owners)) != len(owners):
            raise ValueError('Invalid synthesis reservation owners')
        if type(input_units) is not int or input_units < 0:
            raise ValueError('Invalid synthesis input reservation')
        with self._lock:
            self._check()
            if (self._reservation_setup or self._tickets or len(owners) > self.limits.max_model_requests
                    or input_units * len(owners) > self.limits.max_input_tokens):
                raise ValueError('Invalid synthesis reservation configuration')
            permits = {owner: object() for owner in owners}
            self._synthesis = {permit: (owner, 'reserved') for owner, permit in permits.items()}
            self._synthesis_input = {permit: input_units for permit in permits.values()}
            self._reservation_setup = True
            return permits

    def _owned(self, owner: UUID, permit: object) -> str:
        record = self._synthesis.get(permit)
        if record is None or record[0] != owner:
            raise ValueError('Invalid synthesis reservation owner')
        return record[1]

    def release_synthesis(self, owner: UUID, permit: object) -> bool:
        """Idempotent cleanup; never refund admitted attempts or tokens."""
        with self._lock:
            if self._owned(owner, permit) != 'reserved':
                return False
            self._synthesis[permit] = (owner, 'released')
            return True

    def synthesis_input_allowance(self, owner: UUID, permit: object) -> int:
        """Read an active owner's allowance; admission still checks under lock."""
        with self._lock:
            return self._synthesis_input[permit] if self._owned(owner, permit) == 'reserved' else 0

    def _model_denied(self, reason):
        # A large estimate must not poison another owner's usable reservation.
        if self._reservation_setup:
            raise BudgetDenied(reason)
        self._deny(reason)

    def reserve_model(self, input_units: int, output_tokens: int, *, owner: UUID | None = None,
                      synthesis: object | None = None) -> int:
        if any(type(n) is not int or n <= 0 for n in (input_units, output_tokens)):
            raise ValueError('Invalid reservation')
        with self._lock:
            self._check()
            if synthesis is not None and self._owned(owner, synthesis) != 'reserved':
                raise ValueError('Synthesis reservation already closed')
            own_input = self._synthesis_input[synthesis] if synthesis is not None else 0
            if own_input and input_units > own_input:
                self._model_denied('synthesis_input_allowance')
            held = sum(state == 'reserved' for _, state in self._synthesis.values())
            held_input = sum(self._synthesis_input[permit] for permit, (_, state) in self._synthesis.items()
                             if state == 'reserved') - own_input
            if synthesis is None and held and len(self._tickets) + held >= self.limits.max_model_requests:
                raise InspectionReserved('model_requests')
            if len(self._tickets) >= self.limits.max_model_requests:
                self._model_denied('model_requests')
            if self._input + input_units + held_input > self.limits.max_input_tokens:
                if synthesis is None and held:
                    raise InspectionReserved('input_reservation')
                self._model_denied('input_reservation')
            if (self._output + output_tokens > self.limits.max_output_tokens
                    or output_tokens > self.limits.output_tokens_per_call):
                if synthesis is None and held:
                    raise InspectionReserved('output_reservation')
                self._model_denied('output_reservation')
            ticket = len(self._tickets)
            self._tickets[ticket] = (input_units, output_tokens)
            self._input += input_units
            self._output += output_tokens
            if synthesis is not None:
                self._synthesis[synthesis] = (owner, 'spent')
            return ticket

    def reserve_tool(self):
        with self._lock:
            self._check()
            if self._tools >= self.limits.max_tool_attempts:
                self._deny('tool_attempts')
            self._tools += 1

    def record_usage(self, ticket: int, used_input: int, used_output: int):
        with self._lock:
            if (ticket not in self._tickets or ticket in self._settled
                    or any(type(n) is not int or n < 0 for n in (used_input, used_output))):
                raise ValueError('Invalid usage record')
            self._settled.add(ticket)
            self._reported_in += used_input
            self._reported_out += used_output
            reserved_in, reserved_out = self._tickets[ticket]
            if (used_input > reserved_in or used_output > reserved_out) and self._reason is None:
                self._reason = 'reported_token_overrun'

    def usage(self):
        with self._lock:
            return BudgetUsage(model_attempts=len(self._tickets), tool_attempts=self._tools,
                input_reserved=self._input, output_reserved=self._output,
                reported_input_tokens=self._reported_in, reported_output_tokens=self._reported_out,
                unknown_usage_attempts=len(self._tickets) - len(self._settled))
