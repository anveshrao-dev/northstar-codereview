"""Durable one-use transmission claims and non-resumable execution scopes.

Filesystem exclusion is the cross-process authority. The in-memory scope only
permits the admitted execution's graph branches to use that consumed approval.
It is not serializable, transferable to a child process, or a restart token.
"""
import json
import os
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from threading import Lock
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field
from .execution_profiles import ExecutionProfile, checked_profile, approval_profile

from .models import AgentName, Contract, Digest, Revision
from .real_source_contracts import MODEL, SpecialistScope, TransmissionApproval, IntakeRejected, FailureCategory
from .real_source_authorization import checked_specialists, require_transmission


class ExecutionClaim(Contract):
    execution_profile: ExecutionProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    approval_id: UUID
    import_approval_id: UUID
    run_id: UUID
    manifest_digest: Digest
    approval_digest: Digest
    snapshot_id: UUID
    snapshot_revision: Revision
    provider: Literal['nebius']
    model: Literal['Qwen/Qwen3-30B-A3B-Instruct-2507']
    specialists: SpecialistScope
    requested_specialists: SpecialistScope
    claimed_at: AwareDatetime


@dataclass(repr=False)
class _Execution:
    claim: ExecutionClaim
    pid: int = field(default_factory=os.getpid)
    active: bool = True
    started: bool = False
    finished: bool = False
    jobs: dict[UUID, AgentName] | None = None
    used_jobs: set[UUID] = field(default_factory=set)
    lock: Lock = field(default_factory=Lock)


_ACTIVE: ContextVar[_Execution | None] = ContextVar('real_source_execution', default=None)


@dataclass(repr=False)
class _Worker:
    execution: _Execution
    specialist: AgentName
    job_id: UUID
    active: bool = True


_WORKER: ContextVar[_Worker | None] = ContextVar('real_source_specialist', default=None)


def _digest(permit):
    return sha256(permit.model_dump_json().encode()).hexdigest()


def _reject():
    raise IntakeRejected(FailureCategory.AUTHORIZATION) from None


def ensure_unconsumed(approval: TransmissionApproval):
    """Read-only UI hint. Admission always uses O_EXCL; this is not a lock."""
    from . import real_source as storage
    try:
        permit = TransmissionApproval.model_validate(approval.model_dump(warnings=False))
        with storage._store() as store, storage._child(store, 'transmission_claims') as claims:
            try:
                os.stat(str(permit.approval_id) + '.json', dir_fd=claims, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise IntakeRejected(FailureCategory.TRANSMISSION_CONSUMED)
    except FileNotFoundError:
        return
    except IntakeRejected:
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        _reject()


def _claim(record):
    from . import real_source as storage
    try:
        with storage._store() as store, storage._child(store, 'transmission_claims', create=True) as claims:
            os.fsync(store)  # Persist the private claims directory before use.
            try:
                fd = os.open(str(record.approval_id) + '.json',
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=claims)
            except FileExistsError:
                raise IntakeRejected(FailureCategory.TRANSMISSION_CONSUMED) from None
            # Never unlink, including partial write/fsync failure. An incomplete
            # claim remains consumed and can never release source or start a model.
            try:
                storage._private(fd, False)
                remaining = memoryview(record.model_dump_json().encode())
                while remaining:
                    count = os.write(fd, remaining)
                    if count <= 0:
                        raise OSError('Claim write failed')
                    remaining = remaining[count:]
                os.fsync(fd)
                os.fsync(claims)
            finally:
                os.close(fd)
    except IntakeRejected:
        raise
    except (OSError, ValueError, TypeError):
        raise IntakeRejected(FailureCategory.STORAGE) from None


@contextmanager
def execution_scope(snapshot, approval, *, run_id: UUID, identity: tuple[str, str], specialists, execution_profile="standard"):
    """Consume exactly once, before constructing providers/tools/real agents."""
    specialists = checked_specialists(specialists)
    require_transmission(snapshot, approval, identity=identity, specialists=specialists, execution_profile=execution_profile)
    if not isinstance(run_id, UUID) or _ACTIVE.get() is not None:
        _reject()
    permit = TransmissionApproval.model_validate(approval.model_dump(warnings=False))
    if permit.execution_profile is not None:
        # Named authority must match the durable supported authorization receipt,
        # not just a caller's copy containing another valid registry entry.
        from . import real_source as storage
        try:
            with storage._store() as store, storage._child(store,'imports') as imports, \
                    storage._child(imports,str(permit.import_approval_id)) as bundle, \
                    storage._child(bundle,'receipts') as receipts:
                stored = TransmissionApproval.model_validate(json.loads(storage._read(receipts,
                    'transmission-'+str(permit.approval_id)+'.json',32768),object_pairs_hook=storage._unique_object))
            if stored != permit:
                _reject()
        except (OSError,ValueError,TypeError):
            _reject()
    record = ExecutionClaim(execution_profile=checked_profile(execution_profile), approval_id=permit.approval_id, import_approval_id=permit.import_approval_id,
        run_id=run_id, manifest_digest=permit.manifest_digest, approval_digest=_digest(permit),
        snapshot_id=snapshot.manifest.snapshot_id, snapshot_revision=snapshot.manifest.revision,
        provider=permit.provider, model=permit.model, specialists=permit.specialists,
        requested_specialists=specialists, claimed_at=datetime.now(timezone.utc))
    _claim(record)
    state = _Execution(record)
    token = _ACTIVE.set(state)
    try:
        require_execution(snapshot, permit, run_id=run_id)
        yield record
    finally:
        state.active = False
        _ACTIVE.reset(token)


def require_execution(snapshot, approval, *, run_id=None, specialists=None, execution_profile=None):
    """Repeat identity/expiry checks; a persisted record alone is not a capability."""
    from . import real_source as storage
    require_transmission(snapshot, approval, specialists=specialists, execution_profile=execution_profile)
    state = _ACTIVE.get()
    if (state is None or state.pid != os.getpid() or not state.active or state.finished
            or run_id is not None and state.claim.run_id != run_id):
        _reject()
    try:
        permit = TransmissionApproval.model_validate(approval.model_dump(warnings=False))
        if (state.claim.execution_profile != approval_profile(permit)
                or state.claim.approval_digest != _digest(permit) or state.claim.approval_id != permit.approval_id
                or state.claim.specialists != permit.specialists
                or not set(state.claim.requested_specialists) <= set(permit.specialists)
                or specialists is not None and not set(checked_specialists(specialists)) <= set(state.claim.requested_specialists)
                or state.claim.snapshot_id != snapshot.manifest.snapshot_id
                or state.claim.snapshot_revision != snapshot.manifest.revision):
            _reject()
        with storage._store() as store, storage._child(store, 'transmission_claims') as claims:
            stored = ExecutionClaim.model_validate(json.loads(storage._read(claims,
                str(permit.approval_id) + '.json', 8192), object_pairs_hook=storage._unique_object))
        if stored != state.claim:
            _reject()
    except IntakeRejected:
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        _reject()
    return state


def specialist_identity(agent):
    """Derive policy from the closed implementation registry, not instance labels."""
    from .orchestrator import SPECIALIST_SPECS
    for spec in SPECIALIST_SPECS:
        if type(agent) is spec.agent_type:
            return spec.name
    _reject()


def require_tool_execution(state, specialist):
    worker = _WORKER.get()
    if worker is not None:
        if not worker.active or worker.execution is not state or worker.specialist != specialist:
            _reject()
    elif state.started or state.claim.requested_specialists != (specialist,):
        _reject()


def require_agent_execution(snapshot, approval, *, specialist, job_id):
    state = require_execution(snapshot, approval, specialists=(specialist,))
    worker = _WORKER.get()
    if worker is None or worker.job_id != job_id:
        _reject()
    require_tool_execution(state, specialist)


@contextmanager
def agent_execution(snapshot, approval, *, review_id, job_id, specialist):
    if snapshot.real_source is None:
        yield
        return
    state = require_execution(snapshot, approval, run_id=review_id, specialists=(specialist,))
    if _WORKER.get() is not None:
        _reject()
    direct = state.jobs is None
    with state.lock:
        if direct:
            if state.started:
                _reject()
            state.started = True
        elif state.jobs.get(job_id) != specialist or job_id in state.used_jobs:
            _reject()
        state.used_jobs.add(job_id)
    worker = _Worker(state, specialist, job_id)
    token = _WORKER.set(worker)
    try:
        yield
    finally:
        worker.active = False
        _WORKER.reset(token)
        if direct:
            state.finished = True


@contextmanager
def orchestration_execution(snapshot, approval, *, review_id, jobs):
    if snapshot.real_source is None:
        yield
        return
    specialists = checked_specialists(tuple(jobs))
    require_transmission(snapshot, approval, specialists=specialists)
    scope = (execution_scope(snapshot, approval, run_id=review_id, identity=('nebius', MODEL), specialists=specialists)
             if _ACTIVE.get() is None else nullcontext())
    with scope:
        state = require_execution(snapshot, approval, run_id=review_id, specialists=specialists)
        with state.lock:
            if state.started:
                _reject()
            state.started = True
            state.jobs = {job: name for name, job in jobs.items()}
        try:
            yield
        finally:
            state.finished = True
