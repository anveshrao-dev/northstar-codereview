"""Real local SQLite and LangGraph interrupts; synthetic reports, no inference."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from uuid import uuid4

import pytest

from northstar_codereview.local_source import PROJECT_ROOT
from northstar_codereview.reporting import build_report
from .test_reporting import snapshot, finding, outcome


@pytest.fixture
def database():
    root = PROJECT_ROOT / ".runtime" / "approval-tests"
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=root) as directory:
        yield Path(directory) / "review.sqlite3"


@pytest.fixture
def report(snapshot):
    return build_report(outcome(snapshot, [finding(snapshot)]), snapshot=snapshot.manifest)


def workflow(database, **kwargs):
    from northstar_codereview.approval import ApprovalWorkflow
    return ApprovalWorkflow(database, allowed_reviewers={"reviewer.alice", "reviewer.bob"}, **kwargs)


def decision(report, **changes):
    return dict(review_id=str(report.review_id), report_revision=report.report_revision,
                report_hash=report.report_id, reviewer_id="reviewer.alice", decision="approve", **changes)


def test_real_graph_interrupt_and_resume(database, report, monkeypatch):
    from northstar_codereview.orchestrator import ReviewOrchestrator
    from northstar_codereview.agents.code_quality import CodeQualityAgent
    from northstar_codereview.providers.nebius import NebiusModel
    def forbidden(*args, **kwargs):
        pytest.fail("Approval must not construct a provider or rerun specialists")
    monkeypatch.setattr(ReviewOrchestrator, "run", forbidden)
    monkeypatch.setattr(CodeQualityAgent, "run", forbidden)
    monkeypatch.setattr(NebiusModel, "__init__", forbidden)
    service = workflow(database)
    thread = uuid4()
    paused = service.start(report, thread_id=thread)
    assert paused.status == "pending" and paused.outcome is None
    assert paused.prompt.report_hash == report.report_id
    assert paused.prompt.thread_id == thread and paused.prompt.scope == "local_report_review"
    assert paused.report == report
    # Fresh service/connection must restore an actual interrupted graph.
    assert workflow(database).get(thread) == paused
    finished = workflow(database).submit(thread, decision(report), submitted_by="reviewer.alice")
    assert finished.status == "finalized" and finished.prompt is None
    assert finished.outcome.decision == "approve" and finished.outcome.scope == "local_report_review"
    assert finished.outcome.report_hash == report.report_id
    assert workflow(database).get(thread) == finished


@pytest.mark.parametrize("choice", ["approve", "reject", "request_changes"])
def test_explicit_decisions_allow_optional_comments(database, report, choice):
    service, thread = workflow(database), uuid4()
    service.start(report, thread_id=thread)
    payload = {**decision(report), "decision": choice}
    finished = service.submit(thread, payload, submitted_by="reviewer.alice")
    assert finished.outcome.decision == choice and finished.outcome.comment is None


@pytest.mark.parametrize("changes,actor", [
    ({"review_id": str(uuid4())}, "reviewer.alice"),
    ({"report_revision": 2}, "reviewer.alice"),
    ({"report_revision": True}, "reviewer.alice"),
    ({"report_hash": "0" * 64}, "reviewer.alice"),
    ({"reviewer_id": "unknown"}, "unknown"),
    ({"reviewer_id": "../private"}, "../private"),
    ({"reviewer_id": ""}, ""),
    ({"reviewer_id": "reviewer.bob"}, "reviewer.alice"),
    ({"decision": "publish"}, "reviewer.alice"),
    ({"publish_to_github": True}, "reviewer.alice"),
    ({"scope": "github_post"}, "reviewer.alice"),
    ({"decision": "clarify"}, "reviewer.alice"),
    ({"comment": "NEBIUS_API_KEY=FictionalOnlyValue"}, "reviewer.alice"),
])
def test_invalid_submissions_leave_pending_state_unchanged(database, report, changes, actor):
    from northstar_codereview.approval import ApprovalError
    service, thread = workflow(database), uuid4()
    paused = service.start(report, thread_id=thread)
    with pytest.raises(ApprovalError) as error:
        service.submit(thread, {**decision(report), **changes}, submitted_by=actor)
    assert "FictionalOnlyValue" not in str(error.value) and "../private" not in str(error.value)
    assert service.get(thread) == paused
    assert b"FictionalOnlyValue" not in database.read_bytes()


@pytest.mark.parametrize("conflicting", [False, True])
def test_finalized_thread_rejects_duplicate_or_conflicting_resume(database, report, conflicting):
    from northstar_codereview.approval import ApprovalError
    service, thread = workflow(database), uuid4()
    service.start(report, thread_id=thread)
    first = service.submit(thread, decision(report), submitted_by="reviewer.alice")
    payload = {**decision(report), "decision": "reject" if conflicting else "approve"}
    with pytest.raises(ApprovalError, match="already_finalized"):
        workflow(database).submit(thread, payload, submitted_by="reviewer.alice")
    assert service.get(thread) == first


def test_no_creation_on_missing_checkpoint(database, report):
    from northstar_codereview.approval import ApprovalError
    service, thread = workflow(database), uuid4()
    for action in (lambda: service.get(thread), lambda: service.submit(thread, decision(report), submitted_by="reviewer.alice")):
        with pytest.raises(ApprovalError, match="missing_checkpoint"):
            action()
        assert not database.exists()
    service.start(report, thread_id=thread)
    with pytest.raises(ApprovalError, match="missing_checkpoint"):
        service.submit(uuid4(), decision(report), submitted_by="reviewer.alice")


def test_partial_report_requires_explicit_acknowledgment(database, snapshot):
    from northstar_codereview.approval import ApprovalError
    report = build_report(outcome(snapshot, status="partial", limitations=("Coverage incomplete.",)), snapshot=snapshot.manifest)
    service, thread = workflow(database), uuid4()
    paused = service.start(report, thread_id=thread)
    assert paused.prompt.requires_partial_acknowledgment
    with pytest.raises(ApprovalError, match="partial_acknowledgment_required"):
        service.submit(thread, decision(report), submitted_by="reviewer.alice")
    finished = service.submit(thread, decision(report, acknowledge_partial=True), submitted_by="reviewer.alice")
    assert finished.outcome.acknowledge_partial and finished.report.status == "partial"


def test_new_revision_supersedes_pending_old_report(database, snapshot):
    from northstar_codereview.approval import ApprovalError
    result = outcome(snapshot)
    first = build_report(result, snapshot=snapshot.manifest, report_revision=1)
    second = build_report(result, snapshot=snapshot.manifest, report_revision=2)
    service, old, new = workflow(database), uuid4(), uuid4()
    service.start(first, thread_id=old)
    service.start(second, thread_id=new)
    assert service.get(old).status == "superseded"
    with pytest.raises(ApprovalError, match="stale_report"):
        service.submit(old, decision(first), submitted_by="reviewer.alice")
    assert service.submit(new, decision(second), submitted_by="reviewer.alice").status == "finalized"


def test_concurrent_conflicting_decisions_commit_exactly_one(database, report):
    from northstar_codereview.approval import ApprovalError
    thread, ready = uuid4(), Barrier(2)
    workflow(database).start(report, thread_id=thread)
    def submit(choice):
        ready.wait(timeout=2)
        try:
            return workflow(database, busy_timeout_ms=1000).submit(thread, {**decision(report), "decision": choice}, submitted_by="reviewer.alice")
        except ApprovalError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ("approve", "reject")))
    successes = [item for item in results if not isinstance(item, str)]
    assert len(successes) == 1 and any(item in {"already_finalized", "storage_busy"} for item in results if isinstance(item, str))
    assert workflow(database).get(thread) == successes[0]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1


def test_sqlite_busy_fails_closed_without_retry(database, report):
    from northstar_codereview.approval import ApprovalError
    thread = uuid4()
    workflow(database).start(report, thread_id=thread)
    with sqlite3.connect(database) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(ApprovalError, match="storage_busy"):
            workflow(database, busy_timeout_ms=10).submit(thread, decision(report), submitted_by="reviewer.alice")
        blocker.rollback()
    assert workflow(database).get(thread).status == "pending"


def test_untrusted_or_sensitive_report_rejected_before_storage(database, report):
    from northstar_codereview.approval import ApprovalError
    for changes in ({"report_id": "0" * 64}, {"limitations": ("password=FictionalSecret",)}):
        with pytest.raises(ApprovalError):
            workflow(database).start(report.model_copy(update=changes), thread_id=uuid4())
        assert not database.exists()


def test_checkpoint_contains_only_safe_state(database, report, monkeypatch):
    import pickle
    def forbidden(*args, **kwargs):
        pytest.fail("Pickle deserialization is forbidden")
    monkeypatch.setattr(pickle, "loads", forbidden)
    thread = uuid4()
    workflow(database).start(report, thread_id=thread)
    workflow(database).submit(thread, decision(report, comment="Reviewed the reported limitations."), submitted_by="reviewer.alice")
    assert workflow(database).get(thread).outcome.comment == "Reviewed the reported limitations."
    raw = database.read_bytes()
    for forbidden_bytes in (b"RAW_SOURCE_SECRET_SENTINEL", b"SystemMessage", b"AIMessage", b"pickle", b"snippet"):
        assert forbidden_bytes not in raw
    assert database.stat().st_mode & 0o077 == 0
