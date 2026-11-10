"""Security policy over the established bounded specialist graph.

No provider startup, source execution, scanning, or external writes. The graph,
inspection tools, limits, recovery, parsing, and coverage behavior are inherited
without changing the Code Quality specialist. Only prompt, domain validation,
and runtime attribution differ. This deliberate reuse depends on its node hooks.
"""

from importlib.resources import files
from typing import cast
from uuid import UUID

from .code_quality import ChatModel, CodeQualityAgent, CodeQualityRun, RunLimits
from ..execution_budget import ExecutionBudget
from ..local_source import CollectedSnapshot
from ..real_source_contracts import TransmissionApproval
from ..models import AgentName, Category, SpecialistResult

from ..proposal_validation import validate_observed_proposal


class SecurityRun(CodeQualityRun):
    """Security result with the shared usage and internal inspection-event contract.

    Events contain source observations; they are not a sanitized logging payload.
    Evidence validation does not establish exploitability or claim correctness.
    """


class SecurityAgent(CodeQualityAgent):
    def __init__(self, snapshot: CollectedSnapshot, *, model: ChatModel,
                 approved_synthetic: bool = False, limits: RunLimits | None = None,
                 execution_budget: ExecutionBudget | None = None,
                 transmission_approval: TransmissionApproval | None = None,
                 prepared_context: bool | None = None):
        super().__init__(snapshot, model=model, approved_synthetic=approved_synthetic, limits=limits,
                         execution_budget=execution_budget, transmission_approval=transmission_approval,
                         prepared_context=prepared_context)
        # Fixed package resource, not Markdown selected by a model or repository.
        # Preparation happens at run time, after this policy is installed.
        self.skill = files("northstar_codereview").joinpath("skills/security-review.md").read_text(encoding="utf-8")

    async def run(self, *, review_id: UUID, job_id: UUID | None = None) -> SecurityRun:
        # Both normal and timeout paths call our _finalize hook.
        return cast(SecurityRun, await super().run(review_id=review_id, job_id=job_id))

    async def _validate(self, state):
        session = state["session"]
        for index, data in enumerate(session.proposals, 1):
            validated, diagnostic = validate_observed_proposal(
                data, self.snapshot, session.observed,
                categories={Category.SECURITY}, agent_name=AgentName.SECURITY)
            if diagnostic is not None:
                session.limitations.append(f"Proposal {index} rejected: {diagnostic}.")
            elif not any(item.finding_id == validated.finding_id for item in session.validated):
                session.validated.append(validated)
        return {"session": session}

    async def _finalize(self, state):
        # Reuse coverage, limitations, usage, and event accounting. Replace the
        # trusted envelope identity before returning anything to the graph/caller.
        shared = (await super()._finalize(state))["output"]
        result = SpecialistResult.model_validate({**shared.result.model_dump(), "agent_name": AgentName.SECURITY})
        return {"output": SecurityRun(result=result, validated_findings=shared.validated_findings,
                                      usage=shared.usage, events=shared.events)}
