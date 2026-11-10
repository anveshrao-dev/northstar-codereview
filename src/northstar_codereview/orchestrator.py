"""Deterministic, bounded orchestration of isolated specialist subgraphs.

No provider startup, source execution, publishing, or LLM report synthesis. Factories
are trusted application code: supply fresh, promptly constructed model contexts
with provider retries already bounded. A filename is a routing hint, not a claim
of language support. No durable/checkpoint or cross-process delivery is provided.
"""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Literal, TypedDict
from uuid import UUID, uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langsmith import tracing_context
from pydantic import Field

from .agents.code_quality import ChatModel, CodeQualityAgent, CodeQualityRun, RunLimits, Usage
from .agents.security import SecurityAgent
from .agents.test_coverage import TestCoverageAgent
from .agents.gitops import GitOpsAgent
from .agents.cicd_groovy import CICDGroovyAgent
from .local_source import CollectedSnapshot
from .real_source_contracts import TransmissionApproval, IntakeRejected, FailureCategory
from .real_source_authorization import authorize_tools, require_transmission
from .transmission_claims import orchestration_execution, require_execution
from .execution_budget import ExecutionBudget
from .models import (AgentName, Category, Contract, FilePath, SourceSnapshot, SpecialistResult,
                     Text, ValidatedFinding, validate_finding)
from .routing import platform_applicability

if TYPE_CHECKING:
    from .reporting import ReviewReport


@dataclass(frozen=True)
class SpecialistSpec:
    """Trusted registration point for future reviewed specialist implementations."""
    name: AgentName
    agent_type: type[CodeQualityAgent]
    suffixes: frozenset[str]
    categories: frozenset[Category]


_APPLICATION = frozenset({".py", ".java", ".ts", ".tsx", ".js", ".jsx"})
_CONFIGURATION = frozenset({".yaml", ".yml", ".json", ".toml", ".tf", ".tfvars", ".xml", ".ini", ".cfg", ".properties"})
# Registry order is also canonical output order; completion order never changes it.
SPECIALIST_SPECS = (
    SpecialistSpec(AgentName.CODE_QUALITY, CodeQualityAgent, _APPLICATION, frozenset({Category.CORRECTNESS, Category.MAINTAINABILITY})),
    SpecialistSpec(AgentName.SECURITY, SecurityAgent, _APPLICATION | _CONFIGURATION, frozenset({Category.SECURITY})),
    SpecialistSpec(AgentName.TEST_COVERAGE, TestCoverageAgent, _APPLICATION, frozenset({Category.TESTS})),
    SpecialistSpec(AgentName.GITOPS, GitOpsAgent, frozenset(), frozenset({Category.GITOPS})),
    SpecialistSpec(AgentName.CICD_GROOVY, CICDGroovyAgent, frozenset(), frozenset({Category.CICD})),
)


class RoutingDecision(Contract):
    selected_specialists: tuple[AgentName, ...]
    unsupported_files: tuple[FilePath, ...]
    uncertain_files: tuple[FilePath, ...] = ()
    limitations: tuple[Text, ...] = ()


def select_specialists(snapshot: CollectedSnapshot) -> RoutingDecision:
    paths = sorted({entry.file_path for entry in snapshot.manifest.files})
    suffixes = {PurePosixPath(path).suffix.lower() for path in paths}
    supported = frozenset().union(*(spec.suffixes for spec in SPECIALIST_SPECS))
    platform = platform_applicability(snapshot)
    selected = {spec.name for spec in SPECIALIST_SPECS if spec.suffixes & suffixes}
    if platform.gitops_files:
        selected.add(AgentName.GITOPS)
    if platform.cicd_files:
        selected.add(AgentName.CICD_GROOVY)
    return RoutingDecision(
        selected_specialists=tuple(spec.name for spec in SPECIALIST_SPECS if spec.name in selected),
        unsupported_files=tuple(path for path in paths if PurePosixPath(path).suffix.lower() not in supported
                                and path not in platform.gitops_files | platform.cicd_files),
        uncertain_files=platform.uncertain_files,
        limitations=platform.limitations,
    )


class OrchestrationLimits(Contract):
    # The specialist retains its own shorter request/run deadlines. This outer
    # boundary catches an integration that raises or fails to finish its wrapper.
    specialist_timeout_seconds: float = Field(default=125, gt=0, le=125, allow_inf_nan=False)


class SpecialistFailure(Contract):
    agent_name: AgentName
    job_id: UUID
    category: Literal["initialization_failed", "shared_model_context", "exception", "timeout",
                      "invalid_output", "missing_result", "conflicting_duplicate", "specialist_failed"]


class SpecialistExecution(Contract):
    result: SpecialistResult
    validated_findings: tuple[ValidatedFinding, ...] = ()
    usage: Usage | None = None  # None means unavailable, not zero consumption.
    failure: SpecialistFailure | None = None


class SpecialistDelivery(Contract):
    agent_name: AgentName
    execution: SpecialistExecution


def merge_deliveries(left: tuple[SpecialistDelivery, ...], right: tuple[SpecialistDelivery, ...]) -> tuple[SpecialistDelivery, ...]:
    """Append without mutation or overwrite; collection validates/deduplicates.

    Ordering is intentionally insignificant. Unlike last-writer-wins, a conflict
    remains visible and can invalidate only its specialist at the join boundary.
    """
    return left + right


class OrchestrationResult(Contract):
    review_id: UUID
    snapshot_id: UUID
    selected_specialists: tuple[AgentName, ...]
    specialist_results: tuple[SpecialistExecution, ...]
    validated_findings: tuple[ValidatedFinding, ...]
    limitations: tuple[Text, ...]
    failures: tuple[SpecialistFailure, ...]
    status: Literal["completed", "partial", "failed", "skipped"]


class OrchestrationState(TypedDict, total=False):
    review_id: UUID
    snapshot_id: UUID
    selected_specialists: tuple[AgentName, ...]
    routing: RoutingDecision
    deliveries: Annotated[tuple[SpecialistDelivery, ...], merge_deliveries]
    specialist_results: tuple[SpecialistExecution, ...]
    validated_findings: tuple[ValidatedFinding, ...]
    limitations: tuple[Text, ...]
    failures: tuple[SpecialistFailure, ...]
    status: Literal["completed", "partial", "failed", "skipped"]
    output: OrchestrationResult


@dataclass(frozen=True)
class _ReviewContext:
    review_id: UUID
    snapshot: CollectedSnapshot
    jobs: Mapping[AgentName, UUID]
    # Only dispatch writes these maps, before the parallel superstep. Branches
    # read their own handle; no agent/model/messages are stored in public state.
    executors: dict[AgentName, CodeQualityAgent] = field(default_factory=dict)
    preparation_failures: dict[AgentName, SpecialistExecution] = field(default_factory=dict)
    synthesis_reservations: dict[UUID, object] = field(default_factory=dict)


class ReviewOrchestrator:
    def __init__(self, snapshot: CollectedSnapshot, *, model_factory: Callable[[AgentName], ChatModel],
                 approved_synthetic: bool = False, specialist_limits: RunLimits | None = None,
                 orchestration_limits: OrchestrationLimits | None = None,
                 execution_budget: ExecutionBudget | None = None,
                 transmission_approval: TransmissionApproval | None = None):
        if snapshot.real_source is not None:
            if approved_synthetic:
                raise IntakeRejected(FailureCategory.AUTHORIZATION)
            require_transmission(snapshot, transmission_approval)
        elif transmission_approval is not None:
            authorize_tools(snapshot, approved_synthetic=approved_synthetic, transmission_approval=transmission_approval)
        elif approved_synthetic is not True:
            raise ValueError("Orchestration requires explicit approved-synthetic source opt-in")
        self._real_context = None
        self.snapshot = snapshot
        self.model_factory = model_factory
        self.transmission_approval = transmission_approval
        self.specialist_limits = specialist_limits or (RunLimits(max_provider_retries=0, max_output_repairs=0)
                                                       if snapshot.real_source else RunLimits())
        if snapshot.real_source and (self.specialist_limits.max_provider_retries or self.specialist_limits.max_output_repairs):
            raise IntakeRejected(FailureCategory.AUTHORIZATION)
        self.orchestration_limits = orchestration_limits or OrchestrationLimits()
        self.execution_budget = execution_budget
        builder = StateGraph(OrchestrationState, context_schema=_ReviewContext)
        for name in ("validate_context", "route", "dispatch", "collect", "finalize"):
            builder.add_node(name, getattr(self, f"_{name}"))
        for spec in SPECIALIST_SPECS:
            # Bind name at construction; avoid Python's late-bound loop closure.
            def branch(name):
                async def execute(state: OrchestrationState, runtime: Runtime[_ReviewContext]):
                    return await self._execute(name, state, runtime)
                return execute
            builder.add_node(spec.name.value, branch(spec.name))
            builder.add_edge("dispatch", spec.name.value)
        builder.add_edge(START, "validate_context")
        builder.add_edge("validate_context", "route")
        builder.add_edge("route", "dispatch")
        # Explicit barrier waits for all branches, including cheap skipped ones.
        builder.add_edge([spec.name.value for spec in SPECIALIST_SPECS], "collect")
        builder.add_edge("collect", "finalize")
        builder.add_edge("finalize", END)
        self.graph = builder.compile()  # In-memory only, no checkpoints/retries.

    async def run(self, *, review_id: UUID) -> OrchestrationResult:
        if not isinstance(review_id, UUID):
            raise ValueError("Invalid trusted review context")
        context = _ReviewContext(review_id, self.snapshot,
                                 MappingProxyType({spec.name: uuid4() for spec in SPECIALIST_SPECS}))
        selected = select_specialists(self.snapshot).selected_specialists if self.snapshot.real_source else ()
        with orchestration_execution(self.snapshot, self.transmission_approval,
                                     review_id=review_id, jobs={name: context.jobs[name] for name in selected}):
            self._real_context = context
            try:
                return await self._run_claimed(context)
            finally:
                self._real_context = None

    async def _run_claimed(self, context):
        review_id = context.review_id
        with tracing_context(enabled=False):
            try:
                state = await self.graph.ainvoke(
                    {"review_id": review_id, "snapshot_id": self.snapshot.manifest.snapshot_id, "deliveries": ()},
                    context=context, config={"callbacks": [], "recursion_limit": 20, "max_concurrency": len(SPECIALIST_SPECS)},
                )
            finally:
                for owner, permit in context.synthesis_reservations.items():
                    self.execution_budget.release_synthesis(owner, permit)
        return state["output"]

    async def run_report(self, *, review_id: UUID, report_revision: int = 1) -> "ReviewReport":
        """Run once and project the collected result using the existing reporter.

        Rendering remains explicit via render_json/render_markdown. The local
        import keeps the reporting layer's typed input dependency acyclic.
        """
        from .reporting import build_report
        # Reject invalid presentation input before spending model/tool budgets.
        if type(report_revision) is not int or report_revision < 1:
            raise ValueError("Invalid report revision")
        result = await self.run(review_id=review_id)
        return build_report(result, snapshot=self.snapshot.manifest, report_revision=report_revision)

    async def _validate_context(self, state: OrchestrationState, runtime: Runtime[_ReviewContext]):
        ctx = runtime.context
        if self.snapshot.real_source is not None:
            require_execution(self.snapshot, self.transmission_approval, run_id=ctx.review_id)
            if ctx is not self._real_context:
                raise IntakeRejected(FailureCategory.AUTHORIZATION)
        try:
            manifest = SourceSnapshot.model_validate(ctx.snapshot.manifest.model_dump())
            if state["review_id"] != ctx.review_id or state["snapshot_id"] != manifest.snapshot_id:
                raise ValueError
            expected = {(entry.side, entry.file_path) for entry in manifest.files}
            if expected != set(ctx.snapshot.sources) or len(set(ctx.jobs.values())) != len(SPECIALIST_SPECS):
                raise ValueError
            for entry in manifest.files:
                text = ctx.snapshot.sources[(entry.side, entry.file_path)]
                if sha256(text.encode("utf-8")).hexdigest() != entry.sha256:
                    raise ValueError
        except Exception:
            raise ValueError("Invalid trusted review context") from None
        return {}

    async def _route(self, state: OrchestrationState, runtime: Runtime[_ReviewContext]):
        decision = select_specialists(runtime.context.snapshot)
        return {"routing": decision, "selected_specialists": decision.selected_specialists}

    def _failure(self, name: AgentName, ctx: _ReviewContext, category: str) -> SpecialistExecution:
        failure = SpecialistFailure(agent_name=name, job_id=ctx.jobs[name], category=category)
        result = SpecialistResult(review_id=ctx.review_id, snapshot_id=ctx.snapshot.manifest.snapshot_id,
                                  job_id=ctx.jobs[name], agent_name=name, status="failed",
                                  limitations=(f"Specialist failed ({category}); details withheld and usage may be unavailable.",))
        return SpecialistExecution(result=result, failure=failure)

    async def _dispatch(self, state: OrchestrationState, runtime: Runtime[_ReviewContext]):
        ctx = runtime.context
        if ctx.snapshot.real_source is not None:
            selected = select_specialists(ctx.snapshot).selected_specialists
            if state['selected_specialists'] != selected:
                raise IntakeRejected(FailureCategory.AUTHORIZATION)
            require_execution(ctx.snapshot, self.transmission_approval, run_id=ctx.review_id, specialists=selected)
        contexts = []  # Retain references until duplicate-identity checks finish.
        if self.execution_budget is not None and state["selected_specialists"]:
            ctx.synthesis_reservations.update(self.execution_budget.reserve_synthesis(
                tuple(ctx.jobs[name] for name in state["selected_specialists"])))
        for spec in SPECIALIST_SPECS:
            if spec.name not in state["selected_specialists"]:
                continue
            try:
                if ctx.snapshot.real_source is not None:
                    require_execution(ctx.snapshot, self.transmission_approval,
                                      run_id=ctx.review_id, specialists=(spec.name,))
                model = self.model_factory(spec.name)
                contexts.append((spec.name, model))
                options = {"execution_budget": self.execution_budget} if self.execution_budget is not None else {}
                if ctx.snapshot.real_source is not None:
                    options["transmission_approval"] = self.transmission_approval
                executor = spec.agent_type(ctx.snapshot, model=model, approved_synthetic=ctx.snapshot.real_source is None,
                                           limits=self.specialist_limits, **options)
                executor.synthesis_reservation = ctx.synthesis_reservations.get(ctx.jobs[spec.name])
                contexts.append((spec.name, executor.model))
                ctx.executors[spec.name] = executor
            except Exception:
                ctx.preparation_failures[spec.name] = self._failure(spec.name, ctx, "initialization_failed")
        for name, model in contexts:
            if any(other_name != name and other is model for other_name, other in contexts):
                ctx.preparation_failures[name] = self._failure(name, ctx, "shared_model_context")
        for name in ctx.preparation_failures:
            if permit := ctx.synthesis_reservations.get(ctx.jobs[name]):
                self.execution_budget.release_synthesis(ctx.jobs[name], permit)
        return {}

    def _validate_execution(self, execution: SpecialistExecution, name: AgentName, ctx: _ReviewContext) -> SpecialistExecution:
        result = SpecialistResult.model_validate(execution.result.model_dump())
        if (result.review_id, result.snapshot_id, result.job_id, result.agent_name) != (
            ctx.review_id, ctx.snapshot.manifest.snapshot_id, ctx.jobs[name], name
        ):
            raise ValueError
        if not set(result.reviewed_files) <= {entry.file_path for entry in ctx.snapshot.manifest.files}:
            raise ValueError
        spec = next(spec for spec in SPECIALIST_SPECS if spec.name == name)
        if any(proposal.category not in spec.categories for proposal in result.findings):
            raise ValueError
        canonical = tuple(validate_finding(proposal, ctx.snapshot.manifest, ctx.snapshot.sources, agent_name=name)
                          for proposal in result.findings)
        if canonical != execution.validated_findings or len({item.finding_id for item in canonical}) != len(canonical):
            raise ValueError
        usage = execution.usage
        if usage is not None:
            usage = Usage.model_validate(usage.model_dump())
            if any(type(value) is not int or value < 0 for value in usage.model_dump().values()):
                raise ValueError
            caps = {"model_calls": "max_model_calls", "tool_rounds": "max_tool_rounds", "tool_attempts": "max_tool_attempts",
                    "retries": "max_provider_retries", "repairs": "max_output_repairs"}
            if any(getattr(usage, key) > getattr(self.specialist_limits, cap) for key, cap in caps.items()):
                raise ValueError
            if usage.usage_unknown_calls > usage.model_calls:
                raise ValueError
            if result.inspection is not None:
                counts = result.inspection
                if (counts.successful_attempts + counts.recovered_errors + counts.unresolved_errors > usage.tool_attempts
                        or counts.recovery_attempts > usage.tool_attempts):
                    raise ValueError
        elif result.status not in {"failed", "skipped"}:
            raise ValueError
        failure = execution.failure
        if failure is not None:
            failure = SpecialistFailure.model_validate(failure.model_dump())
            if failure.agent_name != name or failure.job_id != ctx.jobs[name] or result.status != "failed":
                raise ValueError
        elif result.status == "failed":
            failure = SpecialistFailure(agent_name=name, job_id=ctx.jobs[name], category="specialist_failed")
        return SpecialistExecution(result=result, validated_findings=canonical, usage=usage, failure=failure)

    async def _execute(self, name: AgentName, state: OrchestrationState, runtime: Runtime[_ReviewContext]):
        ctx = runtime.context
        try:
            return await self._execute_reserved(name, state, runtime)
        finally:
            if permit := ctx.synthesis_reservations.get(ctx.jobs[name]):
                self.execution_budget.release_synthesis(ctx.jobs[name], permit)

    async def _execute_reserved(self, name: AgentName, state: OrchestrationState, runtime: Runtime[_ReviewContext]):
        ctx = runtime.context
        if name not in state["selected_specialists"]:
            execution = SpecialistExecution(result=SpecialistResult(
                review_id=ctx.review_id, snapshot_id=ctx.snapshot.manifest.snapshot_id, job_id=ctx.jobs[name],
                agent_name=name, status="skipped", limitations=("Not selected by the current file-type applicability policy.",)))
        elif name in ctx.preparation_failures:
            execution = ctx.preparation_failures[name]
        else:
            try:
                async with asyncio.timeout(self.orchestration_limits.specialist_timeout_seconds):
                    run = await ctx.executors[name].run(review_id=ctx.review_id, job_id=ctx.jobs[name])
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise  # Caller/framework cancellation must still stop the run.
                execution = self._failure(name, ctx, "exception")
            except TimeoutError:
                execution = self._failure(name, ctx, "timeout")
            except Exception:
                execution = self._failure(name, ctx, "exception")
            else:
                try:
                    if not isinstance(run, CodeQualityRun):
                        raise ValueError
                    execution = self._validate_execution(SpecialistExecution(
                        result=run.result, validated_findings=run.validated_findings, usage=run.usage), name, ctx)
                    # Preserve the specialist's observed-evidence boundary, too.
                    observed = [ref for event in run.events if event.observation.status == "ok" for ref in event.observation.evidence]
                    for item in execution.validated_findings:
                        for ref in item.proposal.evidence:
                            if not any(ref.snapshot_id == seen.snapshot_id and ref.file_path == seen.file_path and ref.side == seen.side
                                       and seen.start_line <= ref.start_line <= ref.end_line <= seen.end_line for seen in observed):
                                raise ValueError
                except Exception:
                    execution = self._failure(name, ctx, "invalid_output")
        return {"deliveries": (SpecialistDelivery(agent_name=name, execution=execution),)}

    async def _collect(self, state: OrchestrationState, runtime: Runtime[_ReviewContext]):
        ctx = runtime.context
        await self._validate_context(state, runtime)
        decision = select_specialists(ctx.snapshot)
        if state["selected_specialists"] != decision.selected_specialists or state["routing"] != decision:
            raise ValueError("Invalid trusted routing context")
        executions = []
        for spec in SPECIALIST_SPECS:
            candidates = [delivery.execution for delivery in state["deliveries"] if delivery.agent_name == spec.name]
            if not candidates:
                execution = self._failure(spec.name, ctx, "missing_result")
            elif any(candidate != candidates[0] for candidate in candidates[1:]):
                execution = self._failure(spec.name, ctx, "conflicting_duplicate")
            else:
                try:
                    execution = self._validate_execution(candidates[0], spec.name, ctx)
                    if spec.name not in decision.selected_specialists and execution.result.status != "skipped":
                        raise ValueError
                except Exception:
                    execution = self._failure(spec.name, ctx, "invalid_output")
            executions.append(execution)
        limitations = ["Static path/document routing is an applicability hint, not proof of deployed technology, full language support or semantic review completeness."]
        limitations.extend(decision.limitations)
        if decision.unsupported_files:
            limitations.append("Unsupported file types are present; the current routing policy does not cover the entire snapshot.")
        if any(delivery.agent_name not in {spec.name for spec in SPECIALIST_SPECS} for delivery in state["deliveries"]):
            limitations.append("Unexpected specialist delivery rejected; orchestration consistency is incomplete.")
        for execution in executions:
            # Preserve valid maximum-length notes without adding a prefix that
            # violates Text's bound. Attribution remains in specialist_results.
            limitations.extend(execution.result.limitations)
        selected = [item for item in executions if item.result.agent_name in decision.selected_specialists]
        failures = tuple(item.failure for item in executions if item.failure is not None)
        if not selected:
            status = "skipped" if not failures else "failed"
        elif all(item.result.status == "failed" for item in selected):
            status = "failed"
        elif failures or decision.unsupported_files or any(item.result.status != "completed" for item in selected):
            status = "partial"
        else:
            status = "completed"
        if any(delivery.agent_name not in {spec.name for spec in SPECIALIST_SPECS} for delivery in state["deliveries"]):
            status = "partial"
        return {"specialist_results": tuple(executions),
                "validated_findings": tuple(item for execution in executions for item in execution.validated_findings),
                "failures": failures, "limitations": tuple(dict.fromkeys(limitations)), "status": status}

    async def _finalize(self, state: OrchestrationState, runtime: Runtime[_ReviewContext]):
        return {"output": OrchestrationResult(**{key: state[key] for key in OrchestrationResult.model_fields})}
