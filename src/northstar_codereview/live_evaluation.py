"""Explicit single-scenario live evaluation; no implicit provider startup.

Reuse Report v2 and the semantic scorer. Labels remain separate from model input.
Factory injection is trusted offline-test infrastructure, never a CLI option.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from time import monotonic
from typing import Literal
from uuid import uuid4

from langsmith import tracing_context
from pydantic import Field, model_validator

from .agents.code_quality import RunLimits
from .evaluation import EvaluationError, evaluate, fingerprint, render_evaluation
from .evaluation_artifacts import write_runtime_artifacts
from .evaluation_data import load_dataset, load_scenario
from .evaluation_models import EvaluationResult
from .execution_budget import (BudgetProfile, BudgetReason, BudgetUsage, ExecutionBudget,
                               GlobalLimits, JavaGlobalLimits, select_limits)
from .local_source import PROJECT_ROOT
from .models import AgentName, Contract, InspectionSummary
from .orchestrator import ReviewOrchestrator
from .output_safety import contains_sensitive
from .providers.nebius import NebiusModel, load_nebius_settings
from .reporting import ReviewReport, build_report, render_json, render_markdown


Scenario = Literal['java_backend', 'angular_frontend', 'jenkins_aks']
SCENARIOS = ('java_backend', 'angular_frontend', 'jenkins_aks')
SELECTED_MODEL = 'Qwen/Qwen3-30B-A3B-Instruct-2507'
DATASET_DIGEST = '0873950864212b13d71df19a68985aa6d286da70ed9099fa44db9b0e2b10bde1'
CLEANUP_SECONDS = 2.0


def _specialist_limits(limits):
    return RunLimits(max_provider_retries=0, max_output_repairs=0,
        request_timeout_seconds=limits.request_timeout_seconds, run_timeout_seconds=limits.evaluation_timeout_seconds,
        max_input_tokens=limits.max_input_tokens, max_output_tokens=limits.max_output_tokens,
        output_tokens_per_call=limits.output_tokens_per_call)


def _configuration(limits, profile: BudgetProfile = 'default'):
    result = dict(model=SELECTED_MODEL, dataset_id=DATASET_DIGEST, global_limits=limits.model_dump(mode='json'),
        specialist_limits=_specialist_limits(limits).model_dump(mode='json'), cleanup_seconds=CLEANUP_SECONDS,
        tracing=False, scoring_version='semantic-adjudication-v1', semantic_judgments='human_required',
        synthesis_reservations='one request per selected specialist; tokens checked at admission',
        input_reservation='UTF-8 bytes plus framing, per complete message/tool history; not exact billing',
        token_reporting='reported counts are known subtotals when unknown_usage_attempts is nonzero')
    if profile != 'default':
        result['budget_profile'] = profile
    return result


class LiveSpecialist(Contract):
    agent_name: AgentName
    selected: Literal[True] = True
    status: Literal['completed', 'partial', 'failed', 'skipped', 'unavailable']
    source_inspection_complete: bool | None
    validated_findings: int | None = Field(ge=0)
    inspection: InspectionSummary | None


class LiveEvaluation(Contract):
    schema_version: Literal[3] = 3
    reason: Literal['disabled', 'configuration_error', 'fixture_rejected', 'completed', 'partial', 'failed',
                    'deadline', 'unexpected_error']
    scenario_id: Scenario | None = None
    model: Literal['Qwen/Qwen3-30B-A3B-Instruct-2507'] = SELECTED_MODEL
    started: bool = False
    profile: BudgetProfile = 'default'
    limits: GlobalLimits | JavaGlobalLimits = Field(default_factory=GlobalLimits)
    configuration: dict
    budget_usage: BudgetUsage
    budget_reason: BudgetReason | None = None
    elapsed_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    cleanup: Literal['not_needed', 'completed', 'failed', 'timed_out'] = 'not_needed'
    specialists: tuple[LiveSpecialist, ...] = ()
    validated_findings: int | None = Field(default=None, ge=0)
    report: ReviewReport | None = None
    evaluation: EvaluationResult | None = None

    @model_validator(mode='after')
    def consistent_profile(self):
        select_limits(self.profile, self.scenario_id, self.limits)
        return self


async def _close_providers(providers):
    if not providers:
        return 'not_needed'
    async def close(provider):
        await provider.__aexit__(None, None, None)
    try:
        async with asyncio.timeout(CLEANUP_SECONDS):
            results = await asyncio.gather(*(close(p) for p in providers), return_exceptions=True)
        return 'failed' if any(isinstance(r, BaseException) for r in results) else 'completed'
    except TimeoutError:
        return 'timed_out'


async def run_live_evaluation(*, scenario_id: str = 'java_backend', allow_live: bool = False,
                              environ: Mapping[str, str] | None = None, limits: GlobalLimits | None = None,
                              provider_factory=None, profile: BudgetProfile = 'default') -> LiveEvaluation:
    """At most one pinned scenario. No .env reads and no scripted adjudication.

    The active deadline cancels the whole graph. If it cannot return a collected
    report, findings/coverage remain unavailable; the shared ledger survives.
    Remote cancellation and exact token billing cannot be guaranteed locally.
    """
    began = monotonic()
    chosen = GlobalLimits()
    ledger = ExecutionBudget(chosen)
    scenario = dataset = outcome = report = evaluation = None
    reason, started, cleanup, providers = 'disabled', False, 'not_needed', []
    try:
        chosen = select_limits(profile, scenario_id, limits)
        ledger = ExecutionBudget(chosen, profile=profile, scenario_id=scenario_id)
    except (ValueError, TypeError, AttributeError):
        return LiveEvaluation(reason='configuration_error', configuration=_configuration(GlobalLimits()),
                              budget_usage=ledger.usage())
    if allow_live is not True:
        return LiveEvaluation(reason='disabled', profile=profile, limits=chosen,
            scenario_id=scenario_id if scenario_id in SCENARIOS else None,
            configuration=_configuration(chosen, profile), budget_usage=ledger.usage())
    try:
        if (scenario_id not in SCENARIOS or Path.cwd().resolve() != PROJECT_ROOT
                or sys.version_info[:2] != (3, 12) or Path(sys.prefix).resolve() != (PROJECT_ROOT / '.venv').resolve()):
            raise ValueError('Invalid trusted execution context')
        reason = 'fixture_rejected'
        dataset = load_dataset()
        if fingerprint(dataset) != DATASET_DIGEST:
            raise ValueError('Unapproved dataset')
        scenario = next(s for s in dataset.scenarios if s.id == scenario_id)
        snapshot = load_scenario(scenario)
        reason = 'configuration_error'
        if chosen.max_model_requests < len(scenario.specialists):
            raise ValueError('Insufficient synthesis reservation capacity')
        settings = load_nebius_settings(environ)
        if settings.model != SELECTED_MODEL:
            raise ValueError('Unapproved model')
        def factory(name):
            # No asynchronous gap between expiry check and local construction.
            if ledger.remaining() <= 0 or ledger.reason is not None:
                raise ValueError('Execution unavailable')
            model = (provider_factory(settings, chosen.request_timeout_seconds) if provider_factory else
                     NebiusModel(settings, allow_live=True, timeout_seconds=chosen.request_timeout_seconds))
            providers.append(model)
            return model
        reason = 'unexpected_error'
        with tracing_context(enabled=False):
            async with asyncio.timeout(ledger.remaining()):
                if ledger.remaining() <= 0:
                    raise TimeoutError
                started = True
                outcome = await ReviewOrchestrator(snapshot, model_factory=factory, approved_synthetic=True,
                    specialist_limits=_specialist_limits(chosen), execution_budget=ledger).run(review_id=uuid4())
        report = build_report(outcome, snapshot=snapshot.manifest)
        reason = report.status if report.status in {'completed', 'partial', 'failed'} else 'partial'
    except asyncio.CancelledError:
        ledger.stop('cancelled')
        raise  # Caller cancellation is not converted into a successful evaluation.
    except TimeoutError:
        ledger.stop('deadline')
        reason = 'deadline'
    except Exception:
        if reason == 'disabled':
            reason = 'configuration_error'
        # Never retain raw exceptions, validation inputs, provider bodies or messages.
    finally:
        with tracing_context(enabled=False):
            cleanup = await _close_providers(providers)
    summaries = []
    if scenario is not None and reason not in {'configuration_error', 'fixture_rejected'}:
        for name in scenario.specialists:
            item = next((e for e in outcome.specialist_results if e.result.agent_name == name), None) if outcome else None
            state = item.result.status if item else 'unavailable'
            complete = (not any(s.startswith('Some snapshot source lines') for s in item.result.limitations)
                        if item and state in {'completed', 'partial'} else None)
            summaries.append(LiveSpecialist(agent_name=name, status=state, source_inspection_complete=complete,
                validated_findings=len(item.validated_findings) if item else None,
                inspection=item.result.inspection if item else None))
        evaluation = evaluate(dataset, scenario.id, report, configuration_id=fingerprint(_configuration(chosen, profile)),
                              elapsed_seconds=monotonic() - began)  # No semantic judgments supplied.
    return LiveEvaluation(reason=reason, scenario_id=scenario_id if scenario_id in SCENARIOS else None, started=started,
        profile=profile, limits=chosen, configuration=_configuration(chosen, profile), budget_usage=ledger.usage(), budget_reason=ledger.reason,
        elapsed_seconds=monotonic() - began, cleanup=cleanup, specialists=tuple(summaries),
        validated_findings=len(outcome.validated_findings) if outcome else None, report=report, evaluation=evaluation)


def render_live_json(output: LiveEvaluation) -> str:
    try:
        output = LiveEvaluation.model_validate(output.model_dump(mode='json'))
        if output.configuration != _configuration(output.limits, output.profile):
            raise ValueError('Changed configuration')
        if output.report:
            render_json(output.report)  # Existing safety/hash checks, not raw provider JSON.
        if output.evaluation:
            render_evaluation(output.evaluation)
        payload = output.model_dump(mode='json')
        if contains_sensitive(payload):
            raise ValueError('Blocked sensitive output')
        return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + '\n'
    except (ValueError, TypeError, AttributeError):
        raise EvaluationError('unsafe_output') from None


def render_live_summary(output: LiveEvaluation) -> str:
    output = LiveEvaluation.model_validate_json(render_live_json(output))
    usage = output.budget_usage
    lines = ['# Controlled evaluation', '', f'Scenario: {output.scenario_id or "unavailable"}. Result: {output.reason}.',
        f'Budget profile: {output.profile}.',
        f'Model attempts: {usage.model_attempts}. Tool attempts: {usage.tool_attempts}.',
        f'Unknown-usage attempts: {usage.unknown_usage_attempts}. Cleanup: {output.cleanup}.',
        f'Elapsed seconds: {output.elapsed_seconds:.3f}.', '',
        'Execution, source coverage, evidence support and defect correctness are separate judgments.',
        'Unjudged findings remain pending; missing/incomplete reviews cannot pass negative controls.', '']
    if output.evaluation:
        lines.append(f'Pending semantic judgments: {output.evaluation.quality.pending}.')
    if output.report:
        lines.extend(['', render_markdown(output.report)])
    return '\n'.join(lines) + '\n'


def save_live_artifacts(output):
    if not output.started:
        raise EvaluationError('evaluation_not_started')
    return write_runtime_artifacts(PROJECT_ROOT, json.loads(render_live_json(output)), render_live_summary(output))


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'Invalid controlled evaluation options; no request sent.\n')


def main(argv=None):
    parser = _Parser(description='One pinned fictional scenario; requires separately authorized live inference.', allow_abbrev=False)
    parser.add_argument('--allow-live', action='store_true')
    parser.add_argument('--scenario', choices=SCENARIOS, default='java_backend')
    parser.add_argument('--budget-profile', choices=('default', 'java'), default='default',
                        help='Explicit Java-only ceilings; default policy remains unchanged.')
    for name, value in GlobalLimits().model_dump().items():
        parser.add_argument('--' + name.replace('_', '-'), type=float if name.endswith('seconds') else int, default=None,
                            help='May only lower the selected profile ceiling.')
    args = parser.parse_args(argv)
    try:
        defaults = select_limits(args.budget_profile, args.scenario)
        overrides = {name: getattr(args, name) for name in GlobalLimits.model_fields if getattr(args, name) is not None}
        limits = select_limits(args.budget_profile, args.scenario, defaults.model_copy(update=overrides))
    except ValueError:
        parser.error('Invalid limits')
    try:
        output = asyncio.run(run_live_evaluation(scenario_id=args.scenario, allow_live=args.allow_live,
                                                limits=limits, profile=args.budget_profile))
        print(render_live_json(output))
        if output.started:
            for path in save_live_artifacts(output):
                print(path.relative_to(PROJECT_ROOT))
    except Exception:
        print('Controlled evaluation failed; details withheld.')
        raise SystemExit(1) from None
