"""Explicit, fixed-fixture live evaluation; no automatic dotenv or provider startup."""

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Literal
from uuid import uuid4

import httpx
from langsmith import tracing_context
from pydantic import Field

from .agents.code_quality import CodeQualityAgent, CodeQualityRun, RunLimits, Usage
from .local_source import PROJECT_ROOT, IntakeLimits, SourceIntakeError, collect_local_snapshot
from .models import Contract, InspectionSummary, ReviewRequest, Severity, ToolDiagnostic, ToolRecovery, ToolStatus
from .providers.nebius import NebiusModel, ProviderConfigurationError, load_nebius_settings


SELECTED_MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"
SOURCE_DIGEST = "f5fd5a30b1e01d2752e36de18f66276ae1f0bd0c4fafc9a17785216cac3036fd"
LABELS_DIGEST = "6f78f9f8646abf684d865b770c2a4e5d08754cf6af09b86b856769cfc7e1d05b"
ToolName = Literal["list_snapshot_files", "read_source_lines", "get_evidence", "get_source_metadata", "unknown_tool"]
Limitation = Literal["model_call_budget", "tool_round_budget", "tool_attempt_budget", "token_budget", "timeout",
                     "provider_failure", "malformed_final", "invalid_tool_protocol", "proposal_rejected",
                     "incomplete_coverage", "inspection_failure", "inspection_recovered", "unknown_limitation"]


class EvaluationLimits(RunLimits):
    """Trusted caller may lower these limits; the CLI cannot enable retries."""
    max_model_calls: int = Field(default=4, strict=True, ge=1, le=4)
    max_tool_rounds: int = Field(default=3, strict=True, ge=1, le=3)
    max_tool_attempts: int = Field(default=6, strict=True, ge=1, le=6)
    max_provider_retries: int = Field(default=0, strict=True, ge=0, le=0)
    max_output_repairs: int = Field(default=0, strict=True, ge=0, le=0)
    run_timeout_seconds: float = Field(default=90, gt=0, le=90, allow_inf_nan=False)
    max_input_tokens: int = Field(default=100_000, strict=True, ge=1, le=100_000)
    max_output_tokens: int = Field(default=4_000, strict=True, ge=1, le=4_000)
    output_tokens_per_call: int = Field(default=1_000, strict=True, ge=1, le=1_000)


class SafeRange(Contract):
    file_path: Literal["inventory.py"] = "inventory.py"
    start_line: int = Field(strict=True, ge=1, le=11)
    end_line: int = Field(strict=True, ge=1, le=11)


class SafeFinding(Contract):
    category: Literal["correctness", "maintainability"]
    severity: Severity
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    evidence: tuple[SafeRange, ...]
    evidence_status: Literal["validated"] = "validated"
    claim_status: Literal["unverified"] = "unverified"


class SafeToolEvent(Contract):
    tool_name: ToolName
    status: ToolStatus
    diagnostic: ToolDiagnostic | None = None
    recovery: ToolRecovery | None = None


class EvaluationReport(Contract):
    status: Literal["NOT RUN", "COMPLETED", "PARTIAL", "FAIL"]
    reason: Literal["disabled", "configuration_error", "fixture_rejected", "review_completed", "review_partial", "run_timeout", "unexpected_error"]
    model: Literal["Qwen/Qwen3-30B-A3B-Instruct-2507"] = SELECTED_MODEL
    started: bool = False
    specialist_status: Literal["completed", "partial"] | None = None
    limits: EvaluationLimits = Field(default_factory=EvaluationLimits)
    usage: Usage | None = None
    inspection: InspectionSummary | None = None
    elapsed_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    expected_labels: dict | None = None
    findings: tuple[SafeFinding, ...] = ()
    tools: tuple[SafeToolEvent, ...] = ()
    limitations: tuple[Limitation, ...] = ()
    expected_region_citations: int = 0
    control_region_citations: int = 0
    claim_correctness: Literal["unverified"] = "unverified"


def load_fixture():
    """Read only pinned application-approved files, never caller-supplied paths.

    Intake rejects symlinks, hard links, unsafe ancestors and changed files.
    Pins ensure edited files are not silently re-approved for external inference.
    Labels are returned separately and never become part of the agent snapshot.
    """
    request = ReviewRequest(mode="local", local_path=".")
    limits = IntakeLimits(max_files=1, max_file_bytes=8192, max_total_bytes=8192)
    snapshot = collect_local_snapshot(request, source_root=PROJECT_ROOT / "tests/fixtures/quality_inventory",
                                      selected_files=("inventory.py",), limits=limits)
    label_snapshot = collect_local_snapshot(request, source_root=PROJECT_ROOT / "tests/fixtures",
                                            selected_files=("quality_inventory_expected.json",), limits=limits)
    source = snapshot.sources[("head", "inventory.py")]
    labels = label_snapshot.sources[("head", "quality_inventory_expected.json")]
    if sha256(source.encode()).hexdigest() != SOURCE_DIGEST or sha256(labels.encode()).hexdigest() != LABELS_DIGEST:
        raise ValueError("Evaluation fixture or labels differ from approved bytes.")
    return snapshot, json.loads(labels)


def _limitation_labels(values: tuple[str, ...]) -> tuple[Limitation, ...]:
    prefixes = {
        "Model-call budget": "model_call_budget", "Tool-selection round budget": "tool_round_budget",
        "Physical tool-attempt budget": "tool_attempt_budget", "Input/output token": "token_budget",
        "Reported usage exceeded": "token_budget", "Active specialist execution timeout": "timeout",
        "Model request timeout": "timeout", "Transient model request failed": "provider_failure",
        "Model request failed": "provider_failure", "Model returned": "provider_failure",
        "Model response exceeded": "provider_failure", "Malformed final output": "malformed_final",
        "Malformed tool-call": "invalid_tool_protocol", "Missing, invalid, or duplicate": "invalid_tool_protocol",
        "Proposal ": "proposal_rejected", "Some snapshot source lines": "incomplete_coverage",
        "Inspection attempt ": "inspection_failure", "Unresolved inspection": "inspection_failure",
        "Source-read inspection failure recovered": "inspection_recovered",
        "Final-output diagnostic: unexpected_tool_call.": "invalid_tool_protocol",
        "Final-output diagnostic: ": "malformed_final",
        "Final-format diagnostic: ": "malformed_final",
    }
    return tuple(sorted({next((label for prefix, label in prefixes.items() if value.startswith(prefix)),
                              "unknown_limitation") for value in values}))


def _summarize(result: CodeQualityRun, labels: dict, limits: EvaluationLimits, elapsed: float) -> EvaluationReport:
    """Project validated results onto safe enums/counts/ranges, never model prose."""
    findings = tuple(SafeFinding(category=item.proposal.category, severity=item.proposal.severity,
                                confidence=item.proposal.confidence,
                                evidence=tuple(SafeRange(file_path=ref.file_path, start_line=ref.start_line,
                                                         end_line=ref.end_line) for ref in item.proposal.evidence))
                     for item in result.validated_findings)
    allowed = {"list_snapshot_files", "read_source_lines", "get_evidence", "get_source_metadata"}
    tools = tuple(SafeToolEvent(tool_name=event.tool_name if event.tool_name in allowed else "unknown_tool",
                               status=event.observation.status, diagnostic=event.observation.diagnostic,
                               recovery=event.observation.recovery) for event in result.events)

    def citations(region):
        first, last = region
        return sum(any(ref.start_line <= last and ref.end_line >= first for ref in item.evidence) for item in findings)

    complete = result.result.status == "completed"
    return EvaluationReport(status="COMPLETED" if complete else "PARTIAL",
                            reason="review_completed" if complete else "review_partial", started=True,
                            specialist_status=result.result.status, limits=limits, usage=result.usage, inspection=result.result.inspection,
                            elapsed_seconds=elapsed, expected_labels=labels, findings=findings, tools=tools,
                            limitations=_limitation_labels(result.result.limitations),
                            expected_region_citations=citations(labels["expected_findings"][0]["evidence_lines"]),
                            control_region_citations=citations(labels["negative_control"]["evidence_lines"]))


@asynccontextmanager
async def _bounded_provider(settings, limits, transport, deadline):
    if monotonic() >= deadline:
        raise TimeoutError
    model = NebiusModel(settings, allow_live=True, timeout_seconds=limits.request_timeout_seconds, transport=transport)
    try:
        async with asyncio.timeout(max(0, deadline - monotonic())):
            yield await model.__aenter__()
    finally:
        # A graph cancellation must not start an unbounded async close. Give
        # cleanup its own cancellation deadline, even if the run already expired.
        async with asyncio.timeout(max(0, deadline - monotonic())):
            await model.__aexit__(*sys.exc_info())


async def run_evaluation(*, allow_live: bool = False, environ: Mapping[str, str] | None = None,
                         limits: EvaluationLimits | None = None,
                         transport: httpx.AsyncBaseTransport | None = None) -> EvaluationReport:
    """Explicit opt-in only; transport injection is for trusted offline tests."""
    if allow_live is not True:
        return EvaluationReport(status="NOT RUN", reason="disabled")
    began = monotonic()
    started = False
    result = None
    chosen = EvaluationLimits()
    try:
        try:
            chosen = EvaluationLimits.model_validate(limits.model_dump() if limits is not None else {})
            if (Path.cwd().resolve() != PROJECT_ROOT or sys.version_info[:2] != (3, 12)
                    or Path(sys.prefix).resolve() != (PROJECT_ROOT / ".venv").resolve()):
                raise ValueError("Project-local Python environment required.")
        except (ValueError, TypeError, AttributeError):
            return EvaluationReport(status="NOT RUN", reason="configuration_error", elapsed_seconds=monotonic() - began)
        try:
            snapshot, labels = load_fixture()
        except (SourceIntakeError, ValueError, OSError):
            return EvaluationReport(status="NOT RUN", reason="fixture_rejected", limits=chosen,
                                    elapsed_seconds=monotonic() - began)
        try:
            settings = load_nebius_settings(environ)
            if settings.model != SELECTED_MODEL:
                raise ProviderConfigurationError("Unexpected model; no request sent.")
        except ProviderConfigurationError:
            return EvaluationReport(status="NOT RUN", reason="configuration_error", limits=chosen,
                                    elapsed_seconds=monotonic() - began)
        with tracing_context(enabled=False):
            async with _bounded_provider(settings, chosen, transport, began + chosen.run_timeout_seconds) as model:
                agent = CodeQualityAgent(snapshot, model=model, approved_synthetic=True, limits=chosen)
                started = True
                result = await agent.run(review_id=uuid4())
        return _summarize(result, labels, chosen, monotonic() - began)
    except TimeoutError:
        return EvaluationReport(status="FAIL", reason="run_timeout", started=started, limits=chosen,
                                usage=result.usage if result is not None else None, limitations=("timeout",),
                                elapsed_seconds=monotonic() - began)
    except Exception:
        # No exception string, traceback, model prose, observation or raw payload.
        # If the graph failed without a result, usage is unknown rather than zero.
        return EvaluationReport(status="FAIL", reason="unexpected_error", started=started, limits=chosen,
                                usage=result.usage if result is not None else None,
                                elapsed_seconds=monotonic() - began)


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Invalid live evaluation options; no request sent.\n")


def main() -> None:
    parser = _SafeParser(prog="review_code_quality_live.py", allow_abbrev=False,
                         description="Evaluate the pinned fictional inventory fixture; disabled by default.")
    parser.add_argument("--allow-live", action="store_true", help="Requires separate approval for up to four model requests.")
    defaults = EvaluationLimits()
    names = ("max_model_calls", "max_tool_rounds", "max_tool_attempts", "request_timeout_seconds",
             "run_timeout_seconds", "max_input_tokens", "max_output_tokens", "output_tokens_per_call")
    for name in names:
        parser.add_argument("--" + name.replace("_", "-"), type=float if name.endswith("seconds") else int,
                            default=getattr(defaults, name), help=f"May only lower the default of {getattr(defaults, name)}.")
    args = parser.parse_args()
    try:
        limits = EvaluationLimits(**{name: getattr(args, name) for name in names})
    except ValueError:
        parser.error("Invalid limits")
    report = asyncio.run(run_evaluation(allow_live=args.allow_live, limits=limits))
    print(report.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
