"""Immutable execution authority. Names never infer a sample or source scope."""
from types import MappingProxyType
from typing import Literal

from pydantic import Field, model_validator

from .models import Contract

ProfileName = Literal['standard', 'extended_csv', 'extended_bm25']
_VALUES = MappingProxyType({'standard': (6, 80000, 60), 'extended_csv': (9, 80000, 70),
                           'extended_bm25': (11, 90000, 90)})
PROFILE_NAMES = tuple(_VALUES)


class ExecutionProfile(Contract):
    name: ProfileName
    max_model_requests: Literal[4] = 4
    max_tool_attempts: int = Field(strict=True)
    max_input_tokens: int = Field(strict=True)
    max_output_tokens: Literal[4000] = 4000
    evaluation_timeout_seconds: int = Field(strict=True)
    request_timeout_seconds: Literal[20] = 20
    output_tokens_per_call: Literal[1000] = 1000
    max_provider_retries: Literal[0] = 0
    max_output_repairs: Literal[0] = 0
    synthesis_input_hold: Literal[30000] = 30000

    @model_validator(mode='after')
    def exact_registry_values(self):
        if (self.max_tool_attempts,self.max_input_tokens,self.evaluation_timeout_seconds) != _VALUES[self.name]:
            raise ValueError('Profile limits do not match registry')
        return self

    def global_limits(self):
        from .execution_budget import GlobalLimits, ProfileGlobalLimits
        cls=GlobalLimits if self.name=='standard' else ProfileGlobalLimits
        return cls(**{k:getattr(self,k) for k in GlobalLimits.model_fields})

    def agent_limits(self):
        from .agents.code_quality import RunLimits, ProfileRunLimits
        cls=RunLimits if self.name=='standard' else ProfileRunLimits
        return cls(max_model_calls=self.max_model_requests,max_tool_rounds=3,
            max_tool_attempts=self.max_tool_attempts,max_provider_retries=0,max_output_repairs=0,
            max_input_tokens=self.max_input_tokens,max_output_tokens=self.max_output_tokens,
            output_tokens_per_call=self.output_tokens_per_call,request_timeout_seconds=self.request_timeout_seconds,
            run_timeout_seconds=self.evaluation_timeout_seconds)


def get_profile(name: str = 'standard') -> ExecutionProfile:
    if not isinstance(name,str) or name not in _VALUES:
        raise ValueError('Unknown execution profile')
    tools,inputs,deadline=_VALUES[name]
    return ExecutionProfile(name=name,max_tool_attempts=tools,max_input_tokens=inputs,
                            evaluation_timeout_seconds=deadline)


def checked_profile(value) -> ExecutionProfile:
    """Revalidate copied/constructed models at authority boundaries."""
    return ExecutionProfile.model_validate(value.model_dump()) if isinstance(value,ExecutionProfile) else get_profile(value)


def approval_profile(approval) -> ExecutionProfile:
    return get_profile() if approval.execution_profile is None else checked_profile(approval.execution_profile)
