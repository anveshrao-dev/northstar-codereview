"""Checkpoint integrity, actual process restarts, and local storage containment."""

import json
import os
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest
from langgraph.types import Interrupt

from northstar_codereview.local_source import PROJECT_ROOT
from .test_approval import database, report, snapshot, workflow, decision


def child(database, thread, script, data=""):
    return subprocess.run([str(PROJECT_ROOT / ".venv/bin/python"), "-B", "-c", script, str(database), str(thread)],
                          input=data, text=True, capture_output=True, timeout=15, cwd=PROJECT_ROOT,
                          env={"PYTHONDONTWRITEBYTECODE": "1", "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false",
                               "TMPDIR": str(PROJECT_ROOT / ".cache/tmp")})


PRELUDE = """
import sys, json, os, socket
def forbidden_network(*args, **kwargs):
    os._exit(99)  # Cannot be hidden by application exception handling.
socket.socket.connect = forbidden_network
socket.socket.connect_ex = forbidden_network
socket.create_connection = forbidden_network
socket.getaddrinfo = forbidden_network
from pathlib import Path
from uuid import UUID
from northstar_codereview.approval import ApprovalWorkflow
from northstar_codereview.reporting import ReviewReport
service = ApprovalWorkflow(Path(sys.argv[1]), allowed_reviewers={'reviewer.alice', 'reviewer.bob'})
thread = UUID(sys.argv[2])
"""


def test_fresh_process_start_pause_resume_and_finalized_read(database, report):
    thread = uuid4()
    first = child(database, thread, PRELUDE + "service.start(ReviewReport.model_validate_json(sys.stdin.read()), thread_id=thread)\n", report.model_dump_json())
    assert first.returncode == 0, first.stderr
    assert workflow(database).get(thread).status == "pending"
    second = child(database, thread, PRELUDE + "print(service.submit(thread, json.loads(sys.stdin.read()), submitted_by='reviewer.alice').outcome.record_id)\n", json.dumps(decision(report)))
    assert second.returncode == 0, second.stderr
    record_id = second.stdout.strip()
    third = child(database, thread, PRELUDE + "print(service.get(thread).outcome.record_id)\n")
    assert third.returncode == 0 and third.stdout.strip() == record_id


def test_process_crash_before_decision_commit_rolls_back_to_pending(database, report):
    thread = uuid4()
    workflow(database).start(report, thread_id=thread)
    script = PRELUDE + """
original = service._load
calls = [0]
def crash_before_commit(*args):
    calls[0] += 1
    value = original(*args)
    if calls[0] == 2:
        os._exit(23)
    return value
service._load = crash_before_commit
service.submit(thread, json.loads(sys.stdin.read()), submitted_by='reviewer.alice')
"""
    crashed = child(database, thread, script, json.dumps(decision(report)))
    assert crashed.returncode == 23
    assert workflow(database).get(thread).status == "pending"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    assert workflow(database).submit(thread, decision(report), submitted_by="reviewer.alice").status == "finalized"


def test_response_loss_after_commit_does_not_reapply_decision(database, report):
    from northstar_codereview.approval import ApprovalError
    thread = uuid4()
    workflow(database).start(report, thread_id=thread)
    completed = child(database, thread, PRELUDE + "service.submit(thread, json.loads(sys.stdin.read()), submitted_by='reviewer.alice')\nos._exit(24)\n", json.dumps(decision(report)))
    assert completed.returncode == 24
    saved = workflow(database).get(thread)
    assert saved.status == "finalized"
    with pytest.raises(ApprovalError, match="already_finalized"):
        workflow(database).submit(thread, decision(report), submitted_by="reviewer.alice")
    assert workflow(database).get(thread).outcome.record_id == saved.outcome.record_id


@pytest.mark.parametrize("damage", ["anchor", "checkpoint", "writes", "missing_latest", "phase", "missing_decision", "decision"])
def test_corrupt_state_fails_closed_without_new_thread(database, report, damage):
    from northstar_codereview.approval import ApprovalError
    service, thread = workflow(database), uuid4()
    service.start(report, thread_id=thread)
    if damage in {"missing_decision", "decision"}:
        service.submit(thread, decision(report), submitted_by="reviewer.alice")
    with sqlite3.connect(database) as connection:
        if damage == "anchor":
            connection.execute("UPDATE threads SET anchor='corrupt'")
        elif damage == "checkpoint":
            connection.execute("UPDATE checkpoints SET payload='corrupt' WHERE checkpoint_id=(SELECT latest FROM threads)")
        elif damage == "writes":
            connection.execute("UPDATE writes SET payload='corrupt' WHERE checkpoint_id=(SELECT latest FROM threads)")
        elif damage == "missing_latest":
            connection.execute("DELETE FROM checkpoints WHERE checkpoint_id=(SELECT latest FROM threads)")
        elif damage == "phase":
            connection.execute("UPDATE threads SET phase='finalized'")
        elif damage == "missing_decision":
            connection.execute("DELETE FROM decisions")
        else:
            connection.execute("UPDATE decisions SET payload='corrupt'")
    with pytest.raises(ApprovalError):
        workflow(database).get(thread)
    with pytest.raises(ApprovalError):
        workflow(database).submit(thread, decision(report), submitted_by="reviewer.alice")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 1


def test_corrupt_database_header_is_not_reinitialized(database):
    from northstar_codereview.approval import ApprovalError
    database.write_bytes(b"not a SQLite database")
    database.chmod(0o600)
    before = database.read_bytes()
    with pytest.raises(ApprovalError):
        workflow(database).get(uuid4())
    assert database.read_bytes() == before


@pytest.mark.parametrize("kind", ["file_symlink", "parent_symlink", "hardlink", "sidecar_symlink", "public_file"])
def test_storage_link_and_permission_guards(database, report, kind):
    from northstar_codereview.approval import ApprovalError
    target = database.parent / "unrelated-local-sentinel"
    target.write_text("Do not modify this local test sentinel.")
    if kind == "file_symlink":
        database.symlink_to(target)
    elif kind == "parent_symlink":
        linked = database.parent / "linked"
        linked.symlink_to(database.parent, target_is_directory=True)
        database = linked / database.name
    elif kind == "hardlink":
        os.link(target, database)
    elif kind == "sidecar_symlink":
        database.with_name(database.name + "-journal").symlink_to(target)
    else:
        database.write_bytes(b"")
        database.chmod(0o644)
    with pytest.raises(ApprovalError):
        workflow(database).start(report, thread_id=uuid4())
    assert target.read_text() == "Do not modify this local test sentinel."


@pytest.mark.parametrize("path", ["/tmp/approval.sqlite3", ".cache/approval.sqlite3", ".runtime/../approval.sqlite3", ".runtime/approval.txt"])
def test_database_path_must_be_ignored_project_runtime(path):
    from northstar_codereview.approval import ApprovalError
    with pytest.raises(ApprovalError, match="unsafe_storage_path"):
        workflow(path)


@pytest.mark.parametrize("value", [object(), ValueError("private"), b"pickle payload", {1: "non-string key"}, float("inf")])
def test_closed_serializer_rejects_arbitrary_objects(value):
    from northstar_codereview.approval_store import ApprovalJson
    from northstar_codereview.approval import ApprovalError
    with pytest.raises(ApprovalError):
        ApprovalJson().dumps_typed(value)


@pytest.mark.parametrize("raw", [b'["import","os","system"]', b'["scalar",{}]', b'["dict",[["x",["scalar",1]],["x",["scalar",2]]]]', b'not json'])
def test_closed_serializer_rejects_unknown_tags_and_corrupt_json(raw):
    from northstar_codereview.approval_store import ApprovalJson
    from northstar_codereview.approval import ApprovalError
    with pytest.raises(ApprovalError):
        ApprovalJson().loads_typed(("approval-json-v1", raw))


def test_closed_serializer_roundtrip_interrupt_and_structures():
    from northstar_codereview.approval_store import ApprovalJson
    serde = ApprovalJson()
    value = {"json": [None, True, 1, .25, "safe"], "pending": (Interrupt({"review": "fictional"}, id="safe-id"),)}
    restored = serde.loads_typed(serde.dumps_typed(value))
    assert restored["json"] == value["json"] and isinstance(restored["pending"], tuple)
    assert type(restored["pending"][0]) is Interrupt and restored["pending"][0].value == {"review": "fictional"}


def test_secret_or_non_json_blob_cannot_be_restored():
    from northstar_codereview.approval_store import ApprovalJson
    from northstar_codereview.approval import ApprovalError
    for pair in (("pickle", b"ignored"), ("approval-json-v1", b'["scalar","password=FictionalOnlyValue"]')):
        with pytest.raises(ApprovalError) as error:
            ApprovalJson().loads_typed(pair)
        assert "FictionalOnlyValue" not in str(error.value)


def test_reviewer_policy_is_both_captured_and_current(database, report):
    from northstar_codereview.approval import ApprovalError, ApprovalWorkflow
    thread = uuid4()
    ApprovalWorkflow(database, allowed_reviewers={"reviewer.alice"}).start(report, thread_id=thread)
    with pytest.raises(ApprovalError, match="unauthorized_reviewer"):
        workflow(database).submit(thread, {**decision(report), "reviewer_id": "reviewer.bob"}, submitted_by="reviewer.bob")
    with pytest.raises(ApprovalError, match="unauthorized_reviewer"):
        ApprovalWorkflow(database, allowed_reviewers={"reviewer.bob"}).submit(thread, decision(report), submitted_by="reviewer.alice")
    assert workflow(database).get(thread).status == "pending"


def test_duplicate_start_and_same_revision_on_new_thread_rejected(database, report):
    from northstar_codereview.approval import ApprovalError
    service, thread = workflow(database), uuid4()
    first = service.start(report, thread_id=thread)
    with pytest.raises(ApprovalError, match="duplicate_thread"):
        service.start(report, thread_id=thread)
    with pytest.raises(ApprovalError, match="stale_report"):
        service.start(report, thread_id=uuid4())
    assert service.get(thread) == first


@pytest.mark.parametrize("damage", ["missing", "revision", "hash", "thread"])
def test_revision_index_corruption_cannot_reactivate_stale_review(database, snapshot, damage):
    from northstar_codereview.approval import ApprovalError
    from northstar_codereview.reporting import build_report
    from .test_reporting import outcome
    result = outcome(snapshot)
    old = build_report(result, snapshot=snapshot.manifest, report_revision=1)
    new = build_report(result, snapshot=snapshot.manifest, report_revision=2)
    first, second, service = uuid4(), uuid4(), workflow(database)
    service.start(old, thread_id=first)
    service.start(new, thread_id=second)
    with sqlite3.connect(database) as connection:
        statement = {"missing": "DELETE FROM reviews", "revision": "UPDATE reviews SET revision=0",
                     "hash": "UPDATE reviews SET report_hash='bad'", "thread": "UPDATE reviews SET thread_id='bad'"}[damage]
        connection.execute(statement)
    with pytest.raises(ApprovalError, match="invalid_checkpoint"):
        service.start(old, thread_id=uuid4())
    with pytest.raises(ApprovalError, match="invalid_checkpoint"):
        service.get(second)
    with pytest.raises(ApprovalError, match="invalid_checkpoint"):
        service.submit(first, decision(old), submitted_by="reviewer.alice")


def test_failed_first_start_does_not_poison_database(database, report, monkeypatch):
    import northstar_codereview.approval as module
    original = module._graph
    def fail(saver):
        raise RuntimeError("Synthetic startup fault")
    monkeypatch.setattr(module, "_graph", fail)
    thread = uuid4()
    with pytest.raises(module.ApprovalError):
        workflow(database).start(report, thread_id=thread)
    monkeypatch.setattr(module, "_graph", original)
    with pytest.raises(module.ApprovalError, match="missing_checkpoint"):
        workflow(database).get(thread)
    assert workflow(database).start(report, thread_id=thread).status == "pending"


def test_crash_during_first_start_leaves_initialized_database_for_explicit_retry(database, report):
    from northstar_codereview.approval import ApprovalError
    thread = uuid4()
    script = PRELUDE + """
def crash(*args):
    os._exit(25)
service._load = crash
service.start(ReviewReport.model_validate_json(sys.stdin.read()), thread_id=thread)
"""
    result = child(database, thread, script, report.model_dump_json())
    assert result.returncode == 25
    with pytest.raises(ApprovalError, match="missing_checkpoint"):
        workflow(database).get(thread)
    assert workflow(database).start(report, thread_id=thread).status == "pending"


@pytest.mark.parametrize("reviewers", ["alice", (), {"../private"}, {"password=FictionalValue"}])
def test_invalid_reviewer_policy_rejected_before_storage(database, reviewers):
    from northstar_codereview.approval import ApprovalError, ApprovalWorkflow
    with pytest.raises(ApprovalError, match="invalid_application_policy"):
        ApprovalWorkflow(database, allowed_reviewers=reviewers)
    assert not database.exists()


def test_storage_link_check_does_not_follow_target_metadata(database, report, monkeypatch):
    from pathlib import Path
    from northstar_codereview.approval import ApprovalError
    target = database.parent / "local-sentinel"
    target.write_text("untouched")
    database.symlink_to(target)
    original = Path.exists
    def forbid_follow(path):
        if path == database:
            raise AssertionError("Target-following existence check is forbidden")
        return original(path)
    monkeypatch.setattr(Path, "exists", forbid_follow)
    with pytest.raises(ApprovalError, match="unsafe_storage_path"):
        workflow(database).start(report, thread_id=uuid4())
    assert target.read_text() == "untouched"
