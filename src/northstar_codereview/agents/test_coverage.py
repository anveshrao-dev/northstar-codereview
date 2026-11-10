"""Static test-adequacy policy over the existing bounded specialist graph.

No test runner, numerical coverage calculation, provider startup, or filesystem
discovery outside captured source. Reuse the graph's model/tool/parse/coverage
nodes without changing Code Quality or Security. Only domain policy and trusted
result attribution differ. Source-inspection coverage is not measured test coverage.
"""

from importlib.resources import files
from typing import cast
from uuid import UUID

from .code_quality import ChatModel, CodeQualityAgent, CodeQualityRun, RunLimits
from ..execution_budget import ExecutionBudget
from ..local_source import CollectedSnapshot
from ..real_source_contracts import TransmissionApproval
from ..models import AgentName, Category, SpecialistResult


_SCOPE_LIMITATION = (
    "Static snapshot review only: repository-wide test inventory, test execution results, "
    "numerical coverage, and behavioral completeness are unverified."
)

from ..proposal_validation import validate_observed_proposal


class TestCoverageRun(CodeQualityRun):
    """Test-review result; internal events contain source, not safe log payloads.

    Validated citations do not prove a gap, test success, or behavioral adequacy.
    """


class TestCoverageAgent(CodeQualityAgent):
    def __init__(self, snapshot: CollectedSnapshot, *, model: ChatModel,
                 approved_synthetic: bool = False, limits: RunLimits | None = None,
                 execution_budget: ExecutionBudget | None = None,
                 transmission_approval: TransmissionApproval | None = None,
                 prepared_context: bool | None = None):
        super().__init__(snapshot, model=model, approved_synthetic=approved_synthetic, limits=limits,
                         execution_budget=execution_budget, transmission_approval=transmission_approval,
                         prepared_context=prepared_context)
        # Fixed trusted package resource, never selected by model or repository.
        # The inherited prepare node reads this policy when the run begins.
        self.skill = files("northstar_codereview").joinpath("skills/test-coverage.md").read_text(encoding="utf-8")

    async def run(self, *, review_id: UUID, job_id: UUID | None = None) -> TestCoverageRun:
        # Both successful and timed-out execution use this subclass's finalizer.
        return cast(TestCoverageRun, await super().run(review_id=review_id, job_id=job_id))

    async def _validate(self, state):
        session = state["session"]
        for index, data in enumerate(session.proposals, 1):
            validated, diagnostic = validate_observed_proposal(
                data, self.snapshot, session.observed,
                categories={Category.TESTS}, agent_name=AgentName.TEST_COVERAGE)
            if diagnostic is not None:
                session.limitations.append(f"Proposal {index} rejected: {diagnostic}.")
            elif not any(item.finding_id == validated.finding_id for item in session.validated):
                session.validated.append(validated)
        return {"session": session}

    async def _finalize(self, state):
        shared = (await super()._finalize(state))["output"]
        # Retain execution status. This scope caveat also belongs on a completed
        # static review: reading every captured line cannot establish repository
        # completeness, test success, or numerical/behavioral coverage.
        result = SpecialistResult.model_validate({
            **shared.result.model_dump(),
            "agent_name": AgentName.TEST_COVERAGE,
            "limitations": tuple(dict.fromkeys((*shared.result.limitations, _SCOPE_LIMITATION))),
        })
        return {"output": TestCoverageRun(result=result, validated_findings=shared.validated_findings,
                                          usage=shared.usage, events=shared.events)}
