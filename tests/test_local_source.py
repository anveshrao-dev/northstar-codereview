"""Filesystem attack fixtures stay within pytest's project-local temporary root."""

import os
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from northstar_codereview import local_source as intake
from northstar_codereview.models import ReviewRequest


@pytest.fixture
def source_root(tmp_path):
    assert tmp_path.resolve().is_relative_to(Path(__file__).resolve().parents[1])
    root = tmp_path / "northstar"
    root.mkdir()
    return root


def collect(root, target=".", **kwargs):
    return intake.collect_local_snapshot(ReviewRequest(mode="local", local_path=target), source_root=root, **kwargs)


def test_collects_nested_text_without_executing_or_writing_it(source_root):
    (source_root / "src").mkdir()
    path = source_root / "src" / "sample.py"
    raw = b"raise RuntimeError('Source must never execute')\r\n"
    path.write_bytes(raw)
    before = path.stat()
    bundle = collect(source_root)
    assert bundle.sources[("head", "src/sample.py")].encode("utf-8") == raw
    entry = bundle.manifest.files[0]
    assert entry.file_path == "src/sample.py"
    assert entry.sha256 == sha256(raw).hexdigest()
    assert (path.stat().st_mtime_ns, path.stat().st_size) == (before.st_mtime_ns, before.st_size)
    assert path.read_bytes() == raw


@pytest.mark.parametrize("raw", [b"", b"a\nb\n", b"a\r\nb\r\n", b"final line", b"\xef\xbb\xbfhello\n"])
def test_preserves_exact_utf8_bytes_including_empty_files(source_root, raw):
    (source_root / "file.txt").write_bytes(raw)
    bundle = collect(source_root, "file.txt")
    assert bundle.sources[("head", "file.txt")].encode("utf-8") == raw
    assert bundle.manifest.files[0].sha256 == sha256(raw).hexdigest()


def test_snapshot_identity_is_order_and_root_independent(source_root, tmp_path):
    (source_root / "b.py").write_bytes(b"b\n")
    (source_root / "a.py").write_bytes(b"a\n")
    other = tmp_path / "second"
    other.mkdir()
    (other / "a.py").write_bytes(b"a\n")
    (other / "b.py").write_bytes(b"b\n")
    first = collect(source_root, selected_files=["b.py", "a.py"])
    second = collect(other, selected_files=["a.py", "b.py"])
    assert first.manifest == second.manifest
    assert [f.file_path for f in first.manifest.files] == ["a.py", "b.py"]
    (other / "a.py").write_bytes(b"changed\n")
    assert collect(other).manifest.snapshot_id != first.manifest.snapshot_id
    (source_root / "a.py").rename(source_root / "renamed.py")
    assert collect(source_root).manifest.snapshot_id != first.manifest.snapshot_id


def test_selected_files_do_not_expand_to_unselected_siblings(source_root):
    (source_root / "src").mkdir()
    (source_root / "src" / "a.py").write_bytes(b"a\n")
    (source_root / "bad.bin").write_bytes(b"\x00")
    bundle = collect(source_root, "src", selected_files=["src/a.py"])
    assert [f.file_path for f in bundle.manifest.files] == ["src/a.py"]


@pytest.mark.parametrize("selection", [["a.py", "a.py"], [], ["../outside.py"], ["/outside.py"], ["src"]])
def test_rejects_invalid_or_directory_selections(source_root, selection):
    (source_root / "a.py").write_text("a")
    (source_root / "src").mkdir()
    with pytest.raises(intake.SourceIntakeError):
        collect(source_root, selected_files=selection)


def test_selection_cannot_expand_request_scope(source_root):
    (source_root / "src").mkdir()
    (source_root / "elsewhere.py").write_text("a")
    with pytest.raises(intake.SourceIntakeError):
        collect(source_root, "src", selected_files=["elsewhere.py"])


@pytest.mark.parametrize("raw", [b"\x00hello", b"\xff\xfe", b"valid\x01text", b"\x89PNG\r\n\x1a\n"])
def test_rejects_binary_or_invalid_utf8(source_root, raw):
    (source_root / "file.txt").write_bytes(raw)
    with pytest.raises(intake.SourceIntakeError):
        collect(source_root)


@pytest.mark.parametrize("name", [".git", ".aws", ".ssh", ".venv", ".cache", ".runtime", ".env", ".env.local"])
def test_rejects_forbidden_tree_entries(source_root, name):
    path = source_root / name
    path.mkdir()
    (path / "synthetic.txt").write_text("fictional fixture")
    with pytest.raises(intake.SourceIntakeError):
        collect(source_root)


@pytest.mark.parametrize("target", ["../outside", "/outside", "src/../a.py", "C:\\outside", "src//a.py"])
def test_request_traversal_is_rejected_before_collection(target):
    with pytest.raises(ValidationError):
        ReviewRequest(mode="local", local_path=target)


def test_rejects_an_authorized_root_outside_project_without_opening_it(monkeypatch):
    def unexpected_open(*args, **kwargs):
        pytest.fail("An out-of-project root must be rejected before any file open")
    monkeypatch.setattr(intake.os, "open", unexpected_open)
    with pytest.raises(intake.SourceIntakeError):
        collect(Path("/not-an-authorized-project"))


@pytest.mark.parametrize("kind", ["file", "directory", "root", "intermediate", "dangling"])
def test_rejects_symlinked_files_directories_and_roots(source_root, tmp_path, kind):
    outside = tmp_path / "outside_snapshot"
    outside.mkdir()
    (outside / "file.py").write_text("fictional outside snapshot")
    if kind == "root":
        link = tmp_path / "linked_root"
        link.symlink_to(source_root, target_is_directory=True)
        target_root, target = link, "."
    elif kind == "intermediate":
        link = tmp_path / "linked_parent"
        link.symlink_to(tmp_path, target_is_directory=True)
        target_root, target = link / "northstar", "."
    else:
        (source_root / "link").symlink_to(outside / "file.py" if kind == "file" else outside if kind == "directory" else outside / "missing")
        target_root, target = source_root, "."
    with pytest.raises(intake.SourceIntakeError):
        collect(target_root, target)


def test_rejects_hard_linked_source(source_root, tmp_path):
    original = tmp_path / "original.py"
    original.write_text("fictional")
    os.link(original, source_root / "alias.py")
    with pytest.raises(intake.SourceIntakeError):
        collect(source_root)


def test_rejects_fifo_without_blocking(source_root):
    os.mkfifo(source_root / "pipe")
    with pytest.raises(intake.SourceIntakeError):
        collect(source_root)


@pytest.mark.parametrize("limits,files", [
    ({"max_file_bytes": 4}, {"a": b"12345"}),
    ({"max_total_bytes": 5}, {"a": b"123", "b": b"456"}),
    ({"max_files": 1}, {"a": b"1", "b": b"2"}),
])
def test_rejects_exceeded_input_budgets(source_root, limits, files):
    for name, data in files.items():
        (source_root / name).write_bytes(data)
    with pytest.raises(intake.SourceIntakeError):
        collect(source_root, limits=intake.IntakeLimits(**limits))


def test_accepts_exact_byte_and_count_limits(source_root):
    (source_root / "a").write_bytes(b"1234")
    bundle = collect(source_root, limits=intake.IntakeLimits(max_file_bytes=4, max_total_bytes=4, max_files=1))
    assert bundle.sources[("head", "a")] == "1234"


@pytest.mark.parametrize("limit", ["max_entries", "max_depth"])
def test_bounds_directory_work_even_without_files(source_root, limit):
    (source_root / "a" / "b" / "c").mkdir(parents=True)
    with pytest.raises(intake.SourceIntakeError):
        collect(source_root, limits=intake.IntakeLimits(**{limit: 1}))


def test_rejects_empty_directory_and_missing_target(source_root):
    for target in (".", "missing.py"):
        with pytest.raises(intake.SourceIntakeError):
            collect(source_root, target)


def test_rejects_nonlocal_request(source_root):
    with pytest.raises(intake.SourceIntakeError):
        intake.collect_local_snapshot(ReviewRequest(mode="github", repository="northstar/demo", pr_number=1), source_root=source_root)


def test_detects_content_mutation_during_read(source_root, monkeypatch):
    path = source_root / "a.py"
    path.write_bytes(b"before\n")
    real_read = os.read
    changed = False
    def mutate_after_read(fd, size):
        nonlocal changed
        chunk = real_read(fd, size)
        if chunk and not changed:
            changed = True
            path.write_bytes(b"after!!\n")
        return chunk
    monkeypatch.setattr(intake.os, "read", mutate_after_read)
    with pytest.raises(intake.SourceIntakeError):
        collect(source_root)


def test_rejects_replacement_with_symlink_before_open(source_root, tmp_path, monkeypatch):
    path = source_root / "a.py"
    path.write_bytes(b"original")
    other = tmp_path / "other.py"
    other.write_bytes(b"not approved for this snapshot")
    real_open = os.open
    def swap_before_open(name, flags, *args, **kwargs):
        if name == "a.py":
            path.unlink()
            path.symlink_to(other)
        return real_open(name, flags, *args, **kwargs)
    monkeypatch.setattr(intake.os, "open", swap_before_open)
    with pytest.raises(intake.SourceIntakeError):
        collect(source_root)


def test_snapshot_storage_is_immutable_and_detached_from_disk(source_root):
    path = source_root / "a.py"
    path.write_bytes(b"original")
    bundle = collect(source_root)
    path.write_bytes(b"changed")
    assert bundle.sources[("head", "a.py")] == "original"
    with pytest.raises(TypeError):
        bundle.sources[("head", "a.py")] = "forged"
    with pytest.raises(ValidationError):
        bundle.manifest.revision = "b" * 64


def test_error_messages_do_not_expose_source_roots_or_raw_exceptions(source_root):
    with pytest.raises(intake.SourceIntakeError) as caught:
        collect(source_root, "missing.py")
    assert str(source_root) not in str(caught.value)
    assert "missing.py" not in str(caught.value)


@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_final_validation_rejects_late_intermediate_directory_replacement(source_root, tmp_path, monkeypatch, selected, replacement):
    nested = source_root / "a" / "deep"
    nested.mkdir(parents=True)
    (nested / "file.py").write_bytes(b"original\n")
    (source_root / "z.py").write_bytes(b"trigger\n")
    outside = tmp_path / "outside_snapshot"
    outside.mkdir()
    (outside / "file.py").write_bytes(b"outside\n")
    real_read = os.read
    changed = False

    def replace_completed_directory(fd, size):
        nonlocal changed
        chunk = real_read(fd, size)
        if chunk == b"trigger\n" and not changed:
            changed = True
            nested.rename(nested.with_name("old"))
            if replacement == "symlink":
                nested.symlink_to(outside, target_is_directory=True)
            else:
                nested.mkdir()
                (nested / "file.py").write_bytes(b"replacement\n")
        return chunk

    monkeypatch.setattr(intake.os, "read", replace_completed_directory)
    with pytest.raises(intake.SourceIntakeError):
        collect(source_root, selected_files=["a/deep/file.py", "z.py"] if selected else None)
    assert changed


def test_public_snapshot_container_copies_caller_owned_mapping(source_root):
    (source_root / "a.py").write_bytes(b"original")
    collected = collect(source_root)
    supplied = dict(collected.sources)
    bundle = intake.CollectedSnapshot(collected.manifest, supplied)
    supplied[("head", "a.py")] = "changed after construction"
    assert bundle.sources[("head", "a.py")] == "original"
    with pytest.raises(TypeError):
        bundle.sources[("head", "a.py")] = "forged"
