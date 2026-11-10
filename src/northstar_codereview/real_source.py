"""Fixed-scope future intake. No discovery, execution, providers, or CLI.

Only a hash-bound, approved import receipt permits opening a configured source
root. Tests substitute generated project-local roots, never original projects.
Public diagnostics contain categories only. Bundle writes occur after scanning.
"""

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from .local_source import (PROJECT_ROOT, CollectedSnapshot, IntakeLimits, SourceIntakeError,
                           _Collector, _assemble, _directory, _text)
from .models import SourceSide
from .real_source_privacy import privacy_check
from .real_source_authorization import require_transmission, verify_real_snapshot
from .real_source_contracts import (SAMPLES, FailureCategory as F, ImportApproval, ImportResult,
    IntakeManifest, IntakeRejected, RealSourceContext, SampleId, SourceProject,
    TransmissionApproval, checked_import)


_SOURCE_ROOTS = {
    SourceProject.PORTFOLIO: PROJECT_ROOT.parent / "Week1-project" / "stock-portfolio-analyzer",
    SourceProject.ATLAS: PROJECT_ROOT.parent / "northstar-atlas",
}
_STORE = PROJECT_ROOT / ".runtime" / "real_source"
_LIMITS = IntakeLimits()
_MAX_BUNDLE = 64 * 1024 * 1024  # JSON escaping is bounded; decoded source remains <=10 MiB.


@contextmanager
def _absolute_directory(path: Path):
    # Walk from the filesystem root, rejecting symlinks at EVERY component.
    if not path.is_absolute() or any(p in (".", "..") for p in path.parts[1:]):
        raise IntakeRejected(F.UNSAFE_FILE)
    with ExitStack() as stack:
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        stack.callback(os.close, fd)
        for part in path.parts[1:]:
            fd = stack.enter_context(_directory(fd, part))
        yield fd


def _capture(sample: SampleId) -> CollectedSnapshot:
    project, paths = SAMPLES[sample]
    with _absolute_directory(_SOURCE_ROOTS[project]) as root:
        collector = _Collector(_LIMITS, root)
        for path in paths:
            if Path(path).suffix != ".py":
                raise IntakeRejected(F.UNSAFE_FILE)
            with ExitStack() as stack:
                parent = root
                parts = path.split("/")
                for index, part in enumerate(parts[:-1], 1):
                    parent = stack.enter_context(_directory(parent, part))
                    collector.remember_directory("/".join(parts[:index]), parent)
                collector.visit(parent, parts[-1], path, len(parts), file_only=True)
        collector.revalidate()
        snapshot = _assemble(collector.sources)
    return snapshot


def _private(fd: int, directory: bool) -> None:
    info = os.fstat(fd)
    if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        raise IntakeRejected(F.STORAGE)


@contextmanager
def _child(parent: int, name: str, *, create=False):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    with _directory(parent, name) as fd:
        _private(fd, True)
        yield fd


@contextmanager
def _store(*, create=False):
    # Destination is fixed and project-confined; test substitutions must be too.
    relative = _STORE.relative_to(PROJECT_ROOT)
    if not relative.parts or any(p in (".", "..") for p in relative.parts):
        raise IntakeRejected(F.STORAGE)
    with _absolute_directory(PROJECT_ROOT) as project, ExitStack() as stack:
        fd = project
        for index, part in enumerate(relative.parts):
            # .runtime may already be 0755, but real_source and everything below
            # must be private. Never chmod an existing directory behind the user.
            if index < len(relative.parts) - 1:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                fd = stack.enter_context(_directory(fd, part))
            else:
                fd = stack.enter_context(_child(fd, part, create=create))
        yield fd


def _write(parent: int, name: str, payload: bytes) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=parent)
    try:
        _private(fd, False)
        remaining = memoryview(payload)
        while remaining:
            count = os.write(fd, remaining)
            if count <= 0:
                raise OSError("write failed")
            remaining = remaining[count:]
        os.fsync(fd)
    except BaseException:
        # The descriptor was created exclusively by this call. Never leave an
        # incomplete claim/receipt after write, fsync or cancellation failure.
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        owned = os.fstat(fd)
        if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
            os.unlink(name, dir_fd=parent)
        raise
    finally:
        os.close(fd)


def _read(parent: int, name: str, maximum: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    try:
        _private(fd, False)
        before = os.fstat(fd)
        if before.st_size > maximum:
            raise IntakeRejected(F.SIZE)
        chunks = bytearray()
        while len(chunks) <= maximum:
            chunk = os.read(fd, min(65536, maximum + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        after = os.fstat(fd)
        if (len(chunks) > maximum or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise IntakeRejected(F.INTEGRITY)
        return bytes(chunks)
    finally:
        os.close(fd)


def _remove_tree(parent: int, name: str) -> None:
    """Remove only our staging entry, descriptor-relative; never follow links."""
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=parent)
        return
    with _directory(parent, name) as fd:
        for child in os.listdir(fd):
            _remove_tree(fd, child)
    os.rmdir(name, dir_fd=parent)


def _commit(snapshot: CollectedSnapshot, approval: ImportApproval, *, renewal: dict | None = None) -> None:
    identifier = str(approval.approval_id)
    stage = ".pending-" + uuid4().hex
    with _store(create=True) as store, _child(store, "imports", create=True) as imports, \
            _child(store, "claims", create=True) as claims:
        # Claim is never replaced; simultaneous/conflicting approvals fail closed.
        try:
            _write(claims, identifier, b"claimed\n")
        except FileExistsError:
            raise IntakeRejected(F.CONFLICT) from None
        committed = False
        staged = False
        try:
            if identifier in os.listdir(imports):
                raise IntakeRejected(F.CONFLICT)
            os.mkdir(stage, 0o700, dir_fd=imports)
            staged = True
            with _child(imports, stage) as bundle:
                for name in ("manifests", "snapshots", "receipts", "review_artifacts"):
                    with _child(bundle, name, create=True) as folder:
                        if name == "manifests":
                            _write(folder, "intake.json", approval.manifest.model_dump_json().encode())
                        elif name == "snapshots":
                            payload = {p: text for (_, p), text in snapshot.sources.items()}
                            _write(folder, "source.json", json.dumps(payload, ensure_ascii=False).encode())
                        elif name == "receipts":
                            _write(folder, "import.json", approval.model_dump_json().encode())
                            if renewal is not None:
                                _write(folder, "renewal.json", json.dumps(renewal, sort_keys=True).encode())
                        os.fsync(folder)
                os.fsync(bundle)
            # Recheck expiry immediately before promotion. No partial bundle is
            # visible through load_import(), which accepts only UUID names.
            checked_import(approval, importing=True)
            os.rename(stage, identifier, src_dir_fd=imports, dst_dir_fd=imports)
            committed = True
            os.fsync(imports)
        finally:
            if not committed:
                if staged:
                    _remove_tree(imports, stage)
                os.unlink(identifier, dir_fd=claims)


def _classify_source(error: SourceIntakeError) -> F:
    if error.code in ("binary_source", "invalid_utf8"):
        return F.BINARY
    if error.code in ("byte_limit", "file_count_limit", "entry_limit", "depth_limit"):
        return F.SIZE
    return F.INTEGRITY if error.code == "source_changed" else F.UNSAFE_FILE


def import_sample(sample: SampleId, *, approval: ImportApproval | None) -> ImportResult:
    """Future operator API: no root/path/extension/budget override arguments."""
    receipt = checked_import(approval, importing=True)
    try:
        sample = SampleId(sample)
        if sample != receipt.manifest.sample:
            raise IntakeRejected(F.SCOPE)
        snapshot = _capture(sample)
        if snapshot.manifest != receipt.manifest.snapshot:
            raise IntakeRejected(F.INTEGRITY)
        for text in snapshot.sources.values():
            privacy_check(text)
        _commit(snapshot, receipt)
        return ImportResult(approval_id=receipt.approval_id, manifest_digest=receipt.manifest.digest,
                            snapshot=snapshot.manifest)
    except IntakeRejected:
        raise
    except SourceIntakeError as error:
        raise IntakeRejected(_classify_source(error)) from None
    except (OSError, ValueError, KeyError, TypeError):
        raise IntakeRejected(F.STORAGE) from None


def load_import(approval_id: UUID) -> CollectedSnapshot:
    """Verify every persisted binding; loading grants no model authorization."""
    snapshot = _read_bundle(approval_id)
    verify_real_snapshot(snapshot)
    return snapshot


def _read_bundle(approval_id: UUID) -> CollectedSnapshot:
    """Private archival integrity read, not permission to use expired content.

    Only the normal loader and explicit retention replacement consume this. A
    returned expired context still fails every model/tool authorization boundary.
    """
    try:
        identifier = str(UUID(str(approval_id)))
        with _store() as store, _child(store, "imports") as imports, _child(imports, identifier) as bundle:
            with _child(bundle, "receipts") as receipts:
                receipt = ImportApproval.model_validate(json.loads(
                    _read(receipts, "import.json", 32768), object_pairs_hook=_unique_object))
            if receipt.state.value != "approved" or receipt.approved_at > datetime.now(timezone.utc):
                raise IntakeRejected(F.AUTHORIZATION)
            if str(receipt.approval_id) != identifier:
                raise IntakeRejected(F.INTEGRITY)
            with _child(bundle, "manifests") as manifests:
                manifest = IntakeManifest.model_validate(json.loads(
                    _read(manifests, "intake.json", 32768), object_pairs_hook=_unique_object))
            if manifest != receipt.manifest:
                raise IntakeRejected(F.INTEGRITY)
            with _child(bundle, "snapshots") as snapshots:
                sources = json.loads(_read(snapshots, "source.json", _MAX_BUNDLE), object_pairs_hook=_unique_object)
            if type(sources) is not dict or set(sources) != set(SAMPLES[manifest.sample][1]):
                raise IntakeRejected(F.SCOPE)
            total = 0
            for text in sources.values():
                if type(text) is not str:
                    raise IntakeRejected(F.INTEGRITY)
                raw = text.encode("utf-8")
                total += len(raw)
                if len(raw) > _LIMITS.max_file_bytes or total > _LIMITS.max_total_bytes:
                    raise IntakeRejected(F.SIZE)
                _text(raw)
                privacy_check(text)
            snapshot = CollectedSnapshot(manifest.snapshot, {(SourceSide.HEAD, p): v for p, v in sources.items()},
                                         RealSourceContext(import_approval=receipt))
            if _assemble(snapshot.sources).manifest != snapshot.manifest:
                raise IntakeRejected(F.INTEGRITY)
        return snapshot
    except IntakeRejected:
        raise
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        raise IntakeRejected(F.STORAGE) from None


def renew_import(approval_id: UUID, *, expected_digest: str, allow_renewal: bool = False,
                 days: int = 30) -> ImportResult:
    """Explicit local replacement, never reimport or a transmission extension.

    Preserve the prior bundle; bind a new operator receipt to verified identical
    bytes and retain a lineage record atomically in the replacement bundle.
    """
    if allow_renewal is not True or type(days) is not int or not 1 <= days <= 365:
        raise IntakeRejected(F.AUTHORIZATION)
    previous = _read_bundle(approval_id)
    old = previous.real_source.import_approval
    if old.manifest.digest != expected_digest:
        raise IntakeRejected(F.INTEGRITY)
    now = datetime.now(timezone.utc)
    manifest = IntakeManifest.model_validate({**old.manifest.model_dump(), "retain_until": now + timedelta(days=days)})
    receipt = ImportApproval(approval_id=uuid4(), manifest=manifest, state="approved",
        approved_at=now, expires_at=now + timedelta(hours=1), privacy_reviewed=True)
    renewed = CollectedSnapshot(previous.manifest, previous.sources, RealSourceContext(import_approval=receipt))
    verify_real_snapshot(renewed)
    try:
        _commit(renewed, receipt, renewal={"operation": "local_retention_replacement",
            "previous_import_id": str(old.approval_id), "previous_manifest_digest": old.manifest.digest,
            "replacement_import_id": str(receipt.approval_id), "replacement_manifest_digest": manifest.digest,
            "authorized_at": now.isoformat(), "retention_days": days,
            "model_transmission_authorized": False})
    except (OSError, ValueError):
        raise IntakeRejected(F.STORAGE) from None
    return ImportResult(approval_id=receipt.approval_id, manifest_digest=manifest.digest, snapshot=renewed.manifest)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise IntakeRejected(F.INTEGRITY)
        result[key] = value
    return result


def read_import_approval(approval_id: UUID) -> ImportApproval:
    """Load current metadata from the fixed private inbox, without source reads.

    A UUID selects one record, never an arbitrary path. Existing claims/bundles
    reject reuse early; the atomic importer remains the final concurrency gate.
    This function neither creates approval nor grants model transmission.
    """
    try:
        identifier = str(UUID(str(approval_id)))
        with _store() as store, _child(store, "approvals") as approvals:
            name = identifier + ".json"
            before = os.stat(name, dir_fd=approvals, follow_symlinks=False)
            raw = _read(approvals, name, 32768)
            after = os.stat(name, dir_fd=approvals, follow_symlinks=False)
            from .local_source import _stamp
            if _stamp(before) != _stamp(after):
                raise IntakeRejected(F.AUTHORIZATION)
            receipt = checked_import(ImportApproval.model_validate(
                json.loads(raw, object_pairs_hook=_unique_object)), importing=True)
            if str(receipt.approval_id) != identifier:
                raise IntakeRejected(F.AUTHORIZATION)
            for area in ("claims", "imports"):
                try:
                    with _child(store, area) as folder:
                        try:
                            os.stat(identifier, dir_fd=folder, follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        raise IntakeRejected(F.CONFLICT)
                except FileNotFoundError:
                    pass
        return receipt
    except IntakeRejected:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        raise IntakeRejected(F.AUTHORIZATION) from None


def record_transmission(approval: TransmissionApproval) -> None:
    """Store a separately approved receipt; no transmission or provider startup."""
    try:
        approval = TransmissionApproval.model_validate(approval.model_dump(warnings=False))
        snapshot = load_import(approval.import_approval_id)
        require_transmission(snapshot, approval)
        with _store() as store, _child(store, "imports") as imports, \
                _child(imports, str(approval.import_approval_id)) as bundle, _child(bundle, "receipts") as receipts:
            # Private staging file then atomic link provides no-replace commit.
            stage = ".pending-" + uuid4().hex
            try:
                _write(receipts, stage, approval.model_dump_json().encode())
                os.link(stage, "transmission-" + str(approval.approval_id) + ".json",
                        src_dir_fd=receipts, dst_dir_fd=receipts, follow_symlinks=False)
            finally:
                if stage in os.listdir(receipts):
                    os.unlink(stage, dir_fd=receipts)
            os.fsync(receipts)
    except IntakeRejected:
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        raise IntakeRejected(F.STORAGE) from None
