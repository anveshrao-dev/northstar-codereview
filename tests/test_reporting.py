"""Deterministic reports from synthetic, evidence-validated orchestration results."""

import asyncio
import json
import re
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from northstar_codereview.agents.code_quality import Usage
from northstar_codereview.local_source import collect_local_snapshot
from northstar_codereview.models import (AgentName, EvidenceReference, FindingProposal, ReviewRequest,
                                        SourceSide, SpecialistResult, validate_finding)
from northstar_codereview.orchestrator import (OrchestrationResult, ReviewOrchestrator,
                                             SpecialistExecution, SpecialistFailure)
from northstar_codereview.reporting import (ReviewReport, ReportInputError, build_report,
                                          render_json, render_markdown)


NAMES = (AgentName.CODE_QUALITY, AgentName.SECURITY, AgentName.TEST_COVERAGE)


@pytest.fixture
def snapshot(tmp_path):
    (tmp_path / "alpha.py").write_text('def first(value):\n    return value\n# RAW_SOURCE_SECRET_SENTINEL\n')
    (tmp_path / "beta.py").write_text('def second(value):\n    return value + 1\n')
    return collect_local_snapshot(ReviewRequest(mode="local", local_path="."), source_root=tmp_path)


def finding(snapshot, *, name=AgentName.CODE_QUALITY, severity="medium", path="alpha.py", line=2,
            title="Synthetic finding", confidence=0.5, **overrides):
    source = snapshot.sources[("head", path)].splitlines(keepends=True)
    data = dict(title=title, category={AgentName.CODE_QUALITY: "correctness", AgentName.SECURITY: "security",
                                      AgentName.TEST_COVERAGE: "tests"}[name],
                severity=severity, confidence=confidence, explanation="Potential behavior requires human review.",
                suggested_remediation="Investigate the cited behavior with a focused test.",
                assumptions=["Runtime behavior is unverified."], evidence=[EvidenceReference(
                    snapshot_id=snapshot.manifest.snapshot_id, file_path=path, start_line=line, end_line=line,
                    snippet=source[line - 1])])
    data.update(overrides)
    return validate_finding(FindingProposal(**data), snapshot.manifest, snapshot.sources, agent_name=name)


def outcome(snapshot, findings=(), *, statuses=None, status="completed", limitations=(), selected=NAMES):
    review_id = uuid4()
    statuses = statuses or {}
    executions = []
    for name in NAMES:
        assigned = tuple(item for item in findings if item.agent_name == name)
        state = statuses.get(name, "completed" if name in selected else "skipped")
        job_id = uuid4()
        failure = SpecialistFailure(agent_name=name, job_id=job_id, category="exception") if state == "failed" else None
        notes = ("Specialist context is incomplete.",) if state != "completed" else ()
        usage = None if state in {"failed", "skipped"} else Usage(
            model_calls=2, tool_rounds=1, tool_attempts=1, retries=0, repairs=0,
            input_tokens_reserved=500, output_tokens_reserved=2000, reported_input_tokens=0,
            reported_output_tokens=0, usage_unknown_calls=2)
        result = SpecialistResult(job_id=job_id, review_id=review_id, snapshot_id=snapshot.manifest.snapshot_id,
                                  agent_name=name, status=state, findings=tuple(f.proposal for f in assigned),
                                  reviewed_files=("alpha.py", "beta.py") if state == "completed" else (), limitations=notes)
        executions.append(SpecialistExecution(result=result, validated_findings=assigned, usage=usage, failure=failure))
    return OrchestrationResult(review_id=review_id, snapshot_id=snapshot.manifest.snapshot_id,
                               selected_specialists=selected, specialist_results=tuple(executions),
                               validated_findings=tuple(findings), limitations=limitations,
                               failures=tuple(e.failure for e in executions if e.failure), status=status)


def report(snapshot, result, **kwargs):
    return build_report(result, snapshot=snapshot.manifest, **kwargs)


def test_severity_order_and_confidence_do_not_change_proposed_priority(snapshot):
    findings = [finding(snapshot, severity=severity, title=severity, confidence=confidence)
                for severity, confidence in [("low", .99), ("medium", .9), ("critical", .1), ("high", .4)]]
    result = report(snapshot, outcome(snapshot, findings))
    assert [f.severity.value for f in result.findings] == ["critical", "high", "medium", "low"]
    assert [f.confidence for f in result.findings] == [.1, .4, .9, .99]
    assert result.severity_counts.model_dump() == dict(critical=1, high=1, medium=1, low=1)


def test_stable_tiebreak_order_path_line_category_identity(snapshot):
    findings = [finding(snapshot, path="beta.py"), finding(snapshot, line=2, title="second"),
                finding(snapshot, name=AgentName.SECURITY), finding(snapshot, line=1), finding(snapshot, line=2, title="first")]
    result = report(snapshot, outcome(snapshot, findings))
    expected = sorted(findings, key=lambda f: (f.proposal.evidence[0].file_path, f.proposal.evidence[0].start_line,
                                              f.proposal.evidence[0].end_line, f.proposal.evidence[0].side.value,
                                              f.proposal.category.value, str(f.finding_id)))
    assert [f.finding_id for f in result.findings] == [f.finding_id for f in expected]


def test_report_and_rendering_deterministic_under_delivery_order(snapshot):
    findings = [finding(snapshot, name=name) for name in NAMES]
    original = outcome(snapshot, findings, limitations=("Z limitation", "A limitation"))
    shuffled = original.model_copy(update={"specialist_results": tuple(reversed(original.specialist_results)),
                                          "validated_findings": tuple(reversed(original.validated_findings)),
                                          "selected_specialists": tuple(reversed(original.selected_specialists)),
                                          "limitations": tuple(reversed(original.limitations))})
    first, second = report(snapshot, original), report(snapshot, shuffled)
    assert first == second
    assert render_json(first) == render_json(second)
    assert render_markdown(first) == render_markdown(second)
    assert len(first.report_id) == 64 and "timestamp" not in first.model_dump_json()


def test_report_revision_changes_identity_without_mutating_input(snapshot):
    original = outcome(snapshot, [finding(snapshot)])
    before = original.model_dump_json()
    first, second = report(snapshot, original), report(snapshot, original, report_revision=2)
    assert first.report_revision == 1 and second.report_revision == 2
    assert first.report_id != second.report_id
    assert before == original.model_dump_json()
    assert first.snapshot_revision == snapshot.manifest.revision


@pytest.mark.parametrize("revision", [0, -1, True, "1", 1.5])
def test_report_revision_must_be_positive_strict_integer(snapshot, revision):
    with pytest.raises(ReportInputError):
        report(snapshot, outcome(snapshot), report_revision=revision)


def test_json_round_trip_and_evidence_projection_preserve_statuses(snapshot):
    original = finding(snapshot, name=AgentName.SECURITY, line=3)
    result = report(snapshot, outcome(snapshot, [original]))
    payload = json.loads(render_json(result))
    assert ReviewReport.model_validate(payload) == result
    item = payload["findings"][0]
    assert item["finding_id"] == str(original.finding_id) and item["agent_name"] == "security"
    assert item["evidence_status"] == "validated" and item["claim_status"] == "unverified"
    assert item["evidence"][0]["file_path"] == "alpha.py" and item["evidence"][0]["start_line"] == 3
    assert "snippet" not in render_json(result) and "RAW_SOURCE_SECRET_SENTINEL" not in render_json(result)
    assert "RAW_SOURCE_SECRET_SENTINEL" not in render_markdown(result)
    assert "events" not in payload and "messages" not in payload


def test_markdown_has_summary_counts_actions_evidence_and_coverage(snapshot):
    result = report(snapshot, outcome(snapshot, [finding(snapshot, severity="high")]))
    text = render_markdown(result)
    for expected in ("# Northstar CodeReview", "Overall status: completed", "## Severity counts",
                     "## Findings", "## Specialist coverage", "Evidence: validated", "Claim: unverified",
                     "Recommended action:", "alpha.py", "2-2", "high", "code_quality"):
        assert expected in text
    assert "does not prove" in text and "Runtime behavior is unverified." in text


@pytest.mark.parametrize("state", ["partial", "failed", "skipped"])
def test_incomplete_specialist_cannot_produce_completed_report(snapshot, state):
    original = outcome(snapshot, statuses={AgentName.SECURITY: state})
    result = report(snapshot, original)
    assert result.status != "completed"
    assert next(s.status for s in result.specialists if s.agent_name == AgentName.SECURITY) == state
    assert any("completion" in note for note in result.limitations)


def test_failed_and_partial_review_statuses_are_preserved_with_findings(snapshot):
    findings = [finding(snapshot)]
    original = outcome(snapshot, findings, statuses={AgentName.SECURITY: "failed"}, status="partial")
    result = report(snapshot, original)
    assert result.status == "partial" and len(result.findings) == 1
    assert result.failures[0].category == "exception"
    failed = next(s for s in result.specialists if s.agent_name == AgentName.SECURITY)
    assert failed.usage is None and failed.finding_count == 0
    assert "exception" in render_markdown(result)
    all_failed = outcome(snapshot, statuses={name: "failed" for name in NAMES}, status="failed")
    assert report(snapshot, all_failed).status == "failed"


def test_no_findings_complete_review_is_not_a_correctness_guarantee(snapshot):
    result = report(snapshot, outcome(snapshot))
    text = render_markdown(result)
    assert result.status == "completed" and not result.findings
    assert "No findings were produced." in text
    assert "not a guarantee" in text
    assert result.severity_counts.model_dump() == dict(critical=0, high=0, medium=0, low=0)


@pytest.mark.parametrize("state", ["partial", "failed", "skipped"])
def test_empty_incomplete_report_explicitly_warns_about_coverage(snapshot, state):
    result = report(snapshot, outcome(snapshot, status=state, limitations=("Incomplete review scope.",)))
    text = render_markdown(result)
    assert "No findings were produced, but review coverage was incomplete." in text
    assert result.status == state


def test_identical_finding_duplicates_fold_by_identity(snapshot):
    item = finding(snapshot)
    original = outcome(snapshot, [item])
    duplicate = original.model_copy(update={"validated_findings": (item, item)})
    result = report(snapshot, duplicate)
    assert len(result.findings) == 1 and result.severity_counts.medium == 1
    assert result == report(snapshot, original)


def test_conflicting_same_identity_is_explicit_input_error(snapshot):
    one = finding(snapshot)
    altered = one.model_copy(update={"proposal": one.proposal.model_copy(update={"title": "Different semantics"})})
    original = outcome(snapshot, [one]).model_copy(update={"validated_findings": (one, altered)})
    with pytest.raises(ReportInputError, match="conflicting_finding_identity"):
        report(snapshot, original)


def test_opposing_and_cross_agent_findings_on_same_lines_are_preserved(snapshot):
    findings = [finding(snapshot, title="Increase the threshold"), finding(snapshot, title="Decrease the threshold"),
                finding(snapshot, name=AgentName.SECURITY, title="Separate security risk")]
    result = report(snapshot, outcome(snapshot, findings))
    assert {f.title for f in result.findings} == {f.proposal.title for f in findings}
    assert {f.agent_name for f in result.findings} == {AgentName.CODE_QUALITY, AgentName.SECURITY}
    assert len(result.findings) == 3
    assert result.notices and all(n.category == "overlapping_evidence" for n in result.notices)
    assert "not resolved" in render_markdown(result)


def test_nonoverlapping_findings_have_no_overlap_notice(snapshot):
    result = report(snapshot, outcome(snapshot, [finding(snapshot, line=1), finding(snapshot, path="beta.py")]))
    assert not result.notices


@pytest.mark.parametrize("field", ["snapshot_id", "review_id", "job_id"])
def test_inconsistent_specialist_identity_rejected(snapshot, field):
    original = outcome(snapshot)
    executions = list(original.specialist_results)
    changed = executions[0].result.model_copy(update={field: uuid4()})
    executions[0] = executions[0].model_copy(update={"result": changed})
    # A job must also disagree with its failure or duplicate another job to be invalid.
    if field == "job_id":
        executions[0] = executions[0].model_copy(update={"result": changed.model_copy(update={"job_id": executions[1].result.job_id})})
    original = original.model_copy(update={"specialist_results": tuple(executions)})
    with pytest.raises(ReportInputError):
        report(snapshot, original)


def test_wrong_snapshot_manifest_rejected(snapshot):
    manifest = snapshot.manifest.model_copy(update={"snapshot_id": uuid4()})
    with pytest.raises(ReportInputError, match="snapshot"):
        build_report(outcome(snapshot), snapshot=manifest)


@pytest.mark.parametrize("field,value", [("evidence_status", "invalid"), ("claim_status", "confirmed")])
def test_validation_and_claim_status_cannot_be_promoted(snapshot, field, value):
    item = finding(snapshot).model_copy(update={field: value})
    with pytest.raises(ReportInputError):
        report(snapshot, outcome(snapshot, [item]))


def test_orphan_aggregate_finding_rejected(snapshot):
    original = outcome(snapshot).model_copy(update={"validated_findings": (finding(snapshot),)})
    with pytest.raises(ReportInputError):
        report(snapshot, original)


def test_missing_selected_specialist_cannot_disappear_from_report(snapshot):
    original = outcome(snapshot)
    original = original.model_copy(update={"specialist_results": original.specialist_results[:-1]})
    with pytest.raises(ReportInputError):
        report(snapshot, original)


@pytest.mark.parametrize("hostile", [
    "[click](https://example.invalid/path)", "![pixel](https://example.invalid/pixel)",
    "<script>alert('synthetic')</script>", "<img src=x onerror=alert(1)>",
    "\n# Forged approval\n\n[ref]: https://example.invalid/\n",
    "`close` ``close again`` ```<b>escape</b>```", "https://example.invalid/autolink",
    "row | forged | cells", "text\r\n## Forged heading\n- forged item",
    "visible\u202ehidden\u2028# forged", "&lt;script&gt;entity&lt;/script&gt;",
])
def test_untrusted_prose_cannot_change_markdown_structure(snapshot, hostile):
    item = finding(snapshot, explanation=hostile, suggested_remediation=hostile, assumptions=[hostile])
    result = report(snapshot, outcome(snapshot, [item], limitations=(hostile,)))
    text = render_markdown(result)
    headings = [line for line in text.splitlines() if line.startswith("#")]
    assert headings == ["# Northstar CodeReview", "## Severity counts", "## Findings", "### Finding 1: medium",
                        "## Specialist coverage", "## Limitations", "## Failures", "## Overlap notices"]
    assert "\u202e" not in text and "\u2028" not in text and "\r" not in text
    line = next(line for line in text.splitlines() if line.startswith("**Explanation:** "))
    encoded = line.removeprefix("**Explanation:** ")
    # Literal code-span boundary surrounds the entire untrusted field. Its
    # delimiter is longer than every interior backtick run and cannot close early.
    delimiter = re.match(r"`+", encoded).group()
    assert encoded.endswith(delimiter) and encoded[len(delimiter)] == " "
    interior = encoded[len(delimiter) + 1:-len(delimiter) - 1]
    assert all(len(run) < len(delimiter) for run in re.findall(r"`+", interior))
    assert "\n" not in interior and "\r" not in interior
    assert json.loads(render_json(result))["findings"][0]["explanation"] == item.proposal.explanation


def test_untrusted_filename_and_title_are_not_markdown_links_or_headings(tmp_path):
    name = "[fake](target)`<img>.py"
    (tmp_path / name).write_text("fictional\n")
    snapshot = collect_local_snapshot(ReviewRequest(mode="local", local_path="."), source_root=tmp_path)
    item = finding(snapshot, path=name, line=1, title="\n# False completed review\n")
    original = outcome(snapshot, [item])
    original = original.model_copy(update={"specialist_results": tuple(e.model_copy(update={"result": e.result.model_copy(update={"reviewed_files": (name,)})})
                                                                       for e in original.specialist_results)})
    text = render_markdown(report(snapshot, original))
    assert not any(line.startswith("# False") for line in text.splitlines())
    assert "**Title:** ` # False completed review `" in text
    assert "`` [fake](target)`<img>.py ``" in text


def test_report_identity_counts_and_status_checked_when_loading_json(snapshot):
    result = report(snapshot, outcome(snapshot, [finding(snapshot)]))
    payload = json.loads(render_json(result))
    payload["severity_counts"]["critical"] = 9
    with pytest.raises(ValidationError):
        ReviewReport.model_validate(payload)
    payload = json.loads(render_json(result))
    payload["findings"][0]["title"] = "Tampered title"
    with pytest.raises(ValidationError):
        ReviewReport.model_validate(payload)


def test_builder_and_renderers_do_not_modify_files_or_call_network(snapshot, monkeypatch):
    from pathlib import Path
    import subprocess
    def forbidden(*args, **kwargs):
        raise AssertionError("Report generation must not access files or execute processes")
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    original = outcome(snapshot, [finding(snapshot)])
    result = report(snapshot, original)
    assert render_json(result) and render_markdown(result)


def test_real_orchestrator_result_to_report_without_graph_changes(snapshot):
    class EmptyModel:
        def bind_tools(self, tools):
            return self
        async def ainvoke(self, messages, **kwargs):
            return AIMessage(content='{"findings": []}')
    original = asyncio.run(ReviewOrchestrator(snapshot, model_factory=lambda name: EmptyModel(), approved_synthetic=True).run(review_id=uuid4()))
    result = report(snapshot, original)
    assert result.status == "partial" and not result.findings
    assert len(result.specialists) == 5
    assert all(s.usage.model_calls == 1 for s in result.specialists if s.selected)
    assert all(s.status == "skipped" and s.usage is None for s in result.specialists if not s.selected)
    assert "No findings were produced, but review coverage was incomplete." in render_markdown(result)


def test_minimum_anchor_sets_priority_even_if_evidence_arrives_in_another_order(snapshot):
    alpha = finding(snapshot, line=1)
    beta = finding(snapshot, path="beta.py", line=2)
    combined = finding(snapshot, title="Multiple source anchors", evidence=[*beta.proposal.evidence, *alpha.proposal.evidence])
    later = finding(snapshot, line=2)
    result = report(snapshot, outcome(snapshot, [later, combined]))
    assert [f.finding_id for f in result.findings] == [combined.finding_id, later.finding_id]
    assert result.findings[0].evidence[0].file_path == "alpha.py"


def test_base_and_head_lines_are_distinct_evidence_locations(snapshot):
    from northstar_codereview.local_source import _assemble
    sources = dict(snapshot.sources)
    sources[(SourceSide.BASE, "alpha.py")] = 'def first(value):\n    return value - 1\n'
    snapshot = _assemble(sources)
    head = finding(snapshot)
    base_ref = EvidenceReference(snapshot_id=snapshot.manifest.snapshot_id, file_path="alpha.py", side="base",
                                 start_line=2, end_line=2, snippet="    return value - 1\n")
    base = finding(snapshot, title="Base-side observation", evidence=[base_ref])
    result = report(snapshot, outcome(snapshot, [head, base]))
    assert len(result.findings) == 2 and not result.notices
    assert {f.evidence[0].side for f in result.findings} == {SourceSide.BASE, SourceSide.HEAD}


def test_valid_maximum_length_limitations_survive_projection(snapshot):
    note = "L" * 8000
    result = report(snapshot, outcome(snapshot, limitations=(note,)))
    assert result.limitations == (note,)
    assert note in render_markdown(result)


def test_nonselected_skips_do_not_make_selected_success_incomplete(snapshot):
    result = report(snapshot, outcome(snapshot, selected=(AgentName.SECURITY,)))
    assert result.status == "completed"
    assert [s.status for s in result.specialists] == ["skipped", "completed", "skipped"]


def test_failure_order_does_not_change_report_identity(snapshot):
    original = outcome(snapshot, statuses={name: "failed" for name in NAMES}, status="failed")
    reverse = original.model_copy(update={"failures": tuple(reversed(original.failures)),
                                         "specialist_results": tuple(reversed(original.specialist_results))})
    assert report(snapshot, original) == report(snapshot, reverse)


@pytest.mark.parametrize("renderer", [render_json, render_markdown])
def test_renderers_reject_unchecked_report_mutation_without_payload_leak(snapshot, renderer):
    result = report(snapshot, outcome(snapshot, [finding(snapshot)]))
    altered = result.findings[0].model_copy(update={"explanation": "FAKE_SECRET_PAYLOAD_SENTINEL"})
    result = result.model_copy(update={"findings": (altered,)})
    with pytest.raises(ReportInputError) as error:
        renderer(result)
    assert "FAKE_SECRET_PAYLOAD_SENTINEL" not in str(error.value)


@pytest.mark.parametrize("field,value", [("snapshot_id", uuid4()), ("file_path", "missing.py")])
def test_projected_evidence_must_belong_to_matching_manifest(snapshot, field, value):
    original = finding(snapshot)
    altered_ref = original.proposal.evidence[0].model_copy(update={field: value})
    altered = original.model_copy(update={"proposal": original.proposal.model_copy(update={"evidence": (altered_ref,)})})
    collected = outcome(snapshot, [original])
    first, *remaining = collected.specialist_results
    first = first.model_copy(update={"validated_findings": (altered,),
                                    "result": first.result.model_copy(update={"findings": (altered.proposal,)})})
    collected = collected.model_copy(update={"validated_findings": (altered,), "specialist_results": (first, *remaining)})
    with pytest.raises(ReportInputError):
        report(snapshot, collected)


@pytest.mark.parametrize("confidence", [0.9999999, 0.1234567890123456, 0.000000123456789])
def test_markdown_preserves_confidence_without_rounding_to_certainty(snapshot, confidence):
    result = report(snapshot, outcome(snapshot, [finding(snapshot, confidence=confidence)]))
    text = render_markdown(result)
    value = next(line.removeprefix("Model confidence: ") for line in text.splitlines() if line.startswith("Model confidence: "))
    assert float(value) == confidence
    assert value == str(json.loads(render_json(result))["findings"][0]["confidence"])
