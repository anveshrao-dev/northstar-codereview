"""Synthetic credentials only; no environment or real credential access."""

import json

import pytest

from northstar_codereview.local_source import collect_local_snapshot
from northstar_codereview.models import AgentName, ReviewRequest
from northstar_codereview.reporting import ReportInputError, build_report, render_json, render_markdown
from .test_reporting import snapshot, finding, outcome


SECRET = "sk-" + "FictionalOnlyNeverARealKey0123456789"
PATTERNS = [SECRET, "ghp_" + "F" * 36, "AKIA" + "F" * 16,
            "Authorization: Bearer FictionalCredential12345", 'password="fictional-value"',
            "https://fictional:password@example.invalid/path", "-----BEGIN RSA PRIVATE KEY-----",
            "AccountKey=FictionalStorageKey12345", "api_key%3DFictionalEncodedValue",
            "api\u200b_key=FictionalHiddenSeparator", "sig=FictionalSignedUrlValue"]


@pytest.mark.parametrize("secret", PATTERNS, ids=[f"pattern-{i}" for i in range(len(PATTERNS))])
@pytest.mark.parametrize("field", ["title", "explanation", "suggested_remediation", "assumptions"])
def test_sensitive_finding_is_withheld_without_changing_other_claims(snapshot, secret, field):
    value = [secret] if field == "assumptions" else secret
    unsafe = finding(snapshot, **{field: value})
    safe = finding(snapshot, name=AgentName.SECURITY, title="Independent source concern")
    original = outcome(snapshot, [unsafe, safe])
    report = build_report(original, snapshot=snapshot.manifest)
    assert [item.finding_id for item in report.findings] == [safe.finding_id]
    assert report.findings[0].title == safe.proposal.title
    assert report.status == "partial" and report.safety
    assert any(n.action == "finding_withheld" and n.finding_id == unsafe.finding_id for n in report.safety)
    assert sum(s.finding_count for s in report.specialists) == 1
    for output in (render_json(report), render_markdown(report), report.model_dump_json()):
        assert secret not in output
        assert "withheld" in output
    assert original.validated_findings == (unsafe, safe)


@pytest.mark.parametrize("location", ["limitations", "specialist_limitations", "reviewed_files", "evidence_path"])
def test_sensitive_metadata_is_withheld_without_raw_path_in_notice(tmp_path, location):
    path = SECRET + ".py" if "files" in location or "path" in location else "alpha.py"
    (tmp_path / path).write_text("# fictional\npass\n")
    (tmp_path / "beta.py").write_text("pass\n")
    snapshot = collect_local_snapshot(ReviewRequest(mode="local", local_path="."), source_root=tmp_path)
    item = finding(snapshot, path=path) if location == "evidence_path" else None
    result = outcome(snapshot, [item] if item else [])
    executions = []
    for execution in result.specialist_results:
        changes = {"reviewed_files": (path, "beta.py")}
        if location == "specialist_limitations":
            changes["limitations"] = (SECRET, "Retain this safe limitation.")
        executions.append(execution.model_copy(update={"result": execution.result.model_copy(update=changes)}))
    result = result.model_copy(update={"specialist_results": tuple(executions),
                                      "limitations": (SECRET,) if location == "limitations" else ()})
    report = build_report(result, snapshot=snapshot.manifest)
    assert SECRET not in render_json(report) and SECRET not in render_markdown(report)
    assert report.safety and report.status == "partial"
    if location == "specialist_limitations":
        assert "Retain this safe limitation." in render_markdown(report)


def test_safe_output_hash_is_deterministic_after_withholding(snapshot):
    result = outcome(snapshot, [finding(snapshot, title=SECRET), finding(snapshot, name=AgentName.SECURITY)], limitations=(SECRET, "Safe note"))
    shuffled = result.model_copy(update={"specialist_results": tuple(reversed(result.specialist_results)),
                                         "validated_findings": tuple(reversed(result.validated_findings)),
                                         "limitations": tuple(reversed(result.limitations))})
    first, second = [build_report(value, snapshot=snapshot.manifest) for value in (result, shuffled)]
    assert first.report_id == second.report_id
    assert render_json(first) == render_json(second) and render_markdown(first) == render_markdown(second)
    assert json.loads(render_json(first))["safety"]


@pytest.mark.parametrize("renderer", [render_json, render_markdown])
def test_render_boundary_blocks_bypassed_sensitive_report(snapshot, renderer):
    report = build_report(outcome(snapshot, [finding(snapshot)]), snapshot=snapshot.manifest)
    tampered = report.model_copy(update={"limitations": (SECRET,)})
    with pytest.raises(ReportInputError) as exc:
        renderer(tampered)
    assert SECRET not in str(exc.value)


def test_safe_rendering_preserves_nonsecret_markup_as_literal_data(snapshot):
    title = '<script>alert("fictional")</script> [click](https://example.invalid) ``` | \u202e'
    report = build_report(outcome(snapshot, [finding(snapshot, title=title)]), snapshot=snapshot.manifest)
    assert not report.safety and report.findings[0].title == title.strip()
    parsed = json.loads(render_json(report))
    assert parsed["findings"][0]["title"] == title.strip()
    markdown = render_markdown(report)
    assert "\\u202e" in markdown and "\u202e" not in markdown
    assert '```` <script>' in markdown
    assert "RAW_SOURCE_SECRET_SENTINEL" not in render_json(report)


@pytest.mark.parametrize("status", ["partial", "failed", "skipped"])
def test_sanitization_never_upgrades_incomplete_review(snapshot, status):
    report = build_report(outcome(snapshot, status=status, limitations=(SECRET,)), snapshot=snapshot.manifest)
    assert report.status == status and report.safety


@pytest.mark.parametrize("prose", [
    "AWS_SECRET_ACCESS_KEY=FictionalOnly123",
    "DATABASE_PASSWORD=FictionalOnly123",
    "NEBIUS_API_KEY=FictionalOnly123",
    "CI_JOB_TOKEN=FictionalOnly123",
    "AZURE_STORAGE_CONNECTION_STRING=FictionalOnly123",
    "password='x'",
    "Authorization: Bearer xyz",
], ids=["aws-variable", "password-variable", "nebius-variable", "ci-variable", "connection-variable", "short-password", "short-authorization"])
def test_named_and_short_credentials_are_blocked(snapshot, prose):
    report = build_report(outcome(snapshot, [finding(snapshot, explanation=prose)]), snapshot=snapshot.manifest)
    assert not report.findings and report.safety
    assert prose not in render_json(report) and prose not in render_markdown(report)


@pytest.mark.parametrize("location", ["limitations", "path"])
def test_prefixed_credential_metadata_is_withheld(tmp_path, location):
    secret = "DATABASE_PASSWORD=FictionalOnly123"
    path = secret + ".py" if location == "path" else "alpha.py"
    (tmp_path / path).write_text("# fictional\npass\n")
    (tmp_path / "beta.py").write_text("pass\n")
    captured = collect_local_snapshot(ReviewRequest(mode="local", local_path="."), source_root=tmp_path)
    result = outcome(captured, [finding(captured, path=path)], limitations=(secret,) if location == "limitations" else ())
    result = result.model_copy(update={"specialist_results": tuple(e.model_copy(update={"result": e.result.model_copy(
        update={"reviewed_files": (path, "beta.py")})}) for e in result.specialist_results)})
    report = build_report(result, snapshot=captured.manifest)
    assert report.safety and report.status == "partial"
    assert secret not in render_json(report) and secret not in render_markdown(report)


@pytest.mark.parametrize('legacy_version',[1,2,3])
def test_report_version_identifies_safety_and_inspection_contract(snapshot,legacy_version):
    from northstar_codereview.reporting import ReviewReport
    report = build_report(outcome(snapshot), snapshot=snapshot.manifest)
    assert report.schema_version == 4
    legacy = report.model_dump(mode="json")
    legacy["schema_version"] = legacy_version
    with pytest.raises(ValueError):
        ReviewReport.model_validate(legacy)
