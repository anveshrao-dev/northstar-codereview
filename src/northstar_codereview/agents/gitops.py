"""Static GitOps review over the established bounded specialist graph.

No YAML execution, rendering, external validators, cluster access, or provider
startup. This independently callable specialist does not change outer routing.
Model/tool/parse/coverage nodes and execution limits are reused unchanged.
"""

from importlib.resources import files
from typing import cast
from uuid import UUID

from .code_quality import ChatModel, CodeQualityAgent, CodeQualityRun, RunLimits
from ..execution_budget import ExecutionBudget
from ..local_source import CollectedSnapshot
from ..real_source_contracts import TransmissionApproval
from ..models import AgentName, Category, SpecialistResult


_STATIC_SCOPE = (
    "Static snapshot review only: external resource inventory, template/build output, cluster admission, "
    "AKS runtime state, and Flux reconciliation are unverified."
)

from ..proposal_validation import validate_observed_proposal


class GitOpsRun(CodeQualityRun):
    """GitOps proposals, usage, and internal source-bearing inspection events.

    Evidence validation is not proof of a rendered configuration or runtime fault.
    Internal events are not sanitized logging or public report payloads.
    """


class GitOpsAgent(CodeQualityAgent):
    def __init__(self, snapshot: CollectedSnapshot, *, model: ChatModel,
                 approved_synthetic: bool = False, limits: RunLimits | None = None,
                 execution_budget: ExecutionBudget | None = None,
                 transmission_approval: TransmissionApproval | None = None,
                 prepared_context: bool | None = None):
        super().__init__(snapshot, model=model, approved_synthetic=approved_synthetic, limits=limits,
                         execution_budget=execution_budget, transmission_approval=transmission_approval,
                         prepared_context=prepared_context)
        # Trusted fixed package resource; the inherited prepare node reads it at
        # run time, after construction. Repository Markdown cannot replace it.
        self.skill = files("northstar_codereview").joinpath("skills/gitops-review.md").read_text(encoding="utf-8")

    async def run(self, *, review_id: UUID, job_id: UUID | None = None) -> GitOpsRun:
        return cast(GitOpsRun, await super().run(review_id=review_id, job_id=job_id))

    async def _validate(self, state):
        session = state["session"]
        for index, data in enumerate(session.proposals, 1):
            validated, diagnostic = validate_observed_proposal(
                data, self.snapshot, session.observed,
                categories={Category.GITOPS}, agent_name=AgentName.GITOPS)
            if diagnostic is not None:
                session.limitations.append(f"Proposal {index} rejected: {diagnostic}.")
            elif not any(item.finding_id == validated.finding_id for item in session.validated):
                session.validated.append(validated)
        return {"session": session}

    async def _finalize(self, state):
        shared = (await super()._finalize(state))["output"]
        # Preserve execution status. Even a completed captured-source inspection
        # cannot establish inventory completeness, rendering, or cluster behavior.
        result = SpecialistResult.model_validate({
            **shared.result.model_dump(), "agent_name": AgentName.GITOPS,
            "limitations": tuple(dict.fromkeys((*shared.result.limitations, _STATIC_SCOPE))),
        })
        return {"output": GitOpsRun(result=result, validated_findings=shared.validated_findings,
                                    usage=shared.usage, events=shared.events)}
