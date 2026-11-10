"""Static Jenkins/CI-CD policy over the established bounded specialist graph.

No Jenkinsfile, Groovy, shell, build, scan, or deployment execution. No provider
startup or outer routing changes. Model/tool/parse nodes and limits are inherited.
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
    "Static snapshot review only: shared-library/plugin configuration, Groovy/Jenkins validation, "
    "build/test/scan execution, and deployment outcomes are unverified."
)

from ..proposal_validation import validate_observed_proposal


class CICDGroovyRun(CodeQualityRun):
    """CI/CD findings with shared usage and internal source-bearing observations.

    Valid citations do not prove pipeline correctness or runtime security. Internal
    events are not sanitized logging or public report payloads.
    """


class CICDGroovyAgent(CodeQualityAgent):
    def __init__(self, snapshot: CollectedSnapshot, *, model: ChatModel,
                 approved_synthetic: bool = False, limits: RunLimits | None = None,
                 execution_budget: ExecutionBudget | None = None,
                 transmission_approval: TransmissionApproval | None = None,
                 prepared_context: bool | None = None):
        super().__init__(snapshot, model=model, approved_synthetic=approved_synthetic, limits=limits,
                         execution_budget=execution_budget, transmission_approval=transmission_approval,
                         prepared_context=prepared_context)
        # Fixed application resource; repository content cannot select the policy.
        # The inherited prepare node reads this skill when execution begins.
        self.skill = files("northstar_codereview").joinpath("skills/jenkins-groovy-review.md").read_text(encoding="utf-8")

    async def run(self, *, review_id: UUID, job_id: UUID | None = None) -> CICDGroovyRun:
        return cast(CICDGroovyRun, await super().run(review_id=review_id, job_id=job_id))

    async def _validate(self, state):
        session = state["session"]
        for index, data in enumerate(session.proposals, 1):
            validated, diagnostic = validate_observed_proposal(
                data, self.snapshot, session.observed,
                categories={Category.CICD}, agent_name=AgentName.CICD_GROOVY)
            if diagnostic is not None:
                session.limitations.append(f"Proposal {index} rejected: {diagnostic}.")
            elif not any(item.finding_id == validated.finding_id for item in session.validated):
                session.validated.append(validated)
        return {"session": session}

    async def _finalize(self, state):
        shared = (await super()._finalize(state))["output"]
        # Keep execution status and limitations. Static completion cannot certify
        # compilation, plugin behavior, successful gates, or deployment outcomes.
        result = SpecialistResult.model_validate({
            **shared.result.model_dump(), "agent_name": AgentName.CICD_GROOVY,
            "limitations": tuple(dict.fromkeys((*shared.result.limitations, _STATIC_SCOPE))),
        })
        return {"output": CICDGroovyRun(result=result, validated_findings=shared.validated_findings,
                                        usage=shared.usage, events=shared.events)}
