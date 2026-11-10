"""CLI integration uses generated project-local data, never original projects."""

import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import pytest

from northstar_codereview import real_source as intake
from northstar_codereview.real_source_contracts import (
    SAMPLES, SampleId, IntakeManifest, ImportApproval, snapshot_from_hashes,
)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    assert tmp_path.is_relative_to(intake.PROJECT_ROOT)
    root = tmp_path / "fictional"
    root.mkdir()
    sample = SampleId.PORTFOLIO_RISK
    text = "value = 1\r\n"
    for path in SAMPLES[sample][1]:
        p = root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode())
    monkeypatch.setattr(intake, "_SOURCE_ROOTS", {SAMPLES[sample][0]: root})
    monkeypatch.setattr(intake, "_STORE", tmp_path / "private-store")
    now = datetime.now(timezone.utc)
    manifest = IntakeManifest(sample=sample, project=SAMPLES[sample][0], retain_until=now + timedelta(days=1),
        snapshot=snapshot_from_hashes(sample, {p: sha256(text.encode()).hexdigest() for p in SAMPLES[sample][1]}))
    approval = ImportApproval(approval_id=uuid4(), manifest=manifest, state="approved", privacy_reviewed=True,
                              approved_at=now, expires_at=now + timedelta(hours=1))
    folder = intake._STORE / "approvals"
    folder.mkdir(parents=True, mode=0o700)
    intake._STORE.chmod(0o700)
    path = folder / f"{approval.approval_id}.json"
    path.write_text(approval.model_dump_json())
    path.chmod(0o600)
    return sample, approval, path, root


def invoke(capsys, *arguments):
    from northstar_codereview.real_source_cli import main
    code = main(list(arguments))
    captured = capsys.readouterr()
    assert not captured.err
    return code, json.loads(captured.out)


def test_preflight_checks_metadata_without_opening_source(prepared, monkeypatch, capsys):
    sample, approval, path, root = prepared
    monkeypatch.setattr(intake, "_capture", lambda *a: pytest.fail("preflight opened source"))
    code, report = invoke(capsys, "preflight", "--sample", sample.value, "--approval-id", str(approval.approval_id))
    assert code == 0 and report["status"] == "ready"
    assert report["files"] == list(SAMPLES[sample][1]) and report["expected_source_count"] == 2
    assert report["authorization_metadata_valid"] is True
    assert report["source_verified"] is False
    assert not (intake._STORE / "imports").exists()


def test_import_requires_opt_in_before_source_access(prepared, monkeypatch, capsys):
    sample, approval, _, _ = prepared
    monkeypatch.setattr(intake, "_capture", lambda *a: pytest.fail("unauthorized source read"))
    code, report = invoke(capsys, "import", "--sample", sample.value, "--approval-id", str(approval.approval_id))
    assert code == 2 and report["category"] == "authorization"


def test_authorized_import_preserves_source_without_other_grants(prepared, capsys):
    sample, approval, _, _ = prepared
    code, report = invoke(capsys, "import", "--sample", sample.value, "--approval-id", str(approval.approval_id), "--allow-import")
    assert code == 0 and report["status"] == "committed"
    assert report["source_verified"] is True
    snapshot = intake.load_import(approval.approval_id)
    assert set(snapshot.sources.values()) == {"value = 1\r\n"}
    assert report["snapshot_id"] == str(snapshot.manifest.snapshot_id)
    for key in ("transmission_authorized", "human_review_authorized", "publication_authorized"):
        assert report[key] is False
    assert not list(intake._STORE.rglob("transmission-*.json"))


@pytest.mark.parametrize("sample", list(SampleId))
def test_preflight_without_approval_reports_scope_without_storage(sample, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(intake, "_STORE", tmp_path / "absent")
    monkeypatch.setattr(intake, "_capture", lambda *a: pytest.fail("source read"))
    code, report = invoke(capsys, "preflight", "--sample", sample.value)
    assert code == 2 and report["category"] == "authorization"
    assert report["expected_source_count"] == len(SAMPLES[sample][1])
    assert report["files"] == list(SAMPLES[sample][1])
    assert not intake._STORE.exists()


@pytest.mark.parametrize("arguments", [
    [], ["unknown"], ["preflight", "--sample", "unknown"],
    ["preflight", "--sample", "../../sensitive-marker"],
    ["preflight", "--sample", "portfolio_risk", "--approval-id", "../sensitive-marker"],
    ["preflight", "--sample", "portfolio_risk", "--source-root", "/sensitive-marker"],
    ["import", "--sample", "portfolio_risk", "--file", "sensitive-marker.py"],
    ["preflight", "--sample", "portfolio_risk", "--sam", "portfolio_risk"],
    ["preflight", "--sample", "portfolio_risk", "--sample", "csv_import"],
    ["preflight", "--sample", "portfolio_risk", "--approval-id", str(uuid4()), "--approval-id", str(uuid4())],
    ["preflight", "--sample", "portfolio_risk", "--allow-import"],
    ["preflight", "--sample", "portfolio_risk", "@sensitive-marker"],
])
def test_invalid_cli_arguments_are_redacted(arguments, prepared, monkeypatch, capsys):
    from northstar_codereview import real_source_cli as cli
    monkeypatch.setattr(cli, "read_import_approval", lambda *a: pytest.fail("invalid arguments read approval"))
    code, report = invoke(capsys, *arguments)
    assert code == 2 and report["category"] == "invalid_arguments"
    assert "sensitive-marker" not in json.dumps(report)


def test_missing_opt_in_does_not_read_approval(prepared, monkeypatch, capsys):
    from northstar_codereview import real_source_cli as cli
    sample, approval, _, _ = prepared
    monkeypatch.setattr(cli, "read_import_approval", lambda *a: pytest.fail("missing opt-in opened approval"))
    code, report = invoke(capsys, "import", "--sample", sample.value, "--approval-id", str(approval.approval_id))
    assert code == 2 and report["category"] == "authorization"


@pytest.mark.parametrize("condition", ["absent", "expired", "future", "denied", "pending", "revoked", "unknown_state",
    "wrong_uuid", "wrong_sample", "extra_path", "duplicate_key", "malformed", "secret_extra_field"])
def test_bad_approval_metadata_never_opens_source(prepared, monkeypatch, capsys, condition):
    sample, approval, path, _ = prepared
    data = json.loads(path.read_text())
    now = datetime.now(timezone.utc)
    if condition == "absent":
        path.unlink()
    elif condition == "expired":
        data.update(approved_at=(now - timedelta(hours=2)).isoformat(), expires_at=(now - timedelta(hours=1)).isoformat())
    elif condition == "future":
        data["approved_at"] = (now + timedelta(minutes=10)).isoformat()
    elif condition in {"denied", "pending", "revoked", "unknown_state"}:
        data["state"] = condition
    elif condition == "wrong_uuid":
        data["approval_id"] = str(uuid4())
    elif condition == "wrong_sample":
        sample = SampleId.CSV_IMPORT
    elif condition == "extra_path":
        data["manifest"]["snapshot"]["files"].append({"file_path": "../sensitive-marker", "side": "head", "sha256": "a" * 64})
    elif condition == "secret_extra_field":
        data["unexpected"] = "sensitive-marker"
    if condition != "absent":
        path.write_text(json.dumps(data))
    if condition == "duplicate_key":
        path.write_text('{"state":"denied",' + json.dumps(data)[1:])
    elif condition == "malformed":
        path.write_text("sensitive-marker-not-json")
    monkeypatch.setattr(intake, "_capture", lambda *a: pytest.fail("invalid approval read source"))
    code, report = invoke(capsys, "import", "--sample", sample.value, "--approval-id", str(approval.approval_id), "--allow-import")
    assert code == 2
    assert report["authorization_metadata_valid"] is False
    assert "sensitive-marker" not in json.dumps(report)
    assert not (intake._STORE / "imports").exists()


@pytest.mark.parametrize("kind", ["file_symlink", "folder_symlink", "fifo", "hardlink", "permissions", "directory", "oversized"])
def test_unsafe_approval_storage_is_rejected(prepared, monkeypatch, capsys, kind):
    import os
    sample, approval, path, root = prepared
    if kind == "permissions":
        path.chmod(0o644)
    elif kind == "folder_symlink":
        folder = path.parent
        target = folder.with_name("moved")
        folder.rename(target)
        folder.symlink_to(target, target_is_directory=True)
    elif kind == "oversized":
        path.write_bytes(b" " * 32769)
    else:
        path.unlink()
        if kind == "file_symlink":
            path.symlink_to(root / "tests/test_risk.py")
        elif kind == "hardlink":
            os.link(root / "tests/test_risk.py", path)
        elif kind == "fifo":
            os.mkfifo(path)
        else:
            path.mkdir()
    monkeypatch.setattr(intake, "_capture", lambda *a: pytest.fail("unsafe approval read source"))
    code, report = invoke(capsys, "preflight", "--sample", sample.value, "--approval-id", str(approval.approval_id))
    assert code == 2 and not report["authorization_metadata_valid"]


@pytest.mark.parametrize("kind", ["missing", "changed", "symlink", "fifo", "binary", "oversized"])
def test_import_enforces_existing_source_boundary(prepared, capsys, kind):
    import os
    sample, approval, _, root = prepared
    source = root / "tests/test_risk.py"
    if kind == "missing":
        source.unlink()
    elif kind == "changed":
        source.write_text("changed = 2\n")
    elif kind == "binary":
        source.write_bytes(b"\x00binary")
    elif kind == "oversized":
        source.write_bytes(b"#" * (1024**2 + 1))
    else:
        source.unlink()
        if kind == "symlink":
            source.symlink_to(root / SAMPLES[sample][1][0])
        else:
            os.mkfifo(source)
    code, report = invoke(capsys, "import", "--sample", sample.value, "--approval-id", str(approval.approval_id), "--allow-import")
    assert code == 2 and report["source_verified"] is False
    assert report["category"] in {"storage_failure", "source_integrity", "unsafe_file", "binary_content", "size_limit"}
    assert not (intake._STORE / "imports").exists()


def replace_source_and_approval(prepared, text):
    sample, approval, path, root = prepared
    for relative in SAMPLES[sample][1]:
        (root / relative).write_bytes(text.encode())
    manifest = IntakeManifest.model_validate({**approval.manifest.model_dump(),
        "snapshot": snapshot_from_hashes(sample, {p: sha256(text.encode()).hexdigest() for p in SAMPLES[sample][1]})})
    changed = ImportApproval.model_validate({**approval.model_dump(), "manifest": manifest})
    path.write_text(changed.model_dump_json())
    return changed


def test_privacy_rejection_has_no_source_leak_or_source_storage(prepared, capsys):
    sample, approval, _, _ = prepared
    replace_source_and_approval(prepared, "api_key = 'fictional-sensitive-marker'\n")
    code, report = invoke(capsys, "import", "--sample", sample.value, "--approval-id", str(approval.approval_id), "--allow-import")
    assert code == 2 and report["category"] == "credential_indicator"
    assert "fictional-sensitive-marker" not in json.dumps(report)
    assert not (intake._STORE / "imports").exists()


def test_atomic_failure_cleanup_through_cli(prepared, monkeypatch, capsys):
    sample, approval, _, _ = prepared
    original = intake._write
    def fail_after_write(parent, name, payload):
        original(parent, name, payload)
        if name == "source.json":
            raise OSError("sensitive-marker-write-error")
    monkeypatch.setattr(intake, "_write", fail_after_write)
    code, report = invoke(capsys, "import", "--sample", sample.value, "--approval-id", str(approval.approval_id), "--allow-import")
    assert code == 2 and report["category"] == "storage_failure"
    assert list((intake._STORE / "imports").iterdir()) == []
    assert list((intake._STORE / "claims").iterdir()) == []
    assert "sensitive-marker" not in json.dumps(report)


@pytest.mark.parametrize("operation", ["preflight", "import"])
def test_duplicate_approval_rejected_without_reopening_source(prepared, monkeypatch, capsys, operation):
    sample, approval, _, _ = prepared
    args = ["--sample", sample.value, "--approval-id", str(approval.approval_id)]
    assert invoke(capsys, "import", *args, "--allow-import")[0] == 0
    monkeypatch.setattr(intake, "_capture", lambda *a: pytest.fail("duplicate approval read source"))
    code, report = invoke(capsys, operation, *args, *(["--allow-import"] if operation == "import" else []))
    assert code == 2 and report["category"] == "conflicting_import"


def test_approval_mutation_during_read_is_rejected(prepared, monkeypatch, capsys):
    sample, approval, path, _ = prepared
    original = intake._read
    def mutate(*args):
        raw = original(*args)
        path.write_text('{}')
        return raw
    monkeypatch.setattr(intake, "_read", mutate)
    code, report = invoke(capsys, "preflight", "--sample", sample.value, "--approval-id", str(approval.approval_id))
    assert code == 2 and report["category"] == "authorization"


def test_source_is_not_executed_and_transmission_is_not_called(prepared, monkeypatch, capsys):
    sample, approval, _, _ = prepared
    replace_source_and_approval(prepared, "raise RuntimeError('reviewed source must not execute')\n")
    monkeypatch.setattr(intake, "record_transmission", lambda *a: pytest.fail("transmission granted"))
    code, report = invoke(capsys, "import", "--sample", sample.value, "--approval-id", str(approval.approval_id), "--allow-import")
    assert code == 0 and report["transmission_authorized"] is False


def test_unexpected_error_is_sanitized_without_retry(prepared, monkeypatch, capsys):
    from northstar_codereview import real_source_cli as cli
    sample, approval, _, _ = prepared
    attempts = []
    def fail(*a, **kw):
        attempts.append(True)
        raise RuntimeError("sensitive-marker-source-payload")
    monkeypatch.setattr(cli, "import_sample", fail)
    code, report = invoke(capsys, "import", "--sample", sample.value, "--approval-id", str(approval.approval_id), "--allow-import")
    assert code == 2 and report["category"] == "internal_error"
    assert len(attempts) == 1 and "sensitive-marker" not in json.dumps(report)


def test_entrypoint_returns_cli_exit_code_and_help_without_source(prepared, monkeypatch, capsys):
    import runpy
    import sys
    monkeypatch.setattr(intake, "_capture", lambda *a: pytest.fail("entry point read source"))
    monkeypatch.setattr(sys, "argv", ["import_real_source.py", "preflight", "--sample", "portfolio_risk"])
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(intake.PROJECT_ROOT / "examples/import_real_source.py"), run_name="__main__")
    assert caught.value.code == 2
    assert json.loads(capsys.readouterr().out)["category"] == "authorization"
    monkeypatch.setattr(sys, "argv", ["import_real_source.py", "--help"])
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(intake.PROJECT_ROOT / "examples/import_real_source.py"), run_name="__main__")
    assert caught.value.code == 0
    assert "No inference" in " ".join(capsys.readouterr().out.split())
