"""Pinned, independently callable GitOps evaluation; no outer graph dispatch.

Labels are loaded for evaluation only and never passed to the specialist.
Provider injection is trusted offline-test infrastructure, not a CLI option.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path
from time import monotonic
from typing import Literal
from uuid import uuid4

from langsmith import tracing_context
from pydantic import Field

from .agents.code_quality import RunLimits
from .agents.gitops import GitOpsAgent
from .evaluation import EvaluationError, evaluate, fingerprint, render_evaluation
from .evaluation_artifacts import write_runtime_artifacts
from .evaluation_models import EvaluationDataset, EvaluationResult
from .execution_budget import BudgetUsage, ExecutionBudget, GlobalLimits
from .live_evaluation import _close_providers
from .local_source import PROJECT_ROOT, IntakeLimits, collect_local_snapshot
from .models import AgentName, Contract, ReviewRequest
from .orchestrator import OrchestrationResult, SpecialistExecution
from .output_safety import contains_sensitive
from .providers.nebius import NebiusModel, load_nebius_settings
from .reporting import ReviewReport, build_report, render_json, render_markdown

MODEL = 'Qwen/Qwen3-30B-A3B-Instruct-2507'
SCENARIO = 'gitops_selector_slice'
DATASET_DIGEST = '081f406c42feeea7b54b6fb54ea39b6212d0364bb09f448dfe83dc3e5754a6f2'
FIXTURE = PROJECT_ROOT / 'tests/fixtures/gitops_slice'


class SliceLimits(GlobalLimits):
    max_model_requests: int = Field(default=4, strict=True, ge=1, le=4)
    max_tool_attempts: int = Field(default=6, strict=True, ge=1, le=6)
    max_input_tokens: int = Field(default=80_000, strict=True, ge=1, le=80_000)
    max_output_tokens: int = Field(default=4_000, strict=True, ge=1, le=4_000)


def specialist_limits(limits):
    return RunLimits(max_model_calls=limits.max_model_requests, max_tool_rounds=3,
        max_tool_attempts=limits.max_tool_attempts, max_provider_retries=0, max_output_repairs=0,
        request_timeout_seconds=limits.request_timeout_seconds, run_timeout_seconds=limits.evaluation_timeout_seconds,
        max_input_tokens=limits.max_input_tokens, max_output_tokens=limits.max_output_tokens,
        output_tokens_per_call=limits.output_tokens_per_call)


def configuration(limits):
    return {'model':MODEL,'dataset_id':DATASET_DIGEST,'scenario':SCENARIO,'specialists':['gitops'],
        'global_limits':limits.model_dump(mode='json'),
        'specialist_limits':specialist_limits(limits).model_dump(mode='json'),
        'synthesis_reservations':'one owner-bound request','tracing':False,
        'synthesis_input_hold':min(30_000,limits.max_input_tokens),
        'semantic_judgments':'human_required','source_inventory':'two fictional resources only'}


def load_fixture():
    request=ReviewRequest(mode='local',local_path='.')
    metadata=collect_local_snapshot(request,source_root=FIXTURE,selected_files=('dataset.json',),
        limits=IntakeLimits(max_files=1,max_file_bytes=16384,max_total_bytes=16384))
    dataset=EvaluationDataset.model_validate_json(metadata.sources['head','dataset.json'])
    if fingerprint(dataset)!=DATASET_DIGEST or len(dataset.scenarios)!=1:
        raise EvaluationError('fixture_rejected')
    scenario=dataset.scenarios[0]
    if scenario.id!=SCENARIO or scenario.specialists!=(AgentName.GITOPS,):
        raise EvaluationError('fixture_rejected')
    snapshot=collect_local_snapshot(request,source_root=FIXTURE/'source',selected_files=('resources.yaml',),
        limits=IntakeLimits(max_files=1,max_file_bytes=8192,max_total_bytes=8192))
    if snapshot.manifest!=scenario.snapshot:
        raise EvaluationError('fixture_rejected')
    for label in (*scenario.expected,*scenario.controls):
        for loc in label.locations:
            if loc.end_line>len(snapshot.sources[loc.side,loc.file_path].splitlines()):
                raise EvaluationError('fixture_rejected')
    return snapshot,dataset


class SliceEvaluation(Contract):
    schema_version: Literal[1] = 1
    scenario: Literal['gitops_selector_slice'] = SCENARIO
    model: Literal['Qwen/Qwen3-30B-A3B-Instruct-2507'] = MODEL
    reason: Literal['disabled','configuration_error','fixture_rejected','completed','partial','failed','deadline','unexpected_error']
    started: bool = False
    limits: SliceLimits = Field(default_factory=SliceLimits)
    configuration: dict
    budget_usage: BudgetUsage
    elapsed_seconds: float = Field(default=0,ge=0,allow_inf_nan=False)
    cleanup: Literal['not_needed','completed','failed','timed_out'] = 'not_needed'
    report: ReviewReport | None = None
    evaluation: EvaluationResult | None = None


async def run_slice(*, allow_live=False, environ=None, limits=None, provider_factory=None):
    began=monotonic()
    chosen=SliceLimits()
    try:
        chosen=SliceLimits.model_validate(limits.model_dump() if limits is not None else {})
    except (ValueError,TypeError,AttributeError):
        return SliceEvaluation(reason='configuration_error',configuration=configuration(chosen),
            budget_usage=ExecutionBudget(chosen).usage())
    ledger=ExecutionBudget(chosen)
    if allow_live is not True:
        return SliceEvaluation(reason='disabled',limits=chosen,configuration=configuration(chosen),budget_usage=ledger.usage())
    reason,started,cleanup='configuration_error',False,'not_needed'
    report=evaluation=dataset=reservation=None
    providers=[]
    try:
        if (Path.cwd().resolve()!=PROJECT_ROOT or sys.version_info[:2]!=(3,12)
                or Path(sys.prefix).resolve()!=(PROJECT_ROOT/'.venv').resolve()):
            raise ValueError('Invalid execution context')
        reason='fixture_rejected'
        snapshot,dataset=load_fixture()
        reason='configuration_error'
        settings=load_nebius_settings(environ)
        if settings.model!=MODEL:
            raise ValueError('Unapproved model')
        job=uuid4()
        reservation=ledger.reserve_synthesis((job,),input_units=min(30_000,chosen.max_input_tokens))[job]
        reason='unexpected_error'
        with tracing_context(enabled=False):
            async with asyncio.timeout(ledger.remaining()):
                provider=(provider_factory(settings,chosen.request_timeout_seconds) if provider_factory else
                    NebiusModel(settings,allow_live=True,timeout_seconds=chosen.request_timeout_seconds))
                providers.append(provider)
                agent=GitOpsAgent(snapshot,model=provider,approved_synthetic=True,
                    limits=specialist_limits(chosen),execution_budget=ledger)
                agent.synthesis_reservation=reservation
                started=True
                result=await agent.run(review_id=uuid4(),job_id=job)
        # Adapt the single result to the established report contract. No outer
        # orchestrator is constructed, routed, or invoked.
        collected=OrchestrationResult(review_id=result.result.review_id,snapshot_id=snapshot.manifest.snapshot_id,
            selected_specialists=(AgentName.GITOPS,),
            specialist_results=(SpecialistExecution(result=result.result,validated_findings=result.validated_findings,usage=result.usage),),
            validated_findings=result.validated_findings,limitations=result.result.limitations,failures=(),status=result.result.status)
        report=build_report(collected,snapshot=snapshot.manifest)
        reason=report.status
    except asyncio.CancelledError:
        ledger.stop('cancelled')
        raise
    except TimeoutError:
        ledger.stop('deadline')
        reason='deadline'
    except Exception:
        pass  # Only the current safe stage label leaves this boundary.
    finally:
        if reservation is not None:
            ledger.release_synthesis(job, reservation)
        with tracing_context(enabled=False):
            cleanup=await _close_providers(providers)
    if dataset is not None and started:
        evaluation=evaluate(dataset,SCENARIO,report,configuration_id=fingerprint(configuration(chosen)),
            elapsed_seconds=monotonic()-began,run_kind='scripted_integration' if provider_factory else 'external_review')
    return SliceEvaluation(reason=reason,started=started,limits=chosen,configuration=configuration(chosen),
        budget_usage=ledger.usage(),elapsed_seconds=monotonic()-began,cleanup=cleanup,report=report,evaluation=evaluation)


def render_slice(output):
    try:
        output=SliceEvaluation.model_validate(output.model_dump(mode='json'))
        if output.configuration!=configuration(output.limits):raise ValueError('Changed configuration')
        if output.report:render_json(output.report)
        if output.evaluation:render_evaluation(output.evaluation)
        payload=output.model_dump(mode='json')
        if contains_sensitive(payload):raise ValueError('Unsafe output')
        return json.dumps(payload,sort_keys=True,indent=2,ensure_ascii=True,allow_nan=False)+'\n'
    except (ValueError,TypeError,AttributeError):
        raise EvaluationError('unsafe_output') from None


def save_artifacts(output):
    if not output.started:raise EvaluationError('evaluation_not_started')
    payload=json.loads(render_slice(output))
    summary=f'# GitOps single-specialist evaluation\n\nStatus: {output.reason}.\n'
    summary+='Evidence support and defect correctness are separate; semantic findings require human review.\n'
    if output.report:summary+='\n'+render_markdown(output.report)
    return write_runtime_artifacts(PROJECT_ROOT,payload,summary)


class _Parser(argparse.ArgumentParser):
    def error(self,message):self.exit(2,'Invalid GitOps slice options; no request sent.\n')


def main(argv=None):
    parser=_Parser(description='One pinned GitOps review; separate inference approval required.',allow_abbrev=False)
    parser.add_argument('--allow-live',action='store_true')
    args=parser.parse_args(argv)
    try:
        output=asyncio.run(run_slice(allow_live=args.allow_live))
        print(render_slice(output))
        if output.started:
            for path in save_artifacts(output):print(path.relative_to(PROJECT_ROOT))
    except Exception:
        print('GitOps slice failed; details withheld.')
        raise SystemExit(1) from None
