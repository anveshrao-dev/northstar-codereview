"""Bounded POSIX intake for the editable local project; never executes source.

The application chooses source_root, not a model. Descriptor-relative opens and
identity rechecks reduce races but cannot promise a transactional filesystem view.
"""

import json
import os
import stat
from collections.abc import Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Annotated
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, TypeAdapter

from .models import Contract, FilePath, ReviewMode, ReviewRequest, SourceFile, SourceSide, SourceSnapshot
from .real_source_contracts import RealSourceContext


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FILE_PATH = TypeAdapter(FilePath)
_SELECTION = TypeAdapter(Annotated[tuple[FilePath, ...], Field(min_length=1, max_length=50)])
_SUPPORTED = (
    os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.scandir in os.supports_fd
    and all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK", "O_CLOEXEC"))
)


class SourceIntakeError(ValueError):
    """A fixed public reason, without raw exceptions, source text, or host paths."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"Source intake rejected input ({code}).")


class IntakeLimits(Contract):
    """Trusted caller may lower the conservative MVP limits, not raise them."""

    max_files: int = Field(default=50, strict=True, ge=1, le=50)
    max_file_bytes: int = Field(default=1024 * 1024, strict=True, ge=1, le=1024 * 1024)
    max_total_bytes: int = Field(default=10 * 1024 * 1024, strict=True, ge=1, le=10 * 1024 * 1024)
    max_entries: int = Field(default=500, strict=True, ge=1, le=500)
    max_depth: int = Field(default=16, strict=True, ge=1, le=16)


@dataclass(frozen=True, slots=True)
class CollectedSnapshot:
    """Trusted application-owned bundle; it is not a model input schema.

    The collector copies text into a read-only mapping; tools never revisit disk.
    The (side, path) keys allow later BASE-side adapters without a diff engine.
    """

    manifest: SourceSnapshot
    sources: Mapping[tuple[SourceSide, str], str]
    real_source: RealSourceContext | None = None

    def __post_init__(self) -> None:
        # frozen=True prevents attribute reassignment, not aliasing of a supplied dict.
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))


def _checked_path(path: str) -> str:
    try:
        value = _FILE_PATH.validate_python(path)
        if len(value.encode("utf-8")) > 512:
            raise ValueError("path too long")
        return value
    except (ValueError, UnicodeError):
        raise SourceIntakeError("unsafe_path") from None


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_mode


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


@contextmanager
def _directory(parent_fd: int, name: str):
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise SourceIntakeError("unsafe_directory")
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    try:
        if _identity(before) != _identity(os.fstat(fd)):
            raise SourceIntakeError("source_changed")
        yield fd
        if _identity(before) != _identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)):
            raise SourceIntakeError("source_changed")
    finally:
        os.close(fd)


def _text(raw: bytes) -> str:
    # Conservative text policy, not a universal binary format detector.
    if raw.startswith((b"%PDF-", b"GIF87a", b"GIF89a", b"PK\x03\x04", b"\x7fELF")):
        raise SourceIntakeError("binary_source")
    if any(byte < 32 and byte not in (9, 10, 13) or byte == 127 for byte in raw):
        raise SourceIntakeError("binary_source")
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise SourceIntakeError("invalid_utf8") from None


def _assemble(sources: Mapping[tuple[SourceSide, str], str]) -> CollectedSnapshot:
    files = tuple(
        SourceFile(file_path=path, side=side, sha256=sha256(text.encode("utf-8")).hexdigest())
        for (side, path), text in sorted(sources.items())
    )
    canonical = json.dumps([item.model_dump(mode="json") for item in files], sort_keys=True, separators=(",", ":"))
    revision = sha256(b"northstar-source-manifest-v1\n" + canonical.encode("utf-8")).hexdigest()
    manifest = SourceSnapshot(
        snapshot_id=uuid5(NAMESPACE_URL, "northstar-source-manifest-v1:" + revision),
        revision=revision,
        files=files,
    )
    return CollectedSnapshot(manifest, sources)


class _Collector:
    def __init__(self, limits: IntakeLimits, root_fd: int):
        self.limits = limits
        self.root_fd = root_fd
        self.sources: dict[tuple[SourceSide, str], str] = {}
        self.total_bytes = 0
        self.entries = 0
        self.observations: list[tuple[str, tuple[int, ...]]] = []
        self.directory_identities: dict[str, tuple[int, ...]] = {"": _identity(os.fstat(root_fd))}
        self.directory_stamps: dict[str, tuple[int, ...]] = {}

    def remember_directory(self, path: str, fd: int) -> None:
        identity = _identity(os.fstat(fd))
        if self.directory_identities.setdefault(path, identity) != identity:
            raise SourceIntakeError("source_changed")

    def visit(self, parent: int, name: str, path: str, depth: int, *, file_only: bool = False) -> None:
        _checked_path(path)
        if depth > self.limits.max_depth:
            raise SourceIntakeError("depth_limit")
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode) and not file_only:
            with _directory(parent, name) as fd:
                self.walk(fd, path, depth)
        elif stat.S_ISREG(info.st_mode):
            self.read_file(parent, name, path, info)
        else:
            raise SourceIntakeError("unsupported_file_type")

    def walk(self, fd: int, prefix: str, depth: int) -> None:
        self.remember_directory(prefix, fd)
        before = _stamp(os.fstat(fd))
        self.directory_stamps[prefix] = before
        names = []
        with os.scandir(fd) as iterator:
            for entry in iterator:
                self.entries += 1
                if self.entries > self.limits.max_entries:
                    raise SourceIntakeError("entry_limit")
                names.append(entry.name)
        for name in sorted(names):
            self.visit(fd, name, f"{prefix}/{name}" if prefix else name, depth + 1)
        if _stamp(os.fstat(fd)) != before:
            raise SourceIntakeError("source_changed")

    def read_file(self, parent: int, name: str, path: str, before: os.stat_result) -> None:
        key = (SourceSide.HEAD, path)
        if key in self.sources:
            raise SourceIntakeError("duplicate_path")
        if len(self.sources) >= self.limits.max_files:
            raise SourceIntakeError("file_count_limit")
        if before.st_nlink != 1:
            raise SourceIntakeError("hard_link")
        remaining = min(self.limits.max_file_bytes, self.limits.max_total_bytes - self.total_bytes)
        if before.st_size > remaining:
            raise SourceIntakeError("byte_limit")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        fd = os.open(name, flags, dir_fd=parent)
        try:
            if _stamp(os.fstat(fd)) != _stamp(before):
                raise SourceIntakeError("source_changed")
            chunks = []
            size = 0
            while True:
                chunk = os.read(fd, min(65536, remaining + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > remaining:
                    raise SourceIntakeError("byte_limit")
                chunks.append(chunk)
            if _stamp(os.fstat(fd)) != _stamp(before):
                raise SourceIntakeError("source_changed")
            if _stamp(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _stamp(before):
                raise SourceIntakeError("source_changed")
        finally:
            os.close(fd)
        raw = b"".join(chunks)
        self.sources[key] = _text(raw)
        self.total_bytes += len(raw)
        self.observations.append((path, _stamp(before)))

    @contextmanager
    def reopen_directory(self, path: str):
        """Rewalk from the held source root; old directory FDs alone are insufficient."""
        with ExitStack() as stack:
            fd = self.root_fd
            prefix = []
            for part in path.split("/") if path else ():
                prefix.append(part)
                fd = stack.enter_context(_directory(fd, part))
                if _identity(os.fstat(fd)) != self.directory_identities["/".join(prefix)]:
                    raise SourceIntakeError("source_changed")
            yield fd

    def revalidate(self) -> None:
        for path, identity in self.directory_identities.items():
            with self.reopen_directory(path) as fd:
                info = os.fstat(fd)
                if _identity(info) != identity:
                    raise SourceIntakeError("source_changed")
                if path in self.directory_stamps and _stamp(info) != self.directory_stamps[path]:
                    raise SourceIntakeError("source_changed")
        for path, stamp in self.observations:
            parent_path, _, name = path.rpartition("/")
            with self.reopen_directory(parent_path) as parent:
                if _stamp(os.stat(name, dir_fd=parent, follow_symlinks=False)) != stamp:
                    raise SourceIntakeError("source_changed")


def collect_local_snapshot(
    request: ReviewRequest,
    *,
    source_root: Path,
    selected_files: Sequence[str] | None = None,
    limits: IntakeLimits | None = None,
) -> CollectedSnapshot:
    """Collect HEAD text beneath an explicitly approved project-local root.

    selected_files are root-relative files within request.local_path; directories
    are not selections. Without a selection, collect the requested file/tree.
    Forbidden entries reject the collection rather than being silently omitted.
    Missing or unsupported descriptor protections fail closed.
    """
    if not _SUPPORTED:
        raise SourceIntakeError("platform_unavailable")
    if not isinstance(request, ReviewRequest) or request.mode != ReviewMode.LOCAL:
        raise SourceIntakeError("local_request_required")
    root = Path(source_root)
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    try:
        root_parts = root.relative_to(PROJECT_ROOT).parts
        if any(part in {".", ".."} or part.casefold() in {".git", ".ssh", ".aws", ".venv"} or part.casefold().startswith(".env") for part in root_parts):
            raise ValueError("unsafe root")
        selections = _SELECTION.validate_python(selected_files) if selected_files is not None else None
        if selections is not None and len(set(selections)) != len(selections):
            raise ValueError("duplicate selection")
        scope = request.local_path
        if scope is None:
            raise ValueError("missing scope")
        if selections is not None and scope != "." and any(path != scope and not path.startswith(scope + "/") for path in selections):
            raise ValueError("selection exceeds scope")
    except (ValueError, TypeError):
        raise SourceIntakeError("unsafe_selection") from None
    try:
        with ExitStack() as stack:
            project_fd = os.open(PROJECT_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            stack.callback(os.close, project_fd)
            root_fd = project_fd
            for part in root_parts:
                root_fd = stack.enter_context(_directory(root_fd, part))
            collector = _Collector(limits or IntakeLimits(), root_fd)
            for target in selections if selections is not None else (scope,):
                if target == ".":
                    collector.walk(root_fd, "", 0)
                    continue
                _checked_path(target)
                with ExitStack() as parents:
                    parent_fd = root_fd
                    parts = target.split("/")
                    if len(parts) > collector.limits.max_depth:
                        raise SourceIntakeError("depth_limit")
                    for index, part in enumerate(parts[:-1], start=1):
                        parent_fd = parents.enter_context(_directory(parent_fd, part))
                        collector.remember_directory("/".join(parts[:index]), parent_fd)
                    collector.visit(parent_fd, parts[-1], target, len(parts), file_only=selections is not None)
            if not collector.sources:
                raise SourceIntakeError("no_source_files")
            collector.revalidate()
            bundle = _assemble(collector.sources)
        return bundle
    except SourceIntakeError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise SourceIntakeError("source_unavailable_or_changed") from None
