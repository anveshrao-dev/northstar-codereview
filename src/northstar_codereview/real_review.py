"""Guarded local real-source review. Importing this module performs no I/O.

Trusted operator actions are separate: select, consent, run, human review, export.
The provider factory is an in-process test seam, never a user-selectable plugin.
"""
import asyncio
import json
import os
from contextlib import contextmanager, ExitStack
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Literal
from uuid import UUID, uuid4

from langsmith import tracing_context
from pydantic import Field, model_validator

from . import real_source as intake
from .real_source import load_import, record_transmission
from .real_source_contracts import (MODEL, SAMPLES, SampleId, ImportApproval, IntakeManifest,
    TransmissionApproval, IntakeRejected, FailureCategory)
from .real_source_authorization import require_transmission, ensure_private_execution
from .agents.code_quality import CodeQualityAgent, RunLimits
from .execution_budget import ExecutionBudget, GlobalLimits, ProfileGlobalLimits, BudgetUsage
from .models import Contract, AgentName, Digest, AnalysisDiagnostics
from .chunk_review import build_plan, admission
from .orchestrator import OrchestrationResult, SpecialistExecution
from .reporting import ReviewReport, build_report, render_json
from .output_safety import contains_sensitive
from .providers.nebius import NebiusModel, load_nebius_settings, ProviderConfigurationError
from .approval_models import DecisionRecord
from .transmission_claims import execution_scope
from .evaluation import fingerprint

from .chunk_review import SCOPE_NOTES
from .execution_profiles import ExecutionProfile, get_profile, checked_profile

MAX_RECORD = 2 * 1024 * 1024


def profile(sample: SampleId, execution_profile="standard") -> GlobalLimits:
    SampleId(sample)  # Closed registry; no caller-provided limits or source paths.
    return get_profile(execution_profile).global_limits()


class SnapshotChoice(Contract):
    import_id: UUID
    sample: SampleId
    snapshot_id: UUID
    manifest_digest: str
    retain_until: datetime
    import_expires_at: datetime
    files: tuple[str, ...]
    usable: bool


def snapshot_catalog(sample: SampleId) -> tuple[SnapshotChoice, ...]:
    """Bounded metadata-only inventory. Invalid records are withheld, not repaired."""
    sample = SampleId(sample)
    result = []
    try:
        with intake._store() as store, intake._child(store, 'imports') as imports:
            names = sorted(os.listdir(imports))
            if len(names) > 256:
                raise IntakeRejected(FailureCategory.SIZE)
            for name in names:
                try:
                    if str(UUID(name)) != name:
                        continue
                    with intake._child(imports, name) as bundle:
                        with intake._child(bundle, 'receipts') as d:
                            receipt = ImportApproval.model_validate(json.loads(intake._read(d, 'import.json', 32768), object_pairs_hook=intake._unique_object))
                        with intake._child(bundle, 'manifests') as d:
                            manifest = IntakeManifest.model_validate(json.loads(intake._read(d, 'intake.json', 32768), object_pairs_hook=intake._unique_object))
                    if manifest != receipt.manifest or str(receipt.approval_id) != name or manifest.sample != sample:
                        continue
                    now = datetime.now(timezone.utc)
                    result.append(SnapshotChoice(import_id=receipt.approval_id, sample=sample,
                        snapshot_id=manifest.snapshot.snapshot_id, manifest_digest=manifest.digest,
                        retain_until=manifest.retain_until, import_expires_at=receipt.expires_at,
                        files=SAMPLES[sample][1], usable=receipt.state.value == 'approved' and receipt.approved_at <= now < manifest.retain_until))
                except (OSError, ValueError, TypeError, KeyError):
                    continue
    except FileNotFoundError:
        return ()
    return tuple(sorted(result, key=lambda item: (item.retain_until, str(item.import_id)), reverse=True))


def _selected(sample, import_id):
    snapshot = load_import(UUID(str(import_id)))
    if snapshot.real_source.import_approval.manifest.sample != SampleId(sample):
        raise IntakeRejected(FailureCategory.SCOPE)
    return snapshot


def authorize_transmission(sample, import_id, *, expected_digest, allow_transmission=False, execution_profile="standard"):
    """Human-facing explicit consent, never called on selection or page load."""
    if allow_transmission is not True:
        raise IntakeRejected(FailureCategory.AUTHORIZATION)
    selected_profile=get_profile(execution_profile)
    snapshot = _selected(sample, import_id)
    receipt = snapshot.real_source.import_approval
    if receipt.manifest.digest != expected_digest:
        raise IntakeRejected(FailureCategory.INTEGRITY)
    now = datetime.now(timezone.utc)
    permit = TransmissionApproval(execution_profile=selected_profile, approval_id=uuid4(), import_approval_id=receipt.approval_id,
        manifest_digest=receipt.manifest.digest, snapshot=snapshot.manifest, provider='nebius', model=MODEL,
        specialists=(AgentName.CODE_QUALITY,),
        state='approved', approved_at=now, expires_at=min(now + timedelta(minutes=30), receipt.manifest.retain_until))
    record_transmission(permit)
    return permit


@contextmanager
def _area(import_id, name):
    identifier = str(UUID(str(import_id)))
    with intake._store() as store, intake._child(store, 'imports') as imports, \
            intake._child(imports, identifier) as bundle, intake._child(bundle, name) as folder:
        yield folder


def read_transmission(import_id, transmission_id, *, specialists=None, execution_profile=None):
    snapshot = load_import(UUID(str(import_id)))
    with _area(import_id, 'receipts') as folder:
        raw = intake._read(folder, 'transmission-' + str(UUID(str(transmission_id))) + '.json', 32768)
    permit = TransmissionApproval.model_validate(json.loads(raw, object_pairs_hook=intake._unique_object))
    if permit.approval_id != UUID(str(transmission_id)):
        raise IntakeRejected(FailureCategory.AUTHORIZATION)
    require_transmission(snapshot, permit, identity=('nebius', MODEL), specialists=specialists, execution_profile=execution_profile)
    return permit


class _CapacityDenied(Exception):
    """Internal control flow; diagnostic contains only safe plan metadata."""


class RealReviewRun(Contract):
    run_id: UUID
    import_id: UUID
    sample: SampleId
    transmission_id: UUID | None = None
    manifest_digest: Digest | None = None
    execution_profile: ExecutionProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    limits: GlobalLimits | ProfileGlobalLimits = Field(default_factory=lambda: profile(SampleId.PORTFOLIO_RISK))
    synthesis_input_hold: Literal[30000] = 30000
    provider: Literal['nebius'] = 'nebius'
    model: Literal['Qwen/Qwen3-30B-A3B-Instruct-2507'] = MODEL
    status: Literal['blocked', 'completed', 'partial', 'failed'] = 'blocked'
    category: Literal['live_opt_in_required', 'authorization', 'transmission_consumed', 'configuration', 'run_already_claimed',
        'execution_failed', 'deadline', 'cancelled', 'storage_failure', 'cleanup_failed', 'capacity'] | None = None
    capacity: AnalysisDiagnostics | None = Field(default=None, exclude_if=lambda value: value is None)
    report: ReviewReport | None = None
    usage: BudgetUsage
    elapsed_seconds: float = 0
    cost: None = None
    persisted: bool = False
    cleanup: Literal['not_started', 'closed', 'failed'] = 'not_started'

    @model_validator(mode='after')
    def safe(self):
        bound=checked_profile(self.execution_profile) if self.execution_profile is not None else get_profile()
        if self.limits.model_dump() != bound.global_limits().model_dump():
            raise ValueError('Profile mismatch')
        if self.persisted and (self.transmission_id is None or self.manifest_digest is None):
            raise ValueError('Missing authorization lineage')
        if contains_sensitive(self.model_dump(mode='json')):
            raise ValueError('Unsafe review result')
        if self.report and (self.report.review_id != self.run_id or
                self.status == 'completed' and self.report.status != 'completed'):
            raise ValueError('Report binding mismatch')
        return self


def _atomic_record(folder, name, data):
    stage = '.pending-' + uuid4().hex
    try:
        intake._write(folder, stage, data)
        os.link(stage, name, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)
        os.fsync(folder)
    finally:
        if stage in os.listdir(folder):
            os.unlink(stage, dir_fd=folder)


async def run_review(sample, import_id, *, transmission_id=None, allow_live=False,
                     run_id=None, environ=None, provider_factory=None, prepared_context=True, execution_profile="standard") -> RealReviewRun:
    """One attempt; validation before provider startup; no retries or source execution."""
    sample, import_id = SampleId(sample), UUID(str(import_id))
    run_id = UUID(str(run_id)) if run_id is not None else uuid4()
    selected_profile=get_profile(execution_profile)
    limits = selected_profile.global_limits()
    ledger = ExecutionBudget(limits, execution_profile=selected_profile)
    fields = dict(run_id=run_id, import_id=import_id, sample=sample, usage=ledger.usage(),
                  execution_profile=selected_profile,limits=limits)
    if allow_live is not True:
        return RealReviewRun(**fields, category='live_opt_in_required')
    began = monotonic()
    provider = None
    permit = None
    execution = ExitStack()
    claimed = False
    capacity = None
    status, category, cleanup, report = 'blocked', 'authorization', 'not_started', None
    try:
        ensure_private_execution()
        snapshot = _selected(sample, import_id)
        permit = read_transmission(import_id, transmission_id, specialists=(AgentName.CODE_QUALITY,), execution_profile=selected_profile)
        category = 'configuration'
        settings = load_nebius_settings(environ)
        if settings.model != MODEL:
            raise ProviderConfigurationError('Configured model does not match approval.')
        # Durable claim is never removed or reused, including cancellation/failure.
        with _area(import_id, 'review_artifacts') as folder:
            try:
                intake._write(folder, str(run_id) + '.claim', b'claimed\n')
                os.fsync(folder)
            except FileExistsError:
                return RealReviewRun(**fields, category='run_already_claimed')
        claimed = True
        execution.enter_context(execution_scope(snapshot, permit, run_id=run_id, identity=('nebius', settings.model),
                                                specialists=(AgentName.CODE_QUALITY,), execution_profile=selected_profile))
        job = uuid4()
        reservation = ledger.reserve_synthesis((job,), input_units=30_000)[job]
        category = 'execution_failed'
        if prepared_context:
            # Claimed and authorized before source planning; no provider exists yet.
            capacity = admission(build_plan(snapshot, execution_profile=selected_profile),model_calls=limits.max_model_requests,
                tool_attempts=limits.max_tool_attempts,input_units=limits.max_input_tokens,
                output_units=limits.max_output_tokens,output_per_call=limits.output_tokens_per_call)
            if capacity.admission == 'denied':
                raise _CapacityDenied()
            capacity = None  # Actual execution diagnostics belong to the report.
        with tracing_context(enabled=False):
            async with asyncio.timeout(ledger.remaining()):
                provider = (provider_factory(settings, limits.request_timeout_seconds) if provider_factory else
                            NebiusModel(settings, allow_live=True, timeout_seconds=limits.request_timeout_seconds))
                require_transmission(snapshot, permit, identity=provider.transmission_identity)
                agent = CodeQualityAgent(snapshot, model=provider, transmission_approval=permit, execution_budget=ledger,
                    prepared_context=prepared_context,
                    limits=selected_profile.agent_limits())
                agent.synthesis_reservation = reservation
                result = await agent.run(review_id=run_id, job_id=job)
        outcome = OrchestrationResult(review_id=run_id, snapshot_id=snapshot.manifest.snapshot_id,
            selected_specialists=(AgentName.CODE_QUALITY,),
            specialist_results=(SpecialistExecution(result=result.result, usage=result.usage,
                validated_findings=result.validated_findings),), validated_findings=result.validated_findings,
            limitations=(*result.result.limitations, SCOPE_NOTES[sample],
                'Code Quality only; other specialists were not run. Claims require independent human judgment.',
                'Cross-unit behavior and excluded dependencies remain unverified; validated analysis is not defect correctness.'),
            failures=(), status=result.result.status)
        report = build_report(outcome, snapshot=snapshot.manifest)
        status, category = report.status, getattr(agent, '_analysis_interruption', None)
    except _CapacityDenied:
        status, category = 'blocked', 'capacity'
    except IntakeRejected as error:
        status = 'partial' if ledger.usage().model_attempts else 'blocked'
        category = 'transmission_consumed' if error.category == FailureCategory.TRANSMISSION_CONSUMED else 'authorization'
    except asyncio.CancelledError:
        ledger.stop('cancelled')
        status, category = 'partial', 'cancelled'
    except TimeoutError:
        ledger.stop('deadline')
        status, category = 'partial', 'deadline'
    except Exception:
        status = 'partial' if ledger.usage().model_attempts else 'blocked'
    finally:
        execution.close()
        if provider is not None:
            try:
                with tracing_context(enabled=False):
                    await asyncio.wait_for(provider.__aexit__(None, None, None), timeout=2)
                cleanup = 'closed'
            except (Exception, asyncio.CancelledError):
                cleanup, category, status = 'failed', 'cleanup_failed', 'partial'
    output = RealReviewRun(run_id=run_id, import_id=import_id, sample=sample, usage=ledger.usage(),
        execution_profile=selected_profile,limits=limits,
        transmission_id=permit.approval_id if permit else None, manifest_digest=permit.manifest_digest if permit else None,
        status=status, category=category, report=report, capacity=capacity, elapsed_seconds=monotonic()-began, cleanup=cleanup)
    if claimed:
        try:
            output = output.model_copy(update={'persisted': True})
            body = output.model_dump(mode='json')
            raw = json.dumps({'digest': fingerprint(body), 'result': body}, sort_keys=True).encode()
            if len(raw) > MAX_RECORD: raise ValueError('Oversized result')
            with _area(import_id, 'review_artifacts') as folder:
                _atomic_record(folder, str(run_id) + '.json', raw)
        except Exception:
            output = output.model_copy(update={'persisted': False, 'status': 'partial', 'category': 'storage_failure'})
    return output


def load_review(import_id, run_id):
    snapshot = load_import(UUID(str(import_id)))  # Retention also gates viewing/export.
    with _area(import_id, 'review_artifacts') as folder:
        payload = json.loads(intake._read(folder, str(UUID(str(run_id))) + '.json', MAX_RECORD), object_pairs_hook=intake._unique_object)
    if set(payload) != {'digest', 'result'} or fingerprint(payload['result']) != payload['digest']:
        raise IntakeRejected(FailureCategory.INTEGRITY)
    result = RealReviewRun.model_validate(payload['result'])
    if (result.import_id != UUID(str(import_id)) or result.run_id != UUID(str(run_id)) or
            result.sample != snapshot.real_source.import_approval.manifest.sample or
            result.manifest_digest != snapshot.real_source.import_approval.manifest.digest):
        raise IntakeRejected(FailureCategory.INTEGRITY)
    if result.report:
        render_json(result.report)
        if (result.report.snapshot_id, result.report.snapshot_revision) != (snapshot.manifest.snapshot_id, snapshot.manifest.revision):
            raise IntakeRejected(FailureCategory.INTEGRITY)
        hashes = {f.file_path: f.sha256 for f in snapshot.manifest.files}
        for finding in result.report.findings:
            for ref in finding.evidence:
                if (ref.side.value != 'head' or hashes.get(ref.file_path) != ref.source_sha256 or
                        ref.end_line > len(_physical_lines(snapshot.sources[ref.side, ref.file_path]))):
                    raise IntakeRejected(FailureCategory.INTEGRITY)
    return result


def _physical_lines(text):
    segments = text.split('\n')
    return [segment + '\n' for segment in segments[:-1]] + ([segments[-1]] if segments[-1] else [])


def evidence_excerpt(import_id, run_id, finding_id, index):
    result = load_review(import_id, run_id)
    finding = next(f for f in result.report.findings if f.finding_id == UUID(str(finding_id)))
    if type(index) is not int or not 0 <= index < len(finding.evidence):
        raise IntakeRejected(FailureCategory.SCOPE)
    ref = finding.evidence[index]
    snapshot = load_import(UUID(str(import_id)))
    text = snapshot.sources[ref.side, ref.file_path]
    # LF-delimited exact bytes, consistent with the evidence tools; never execute.
    lines = _physical_lines(text)
    return ''.join(lines[ref.start_line-1:ref.end_line])


def _source_for_saved_review(import_id, run_id):
    """Local viewing only; rebind the second verified load, never use archival reads."""
    try:
        result = load_review(import_id, run_id)
        snapshot = load_import(UUID(str(import_id)))
    except IntakeRejected:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise IntakeRejected(FailureCategory.INTEGRITY) from None
    receipt = snapshot.real_source.import_approval
    if (result.report is None or not result.persisted
            or receipt.approval_id != result.import_id
            or receipt.manifest.sample != result.sample
            or receipt.manifest.digest != result.manifest_digest
            or (snapshot.manifest.snapshot_id, snapshot.manifest.revision)
            != (result.report.snapshot_id, result.report.snapshot_revision)):
        raise IntakeRejected(FailureCategory.INTEGRITY)
    return snapshot


def source_file_catalog(import_id, run_id):
    """Bounded metadata only; no source returned, cached, executed or transmitted."""
    from .evidence_tools import list_snapshot_files, get_source_metadata
    from .models import SourceSide
    with tracing_context(enabled=False):
        snapshot = _source_for_saved_review(import_id, run_id)
        page = list_snapshot_files(snapshot, limit=50)
        if page.status != 'ok' or page.data.next_offset is not None:
            raise IntakeRejected(FailureCategory.SCOPE)
        metadata = []
        for entry in page.data.files:
            if entry.side != SourceSide.HEAD:
                raise IntakeRejected(FailureCategory.SCOPE)
            item = get_source_metadata(snapshot, file_path=entry.file_path)
            if item.status != 'ok':
                raise IntakeRejected(FailureCategory.SCOPE)
            metadata.append(item.data)
        return tuple(metadata)


def source_excerpt(import_id, run_id, *, file_path, start_line, end_line):
    """Explicit local range read; reuse strict path, 100-line and output limits."""
    from .evidence_tools import read_source_lines
    with tracing_context(enabled=False):
        snapshot = _source_for_saved_review(import_id, run_id)
        result = read_source_lines(snapshot, file_path=file_path, start_line=start_line, end_line=end_line)
        if result.status != 'ok' or result.data is None:
            raise IntakeRejected(FailureCategory.SCOPE)
        return result.data.text


def export_review(import_id, run_id, *, decision: DecisionRecord | None = None):
    result = load_review(import_id, run_id)
    if decision is not None:
        decision = DecisionRecord.model_validate(decision.model_dump())
        if (not result.report or decision.report_hash != result.report.report_id or
                decision.review_id != result.report.review_id or decision.report_revision != result.report.report_revision):
            raise IntakeRejected(FailureCategory.INTEGRITY)
    return json.dumps({'review': result.model_dump(mode='json'),
        'human_decision': decision.model_dump(mode='json') if decision else None,
        'semantic_correctness': 'not_automatically_established', 'publication_authorized': False}, indent=2)
