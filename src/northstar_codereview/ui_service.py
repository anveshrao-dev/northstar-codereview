"""Local UI application boundary. No live runner or publication action.

Saved artifacts are bounded, no-follow reads from one fixed runtime directory.
Checksums detect corruption, not malicious same-user rewriting or provenance.
"""
import asyncio
import json
import os
import re
import stat
from contextlib import ExitStack
from pathlib import Path
from uuid import UUID, NAMESPACE_URL, uuid5

from .approval import ApprovalWorkflow
from .approval_models import ApprovalDecision, ApprovalError
from .evaluation import fingerprint, render_evaluation
from .evaluation_data import load_dataset, load_scenario
from .evaluation_runner import run_scripted
from .live_evaluation import LiveEvaluation, DATASET_DIGEST, render_live_json
from .local_source import PROJECT_ROOT
from .output_safety import contains_sensitive
from .reporting import ReviewReport, render_json
from .ui_models import ReviewBundle, SCENARIOS

MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_CATALOG_BYTES = 16 * 1024 * 1024
MAX_ARTIFACTS = 64
LOCAL_REVIEWER = 'local-ui-operator'


class UIError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('Local UI operation unavailable.')


def pinned_dataset():
    dataset = load_dataset()
    if fingerprint(dataset) != DATASET_DIGEST or tuple(s.id for s in dataset.scenarios) != SCENARIOS:
        raise UIError('dataset_rejected')
    return dataset


def run_baseline(scenario_id: str) -> ReviewBundle:
    if scenario_id not in SCENARIOS:
        raise UIError('scenario_rejected')
    try:
        result = asyncio.run(run_scripted(pinned_dataset(), scenario_id))
        return ReviewBundle(scenario_id=scenario_id, provenance='scripted_integration',
            report=result.report, evaluation=result.evaluation, run_status=result.report.status,
            unknown_usage=result.evaluation.workflow.unknown_usage_attempts,
            elapsed_seconds=result.evaluation.workflow.elapsed_seconds,
            model_attempts=result.evaluation.workflow.model_attempts,
            tool_attempts=result.evaluation.workflow.tool_attempts)
    except Exception:
        raise UIError('scripted_run_failed') from None


def scripted_evidence(bundle, finding_id, index):
    """Local bounded excerpt from pinned fictional data, never an imported snapshot."""
    from .evidence_tools import read_source_lines
    try:
        bundle = ReviewBundle.model_validate_json(bundle.model_dump_json())
        dataset = pinned_dataset()
        if (bundle.provenance != 'scripted_integration' or bundle.report is None
                or bundle.evaluation is None or bundle.evaluation.dataset_id != fingerprint(dataset)):
            raise ValueError
        scenario = next(s for s in dataset.scenarios if s.id == bundle.scenario_id)
        snapshot = load_scenario(scenario)
        if (bundle.report.snapshot_id, bundle.report.snapshot_revision) != (snapshot.manifest.snapshot_id, snapshot.manifest.revision):
            raise ValueError
        finding = next(f for f in bundle.report.findings if f.finding_id == UUID(str(finding_id)))
        if type(index) is not int or not 0 <= index < len(finding.evidence):
            raise ValueError
        ref = finding.evidence[index]
        if not any((f.file_path, f.side, f.sha256) == (ref.file_path, ref.side, ref.source_sha256) for f in snapshot.manifest.files):
            raise ValueError
        result = read_source_lines(snapshot, file_path=ref.file_path, side=ref.side,
                                   start_line=ref.start_line, end_line=ref.end_line)
        if result.status != 'ok': raise ValueError
        return result.data.text
    except Exception:
        raise UIError('fictional_evidence_unavailable') from None


def _artifact_payloads():
    """Fixed directory, bounded work, no symlinks, FIFOs, arbitrary uploads/paths."""
    try:
        with ExitStack() as stack:
            fd = os.open(PROJECT_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, fd)
            for part in ('.runtime', 'evaluation'):
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                stack.callback(os.close, fd)
            names = [n for n in os.listdir(fd) if re.fullmatch(r'[0-9a-f]{64}\.json', n)]
            # mtime selects recent records; it is never treated as trusted run time.
            names.sort(key=lambda n: os.stat(n, dir_fd=fd, follow_symlinks=False).st_mtime_ns, reverse=True)
            consumed = 0
            for name in names[:MAX_ARTIFACTS]:
                handle = None
                try:
                    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    info = os.fstat(handle)
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                            or not 0 < info.st_size <= MAX_ARTIFACT_BYTES):
                        continue
                    if consumed + info.st_size > MAX_CATALOG_BYTES:
                        break
                    consumed += info.st_size
                    with os.fdopen(handle, 'rb') as source:
                        handle = None
                        raw = source.read(MAX_ARTIFACT_BYTES + 1)
                    if len(raw) != info.st_size:
                        continue
                    payload = json.loads(raw)
                    if fingerprint(payload) != name[:-5] or contains_sensitive(payload):
                        continue
                    yield name, payload
                except (OSError, ValueError, TypeError, RecursionError):
                    continue
                finally:
                    if handle is not None:
                        os.close(handle)
    except (OSError, ValueError):
        return


def saved_live(scenario_id: str) -> tuple[ReviewBundle, ...]:
    if scenario_id not in SCENARIOS:
        raise UIError('scenario_rejected')
    dataset = pinned_dataset()
    scenario = next(s for s in dataset.scenarios if s.id == scenario_id)
    snapshot = load_scenario(scenario)
    results = []
    for name, payload in _artifact_payloads():
        try:
            live = LiveEvaluation.model_validate(payload)
            render_live_json(live)
            if not live.started or live.scenario_id != scenario_id or live.configuration.get('dataset_id') != DATASET_DIGEST:
                continue
            if live.report:
                render_json(live.report)
                if (live.report.snapshot_id != snapshot.manifest.snapshot_id
                        or live.report.snapshot_revision != snapshot.manifest.revision):
                    continue
                manifest = {(f.side, f.file_path): f for f in snapshot.manifest.files}
                for finding in live.report.findings:
                    for ref in finding.evidence:
                        entry = manifest[ref.side, ref.file_path]
                        if (entry.sha256 != ref.source_sha256
                                or ref.end_line > len(snapshot.sources[ref.side, ref.file_path].splitlines())):
                            raise ValueError('Source reference mismatch')
            if live.evaluation:
                render_evaluation(live.evaluation)
                if (live.evaluation.dataset_id != DATASET_DIGEST
                        or live.evaluation.configuration_id != fingerprint(live.configuration)
                        or live.evaluation.snapshot_id != snapshot.manifest.snapshot_id
                        or live.evaluation.snapshot_revision != snapshot.manifest.revision):
                    continue
            results.append(ReviewBundle(scenario_id=scenario_id, provenance='recorded_live', report=live.report,
                model=live.model if 'model' in live.model_fields_set else None,
                evaluation=live.evaluation, artifact_name=name, run_status=live.reason,
                input_tokens=live.budget_usage.reported_input_tokens,
                output_tokens=live.budget_usage.reported_output_tokens,
                unknown_usage=live.budget_usage.unknown_usage_attempts, elapsed_seconds=live.elapsed_seconds,
                model_attempts=live.budget_usage.model_attempts, tool_attempts=live.budget_usage.tool_attempts))
        except (ValueError, TypeError, KeyError):
            continue
    return tuple(results)


class LocalReview:
    """Unauthenticated local operator; no production identity or posting authority."""
    def __init__(self, database: Path | None = None):
        self.database = database or PROJECT_ROOT / '.runtime/approvals/reviews.sqlite3'
        self.workflow = ApprovalWorkflow(self.database, allowed_reviewers=(LOCAL_REVIEWER,))

    @staticmethod
    def thread_for(report):
        report = ReviewReport.model_validate_json(render_json(report))
        return uuid5(NAMESPACE_URL, 'northstar-local-ui:' + report.report_id)

    def start(self, report):
        thread = self.thread_for(report)
        try:
            return self.workflow.start(report, thread_id=thread)
        except ApprovalError as error:
            if error.code != 'duplicate_thread':
                raise
            return self.get(thread, report)

    def get(self, thread: UUID, report: ReviewReport):
        view = self.workflow.get(thread)
        if view.report.report_id != report.report_id:
            raise ApprovalError('report_hash_mismatch')
        return view

    def submit(self, thread, report, decision, *, acknowledge_partial=False):
        view = self.get(thread, report)
        proposed = ApprovalDecision(review_id=view.report.review_id, report_revision=view.report.report_revision,
            report_hash=view.report.report_id, reviewer_id=LOCAL_REVIEWER, decision=decision,
            acknowledge_partial=acknowledge_partial)
        return self.workflow.submit(thread, proposed, submitted_by=LOCAL_REVIEWER)
