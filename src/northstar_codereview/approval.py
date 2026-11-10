"""Durable post-report human review. No model, source tools, or publication API.

Only the service methods own transactions and may mutate durable state. Caller
reviewer IDs and allowlists must come from a trusted application. They do not
authenticate a human and do not constitute production authorization.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from langsmith import tracing_context
from pydantic import TypeAdapter

from .approval_models import (ApprovalAnchor, ApprovalDecision, ApprovalError, ApprovalPrompt,
                              ApprovalView, DecisionRecord, ReviewerId)
from .approval_store import ApprovalSaver, checked_path, pack, transaction, unpack
from .local_source import PROJECT_ROOT
from .output_safety import contains_sensitive
from .reporting import ReviewReport


class _State(TypedDict, total=False):
    anchor: dict
    status: str
    proposal: dict
    record: dict


@dataclass(frozen=True)
class _Authorization:
    reviewers: frozenset[str]
    actor: str | None = None
    recorded_at: datetime | None = None


def _restore_report(report) -> ReviewReport:
    try:
        if type(report) is not ReviewReport:
            raise ValueError
        restored = ReviewReport.model_validate_json(report.model_dump_json())
        if restored.status not in {"completed", "partial"}:
            raise ValueError
        return restored
    except Exception:
        raise ApprovalError("invalid_report") from None


def _anchor(value) -> ApprovalAnchor:
    try:
        return ApprovalAnchor.model_validate_json(json.dumps(value, allow_nan=False))
    except Exception:
        raise ApprovalError("invalid_checkpoint") from None


def _prompt(anchor: ApprovalAnchor) -> ApprovalPrompt:
    report = anchor.report
    return ApprovalPrompt(thread_id=anchor.thread_id, review_id=report.review_id,
                          report_revision=report.report_revision, report_hash=report.report_id,
                          report_status=report.status, requires_partial_acknowledgment=report.status == "partial")


def _decision(value) -> ApprovalDecision:
    try:
        data = value.model_dump(mode="json") if isinstance(value, ApprovalDecision) else value
        return ApprovalDecision.model_validate_json(json.dumps(data, allow_nan=False))
    except Exception:
        raise ApprovalError("invalid_decision") from None


def _authorize(anchor, proposed, auth):
    report = anchor.report
    if proposed.review_id != report.review_id:
        raise ApprovalError("review_mismatch")
    if proposed.report_revision != report.report_revision:
        raise ApprovalError("stale_report")
    if proposed.report_hash != report.report_id:
        raise ApprovalError("report_hash_mismatch")
    if (auth.actor != proposed.reviewer_id or proposed.reviewer_id not in auth.reviewers
            or proposed.reviewer_id not in anchor.reviewers):
        raise ApprovalError("unauthorized_reviewer")
    if report.status == "partial" and not proposed.acknowledge_partial:
        raise ApprovalError("partial_acknowledgment_required")


def _graph(saver):
    """No effectful nodes; durable writes are the saver and transaction boundary."""
    def validate_report(state):
        anchor = _anchor(state["anchor"])
        _restore_report(anchor.report)
        return {"status": "preparing"}

    def prepare_checkpoint(state):
        return {"status": "pending"}

    def await_decision(state):
        # LangGraph restarts this node on resume. There is no side effect before
        # the interrupt and no specialist/model graph to rerun.
        proposed = interrupt(_prompt(_anchor(state["anchor"])).model_dump(mode="json"))
        return {"proposal": proposed}

    def validate_decision(state, runtime: Runtime[_Authorization]):
        anchor, proposed = _anchor(state["anchor"]), _decision(state["proposal"])
        _authorize(anchor, proposed, runtime.context)
        return {"proposal": proposed.model_dump(mode="json")}

    def record_outcome(state, runtime: Runtime[_Authorization]):
        anchor, proposed = _anchor(state["anchor"]), _decision(state["proposal"])
        _authorize(anchor, proposed, runtime.context)
        record = DecisionRecord.create(**proposed.model_dump(mode="json"), thread_id=anchor.thread_id,
                                       recorded_at=runtime.context.recorded_at)
        return {"status": "finalized", "record": record.model_dump(mode="json")}

    graph = StateGraph(_State, context_schema=_Authorization)
    nodes = (validate_report, prepare_checkpoint, await_decision, validate_decision, record_outcome)
    previous = START
    for node in nodes:
        graph.add_node(node.__name__, node)
        graph.add_edge(previous, node.__name__)
        previous = node.__name__
    graph.add_edge(previous, END)
    return graph.compile(checkpointer=saver)


class ApprovalWorkflow:
    def __init__(self, database: Path = PROJECT_ROOT / ".runtime/approvals/reviews.sqlite3", *,
                 allowed_reviewers, busy_timeout_ms: int = 250):
        self.database = checked_path(database)
        try:
            if isinstance(allowed_reviewers, (str, bytes)):
                raise ValueError
            reviewers = TypeAdapter(tuple[ReviewerId, ...]).validate_python(tuple(sorted(set(allowed_reviewers))))
            if not 1 <= len(reviewers) <= 20 or contains_sensitive(reviewers):
                raise ValueError
            if type(busy_timeout_ms) is not int or not 1 <= busy_timeout_ms <= 2000:
                raise ValueError
        except Exception:
            raise ApprovalError("invalid_application_policy") from None
        self.reviewers, self.busy_timeout_ms = frozenset(reviewers), busy_timeout_ms

    @staticmethod
    def _thread(thread_id):
        if type(thread_id) is not UUID:
            raise ApprovalError("invalid_thread_id")
        return str(thread_id)

    @staticmethod
    def _config(thread_id):
        return {"configurable": {"thread_id": thread_id}, "callbacks": [], "recursion_limit": 12}

    @staticmethod
    def _current(connection, review_id):
        # The index is an optimization, never an authority. Reconstruct the
        # current revision from checked immutable anchors, including when an
        # index row is missing. This deliberately favors integrity over scale.
        history = []
        for thread, raw, digest in connection.execute("SELECT thread_id,anchor,digest FROM threads"):
            anchor = _anchor(unpack(raw, digest))
            if str(anchor.thread_id) != thread:
                raise ApprovalError("invalid_checkpoint")
            if anchor.report.review_id == review_id:
                history.append((anchor.report.report_revision, anchor.report.report_id, thread))
        if len({item[0] for item in history}) != len(history):
            raise ApprovalError("invalid_checkpoint")
        expected = max(history, default=None)
        current = connection.execute("SELECT revision,report_hash,thread_id FROM reviews WHERE review_id=?", (str(review_id),)).fetchone()
        if current != expected:
            raise ApprovalError("invalid_checkpoint")
        return current

    def _load(self, connection, thread_id):
        row = connection.execute("SELECT anchor,digest,phase FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
        if row is None:
            raise ApprovalError("missing_checkpoint")
        anchor = _anchor(unpack(row[0], row[1]))
        if str(anchor.thread_id) != thread_id or row[2] not in {"pending", "finalized"}:
            raise ApprovalError("invalid_checkpoint")
        current = self._current(connection, anchor.report.review_id)
        saver = ApprovalSaver(connection, thread_id)
        saved = saver.get_tuple(self._config(thread_id))
        graph = _graph(saver)
        state = graph.get_state(self._config(thread_id))
        if _anchor(state.values.get("anchor")) != anchor or state.values.get("status") != row[2]:
            raise ApprovalError("invalid_checkpoint")
        entry = connection.execute("SELECT payload,digest FROM decisions WHERE thread_id=?", (thread_id,)).fetchone()
        if row[2] == "pending":
            expected = _prompt(anchor)
            if (state.next != ("await_decision",) or len(state.interrupts) != 1
                    or state.interrupts[0].value != expected.model_dump(mode="json")
                    or entry is not None or "proposal" in state.values or "record" in state.values
                    or any(channel == "__resume__" for _, channel, _ in saved.pending_writes)):
                raise ApprovalError("invalid_checkpoint")
            if current is None:
                raise ApprovalError("invalid_checkpoint")
            superseded = current != (anchor.report.report_revision, anchor.report.report_id, thread_id)
            view = ApprovalView(thread_id=anchor.thread_id, report=anchor.report,
                                status="superseded" if superseded else "pending", prompt=expected)
        else:
            if state.next or state.interrupts or entry is None:
                raise ApprovalError("invalid_checkpoint")
            try:
                record = DecisionRecord.model_validate_json(json.dumps(unpack(*entry)))
                if state.values.get("record") != record.model_dump(mode="json"):
                    raise ValueError
                if record.thread_id != anchor.thread_id:
                    raise ValueError
                _authorize(anchor, record, _Authorization(frozenset(anchor.reviewers), record.reviewer_id))
                if _decision(state.values.get("proposal")) != ApprovalDecision.model_validate(record.model_dump(exclude={"schema_version", "thread_id", "recorded_at", "scope", "record_id"})):
                    raise ValueError
            except Exception:
                raise ApprovalError("invalid_checkpoint") from None
            view = ApprovalView(thread_id=anchor.thread_id, status="finalized", report=anchor.report, outcome=record)
        return anchor, graph, view

    def start(self, report: ReviewReport, *, thread_id: UUID) -> ApprovalView:
        thread = self._thread(thread_id)
        report = _restore_report(report)
        anchor = ApprovalAnchor(thread_id=thread_id, report=report, reviewers=tuple(sorted(self.reviewers)))
        payload, digest = pack(anchor.model_dump(mode="json"))  # Before creating any storage.
        with tracing_context(enabled=False), transaction(self.database, create=True, busy_timeout_ms=self.busy_timeout_ms) as connection:
            if connection.execute("SELECT 1 FROM threads WHERE thread_id=?", (thread,)).fetchone():
                raise ApprovalError("duplicate_thread")
            current = self._current(connection, report.review_id)
            if current is not None and report.report_revision <= current[0]:
                raise ApprovalError("stale_report")
            connection.execute("INSERT INTO threads VALUES (?,?,?,'preparing',NULL)", (thread, payload, digest))
            connection.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?,?)", (str(report.review_id), report.report_revision, report.report_id, thread))
            graph = _graph(ApprovalSaver(connection, thread))
            graph.invoke({"anchor": anchor.model_dump(mode="json")}, self._config(thread), context=_Authorization(self.reviewers), durability="sync")
            connection.execute("UPDATE threads SET phase='pending' WHERE thread_id=?", (thread,))
            return self._load(connection, thread)[2]

    def get(self, thread_id: UUID) -> ApprovalView:
        thread = self._thread(thread_id)
        with tracing_context(enabled=False), transaction(self.database, busy_timeout_ms=self.busy_timeout_ms) as connection:
            return self._load(connection, thread)[2]

    def submit(self, thread_id: UUID, decision, *, submitted_by: str) -> ApprovalView:
        thread, proposed = self._thread(thread_id), _decision(decision)
        auth = _Authorization(self.reviewers, submitted_by, datetime.now(timezone.utc))
        with tracing_context(enabled=False), transaction(self.database, busy_timeout_ms=self.busy_timeout_ms) as connection:
            anchor, graph, view = self._load(connection, thread)
            if view.status == "finalized":
                raise ApprovalError("already_finalized")
            if view.status == "superseded":
                raise ApprovalError("stale_report")
            _authorize(anchor, proposed, auth)  # Before any resume payload can be persisted.
            state = graph.invoke(Command(resume=proposed.model_dump(mode="json")), self._config(thread), context=auth, durability="sync")
            record = DecisionRecord.model_validate_json(json.dumps(state["record"]))
            payload, digest = pack(record.model_dump(mode="json"))
            connection.execute("INSERT INTO decisions VALUES (?,?,?)", (thread, payload, digest))
            connection.execute("UPDATE threads SET phase='finalized' WHERE thread_id=?", (thread,))
            return self._load(connection, thread)[2]
