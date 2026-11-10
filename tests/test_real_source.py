"""Only generated local text; never reads the original application projects."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import pytest
from pydantic import ValidationError

from northstar_codereview.real_source_contracts import (
    SAMPLES, SampleId, IntakeManifest, ImportApproval, TransmissionApproval,
    ApprovalState, snapshot_from_hashes,
)
from northstar_codereview.real_source import import_sample, load_import, IntakeRejected


def approval_for(sample=SampleId.PORTFOLIO_RISK, text="value = 1\r\n"):
    now = datetime.now(timezone.utc)
    manifest = IntakeManifest(sample=sample, project=SAMPLES[sample][0],
        snapshot=snapshot_from_hashes(sample, {p: sha256(text.encode()).hexdigest() for p in SAMPLES[sample][1]}),
        retain_until=now + timedelta(days=1))
    return ImportApproval(approval_id=uuid4(), manifest=manifest, state=ApprovalState.APPROVED,
                          approved_at=now, expires_at=now + timedelta(hours=1), privacy_reviewed=True)


def test_sample_contract_is_exact():
    receipt = approval_for()
    assert len(receipt.manifest.snapshot.files) == 2
    with pytest.raises(ValueError):
        snapshot_from_hashes(SampleId.PORTFOLIO_RISK, {"../escape.py": "a" * 64})
    with pytest.raises(ValidationError):
        IntakeManifest.model_validate({**receipt.manifest.model_dump(), "sample": "arbitrary"})


@pytest.fixture
def local_intake(tmp_path, monkeypatch):
    from northstar_codereview import real_source as intake
    root = tmp_path / "fictional"
    root.mkdir()
    for path in SAMPLES[SampleId.PORTFOLIO_RISK][1]:
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"value = 1\r\n")
    monkeypatch.setattr(intake, "_SOURCE_ROOTS", {SAMPLES[SampleId.PORTFOLIO_RISK][0]: root})
    monkeypatch.setattr(intake, "_STORE", tmp_path / "private-store")
    return root, intake


def test_import_is_hash_bound_and_preserves_line_endings(local_intake):
    _, intake = local_intake
    receipt = approval_for()
    result = import_sample(SampleId.PORTFOLIO_RISK, approval=receipt)
    assert result.status == "committed"
    snapshot = load_import(receipt.approval_id)
    assert snapshot.real_source.import_approval == receipt
    assert set(snapshot.sources.values()) == {"value = 1\r\n"}
    assert snapshot.manifest == receipt.manifest.snapshot


def test_missing_approval_never_opens_source(local_intake, monkeypatch):
    _, intake = local_intake
    monkeypatch.setattr(intake, "_capture", lambda *a: pytest.fail("unauthorized source access"))
    with pytest.raises(IntakeRejected, match="authorization"):
        import_sample(SampleId.PORTFOLIO_RISK, approval=None)


def transmission_for(receipt, specialists=("code_quality",)):
    return TransmissionApproval(approval_id=uuid4(), import_approval_id=receipt.approval_id,
        manifest_digest=receipt.manifest.digest, snapshot=receipt.manifest.snapshot,
        provider="nebius", model="Qwen/Qwen3-30B-A3B-Instruct-2507", state="approved", specialists=specialists,
        approved_at=datetime.now(timezone.utc), expires_at=receipt.expires_at)


@pytest.mark.parametrize("sample", list(SampleId))
def test_all_sample_allowlists_are_exact(sample):
    receipt = approval_for(sample)
    assert {f.file_path for f in receipt.manifest.snapshot.files} == set(SAMPLES[sample][1])
    assert all(f.file_path.endswith(".py") for f in receipt.manifest.snapshot.files)


@pytest.mark.parametrize("state", ["pending", "denied", "revoked", "unknown"])
def test_unapproved_states_fail_before_source_access(local_intake, monkeypatch, state):
    _, intake = local_intake
    monkeypatch.setattr(intake, "_capture", lambda *a: pytest.fail("source read"))
    receipt = approval_for().model_copy(update={"state": state})
    with pytest.raises(IntakeRejected, match="authorization"):
        import_sample(SampleId.PORTFOLIO_RISK, approval=receipt)


@pytest.mark.parametrize("update", ["expired", "future", "privacy", "snapshot", "project", "path", "extension"])
def test_stale_or_ambiguous_approval_rejected_before_read(local_intake, monkeypatch, update):
    _, intake = local_intake
    receipt = approval_for()
    data = receipt.model_dump()
    now = datetime.now(timezone.utc)
    if update == "expired":
        data.update(approved_at=now - timedelta(hours=2), expires_at=now - timedelta(hours=1))
    elif update == "future":
        data.update(approved_at=now + timedelta(minutes=10))
    elif update == "privacy":
        data["privacy_reviewed"] = False
    elif update == "snapshot":
        data["manifest"]["snapshot"]["snapshot_id"] = uuid4()
    elif update == "project":
        data["manifest"]["project"] = "northstar_atlas"
    else:
        data["manifest"]["snapshot"]["files"][0]["file_path"] = "../source.py" if update == "path" else "data.csv"
    receipt = ImportApproval.model_construct(**data)
    monkeypatch.setattr(intake, "_capture", lambda *a: pytest.fail("source read"))
    with pytest.raises(IntakeRejected):
        import_sample(SampleId.PORTFOLIO_RISK, approval=receipt)


def test_no_arbitrary_root_or_path_parameters(local_intake):
    with pytest.raises(TypeError):
        import_sample(SampleId.PORTFOLIO_RISK, approval=approval_for(), source_root="elsewhere")
    with pytest.raises(TypeError):
        import_sample(SampleId.PORTFOLIO_RISK, approval=approval_for(), selected_files=["elsewhere.py"])
    with pytest.raises(IntakeRejected):
        import_sample("../elsewhere", approval=approval_for())


@pytest.mark.parametrize("kind", ["symlink_file", "symlink_parent", "symlink_root", "fifo", "directory", "hardlink"])
def test_unsafe_files_never_committed(local_intake, kind):
    import os
    root, intake = local_intake
    path = root / SAMPLES[SampleId.PORTFOLIO_RISK][1][0]
    if kind == "symlink_root":
        alias = root.parent / "alias"
        alias.symlink_to(root, target_is_directory=True)
        intake._SOURCE_ROOTS[SAMPLES[SampleId.PORTFOLIO_RISK][0]] = alias
    elif kind == "symlink_parent":
        renamed = path.parent.with_name("renamed")
        path.parent.rename(renamed)
        path.parent.symlink_to(renamed, target_is_directory=True)
    else:
        path.unlink()
        if kind == "symlink_file":
            path.symlink_to(root / "tests/test_risk.py")
        elif kind == "fifo":
            os.mkfifo(path)
        elif kind == "directory":
            path.mkdir()
        else:
            os.link(root / "tests/test_risk.py", path)
    with pytest.raises(IntakeRejected):
        import_sample(SampleId.PORTFOLIO_RISK, approval=approval_for())
    assert not intake._STORE.exists()


@pytest.mark.parametrize("raw", [b"\x00binary", b"\xff\xfe", b"%PDF-data", b"PK\x03\x04data"])
def test_binary_never_written(local_intake, raw):
    root, intake = local_intake
    (root / SAMPLES[SampleId.PORTFOLIO_RISK][1][0]).write_bytes(raw)
    with pytest.raises(IntakeRejected, match="binary_content"):
        import_sample(SampleId.PORTFOLIO_RISK, approval=approval_for())
    assert not intake._STORE.exists()


@pytest.mark.parametrize("limits", [{"max_file_bytes": 2}, {"max_total_bytes": 15}, {"max_files": 1}])
def test_size_and_file_limits(local_intake, monkeypatch, limits):
    from northstar_codereview.local_source import IntakeLimits
    _, intake = local_intake
    monkeypatch.setattr(intake, "_LIMITS", IntakeLimits(**limits))
    with pytest.raises(IntakeRejected, match="size_limit"):
        import_sample(SampleId.PORTFOLIO_RISK, approval=approval_for())
    assert not intake._STORE.exists()


def test_production_limits_cannot_be_increased():
    from northstar_codereview.local_source import IntakeLimits
    for field, maximum in (("max_files", 50), ("max_file_bytes", 1024**2), ("max_total_bytes", 10 * 1024**2)):
        with pytest.raises(ValidationError):
            IntakeLimits(**{field: maximum + 1})


def test_changed_source_hash_rejected(local_intake):
    root, intake = local_intake
    receipt = approval_for()
    (root / "tests/test_risk.py").write_text("changed = True\n")
    with pytest.raises(IntakeRejected, match="source_integrity"):
        import_sample(SampleId.PORTFOLIO_RISK, approval=receipt)
    assert not intake._STORE.exists()


def test_mutation_during_capture_rejected(local_intake, monkeypatch):
    root, intake = local_intake
    original = intake._Collector.read_file
    def mutate(self, *args):
        original(self, *args)
        (root / "tests/test_risk.py").write_text("changed = True\n")
    monkeypatch.setattr(intake._Collector, "read_file", mutate)
    with pytest.raises(IntakeRejected, match="source_integrity"):
        import_sample(SampleId.PORTFOLIO_RISK, approval=approval_for())
    assert not intake._STORE.exists()


@pytest.mark.parametrize("text,category", [
    ("api_key = 'not-a-real-test-credential-value'\n", "credential_indicator"),
    ("-----BEGIN PRIVATE KEY-----\nfictional\n", "private_key_indicator"),
    ("contact = 'person@example.invalid'\n", "personal_data_indicator"),
    ("value = '123-45-6789'\n", "personal_data_indicator"),
    ("path = '/Users/fictional/work'\n", "workstation_path_indicator"),
    ("path = 'C:\\Users\\fictional\\work'\n", "workstation_path_indicator"),
    ("site = 'https://example.invalid'\n", "external_url_indicator"),
])
def test_privacy_scan_before_persistence_and_safe_errors(local_intake, text, category, caplog):
    import traceback
    root, intake = local_intake
    for path in SAMPLES[SampleId.PORTFOLIO_RISK][1]:
        (root / path).write_bytes(text.encode())
    with pytest.raises(IntakeRejected, match=category) as caught:
        import_sample(SampleId.PORTFOLIO_RISK, approval=approval_for(text=text))
    rendered = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert text.strip() not in rendered
    assert str(root) not in str(caught.value)
    assert not intake._STORE.exists()


def test_atomic_bundle_is_private_and_duplicate_rejected(local_intake):
    import stat
    _, intake = local_intake
    receipt = approval_for()
    import_sample(SampleId.PORTFOLIO_RISK, approval=receipt)
    before = {str(p.relative_to(intake._STORE)): p.read_bytes() for p in intake._STORE.rglob("*") if p.is_file()}
    with pytest.raises(IntakeRejected, match="conflicting_import"):
        import_sample(SampleId.PORTFOLIO_RISK, approval=receipt)
    for path in intake._STORE.rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    after = {str(p.relative_to(intake._STORE)): p.read_bytes() for p in intake._STORE.rglob("*") if p.is_file()}
    assert before == after


@pytest.mark.parametrize("failure", ["manifest", "snapshot", "receipt", "rename"])
def test_partial_write_cleanup(local_intake, monkeypatch, failure):
    _, intake = local_intake
    receipt = approval_for()
    write = intake._write
    names = {"manifest": "intake.json", "snapshot": "source.json", "receipt": "import.json"}
    def fail_write(parent, name, payload):
        write(parent, name, payload)
        if name == names.get(failure):
            raise OSError("synthetic write failure")
    monkeypatch.setattr(intake, "_write", fail_write)
    if failure == "rename":
        monkeypatch.setattr(intake.os, "rename", lambda *a, **kw: (_ for _ in ()).throw(OSError("synthetic rename failure")))
    with pytest.raises(IntakeRejected, match="storage_failure"):
        import_sample(SampleId.PORTFOLIO_RISK, approval=receipt)
    assert list((intake._STORE / "imports").iterdir()) == []
    assert list((intake._STORE / "claims").iterdir()) == []
    with pytest.raises(IntakeRejected):
        load_import(receipt.approval_id)


def test_import_not_visible_before_promotion(local_intake, monkeypatch):
    _, intake = local_intake
    receipt = approval_for()
    original = intake.os.rename
    def check(*args, **kwargs):
        with pytest.raises(IntakeRejected):
            load_import(receipt.approval_id)
        return original(*args, **kwargs)
    monkeypatch.setattr(intake.os, "rename", check)
    import_sample(SampleId.PORTFOLIO_RISK, approval=receipt)
    assert load_import(receipt.approval_id).real_source


def test_concurrent_duplicate_admission(local_intake):
    from concurrent.futures import ThreadPoolExecutor
    _, intake = local_intake
    receipt = approval_for()
    def attempt():
        try:
            import_sample(SampleId.PORTFOLIO_RISK, approval=receipt)
            return "committed"
        except IntakeRejected as e:
            return e.category.value
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(outcomes) == ["committed", "conflicting_import"]
    assert load_import(receipt.approval_id).manifest == receipt.manifest.snapshot


@pytest.mark.parametrize("tamper", ["content", "manifest", "receipt", "mode", "symlink"])
def test_storage_tampering_is_rejected(local_intake, tamper):
    import json
    _, intake = local_intake
    receipt = approval_for()
    import_sample(SampleId.PORTFOLIO_RISK, approval=receipt)
    bundle = intake._STORE / "imports" / str(receipt.approval_id)
    source = bundle / "snapshots/source.json"
    if tamper == "content":
        data = json.loads(source.read_bytes())
        data["tests/test_risk.py"] = "changed = 2\n"
        source.write_text(json.dumps(data))
    elif tamper == "manifest":
        (bundle / "manifests/intake.json").write_text("{}")
    elif tamper == "receipt":
        (bundle / "receipts/import.json").write_text("{}")
    elif tamper == "mode":
        source.chmod(0o644)
    else:
        source.unlink()
        source.symlink_to(bundle / "manifests/intake.json")
    with pytest.raises(IntakeRejected):
        load_import(receipt.approval_id)


def test_destination_symlink_never_written(local_intake):
    root, intake = local_intake
    intake._STORE.symlink_to(root, target_is_directory=True)
    with pytest.raises(IntakeRejected):
        import_sample(SampleId.PORTFOLIO_RISK, approval=approval_for())
    assert not (root / "imports").exists()


def test_reviewed_source_is_never_executed(local_intake):
    root, _ = local_intake
    text = "raise RuntimeError('reviewed source must never execute')\n"
    for path in SAMPLES[SampleId.PORTFOLIO_RISK][1]:
        (root / path).write_text(text)
    receipt = approval_for(text=text)
    import_sample(SampleId.PORTFOLIO_RISK, approval=receipt)
    assert set(load_import(receipt.approval_id).sources.values()) == {text}


@pytest.fixture
def imported(local_intake):
    receipt = approval_for()
    import_sample(SampleId.PORTFOLIO_RISK, approval=receipt)
    return load_import(receipt.approval_id), receipt


def test_import_and_synthetic_flag_do_not_authorize_tools(imported):
    from northstar_codereview.evidence_tools import build_snapshot_tools
    snapshot, receipt = imported
    for options in ({}, {"approved_synthetic": True},
                    {"approved_synthetic": True, "transmission_approval": transmission_for(receipt)}):
        with pytest.raises(IntakeRejected, match="authorization"):
            build_snapshot_tools(snapshot, **options)


@pytest.mark.parametrize("change", ["expired", "future", "state", "import_id", "snapshot", "digest", "provider", "model", "retention"])
def test_transmission_mismatch_fails_closed(imported, change):
    from northstar_codereview.evidence_tools import build_snapshot_tools
    snapshot, receipt = imported
    permit = transmission_for(receipt)
    now = datetime.now(timezone.utc)
    updates = {
        "expired": {"approved_at": now - timedelta(hours=2), "expires_at": now - timedelta(hours=1)},
        "future": {"approved_at": now + timedelta(minutes=10)}, "state": {"state": "revoked"},
        "import_id": {"import_approval_id": uuid4()}, "snapshot": {"snapshot": permit.snapshot.model_copy(update={"snapshot_id": uuid4()})},
        "digest": {"manifest_digest": "a" * 64}, "provider": {"provider": "other"}, "model": {"model": "other"},
        "retention": {"expires_at": receipt.manifest.retain_until + timedelta(seconds=1)},
    }
    with pytest.raises(IntakeRejected, match="authorization"):
        build_snapshot_tools(snapshot, transmission_approval=permit.model_copy(update=updates[change]))


def test_authorized_tools_preserve_exact_evidence_and_receipt(claimed_execution, imported):
    from northstar_codereview.evidence_tools import build_snapshot_tools
    from northstar_codereview.real_source import record_transmission
    snapshot, receipt = imported
    permit = claimed_execution[0]
    record_transmission(permit)
    tool = next(t for t in build_snapshot_tools(snapshot, transmission_approval=permit) if t.name == "read_source_lines")
    message = tool.invoke({"type": "tool_call", "id": "read-1", "name": tool.name,
                          "args": {"file_path": "tests/test_risk.py", "start_line": 1, "end_line": 1}})
    assert message.tool_call_id == "read-1"
    assert message.artifact.evidence[0].snippet == "value = 1\r\n"
    assert message.artifact.evidence[0].snapshot_id == snapshot.manifest.snapshot_id


def test_expiry_rechecked_when_tool_invoked(claimed_execution, imported, monkeypatch):
    from northstar_codereview import real_source_authorization as auth
    from northstar_codereview.evidence_tools import build_snapshot_tools
    snapshot, receipt = imported
    permit = claimed_execution[0]
    tool = build_snapshot_tools(snapshot, transmission_approval=permit)[0]
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return permit.expires_at + timedelta(seconds=1)
    monkeypatch.setattr(auth, "datetime", Later)
    with pytest.raises(IntakeRejected):
        tool.invoke({})


def test_modified_snapshot_cannot_reuse_approval(imported):
    from dataclasses import replace
    from northstar_codereview.evidence_tools import build_snapshot_tools
    snapshot, receipt = imported
    sources = dict(snapshot.sources)
    sources[("head", "tests/test_risk.py")] = "changed = True\n"
    with pytest.raises(IntakeRejected):
        build_snapshot_tools(replace(snapshot, sources=sources), transmission_approval=transmission_for(receipt))


class ReviewModel:
    """Local fake only; assertions exercise the real model/tool message flow."""
    transmission_identity = ("nebius", "Qwen/Qwen3-30B-A3B-Instruct-2507")

    def __init__(self, category="correctness", *, failure=False, after_first=None):
        self.category = category
        self.failure = failure
        self.after_first = after_first
        self.histories = []
        self.bindings = []

    def bind_tools(self, tools):
        self.tools = tools
        self.bindings.append(tuple(t.name for t in tools))
        return self

    async def ainvoke(self, messages, **kwargs):
        import json
        from langchain_core.messages import AIMessage, ToolMessage
        from northstar_codereview.agents.code_quality import TransientModelError
        self.histories.append(list(messages))
        assert "approved fictional snapshot" not in messages[1].content
        if self.failure:
            raise TransientModelError()
        if any('PREPARED_SOURCE_DATA=' in str(m.content) for m in messages):
            from .test_prepared_review import PreparedModel
            reply = await PreparedModel('valid').ainvoke(messages, **kwargs)
            payload = json.loads(reply.content)
            payload['findings'][0]['category'] = self.category
            return AIMessage(content=json.dumps(payload))
        if len(self.histories) == 1:
            assert len(self.tools) == 4
            assert "synthesis_json" not in kwargs
            if self.after_first:
                self.after_first()
            return AIMessage(content="", tool_calls=[{"name": "read_source_lines", "id": f"read-{index}",
                "args": {"file_path": path, "start_line": 1, "end_line": 1}}
                for index, path in enumerate(SAMPLES[SampleId.PORTFOLIO_RISK][1])])
        assert not self.tools and kwargs["synthesis_json"] is True
        observations = [m for m in messages if isinstance(m, ToolMessage)]
        assert [m.tool_call_id for m in observations] == ["read-0", "read-1"]
        evidence = json.loads(observations[0].content)["evidence"]
        proposal = dict(title="Synthetic workflow proposal", category=self.category, severity="low", confidence=0.5,
            explanation="A fictional workflow claim for validating the contract only.",
            suggested_remediation="Confirm the intended behavior before taking action.", evidence=evidence)
        return AIMessage(content=json.dumps({"findings": [proposal]}))


@pytest.mark.parametrize("agent_name,category", [
    ("CodeQualityAgent", "correctness"), ("SecurityAgent", "security"),
    ("TestCoverageAgent", "tests"), ("GitOpsAgent", "gitops"), ("CICDGroovyAgent", "cicd")])
def test_all_five_specialists_accept_separate_authority(claimed_execution, imported, agent_name, category):
    import asyncio
    from northstar_codereview.orchestrator import SPECIALIST_SPECS
    from northstar_codereview.agents.code_quality import RunLimits
    snapshot, receipt = imported
    agent_type = next(s.agent_type for s in SPECIALIST_SPECS if s.agent_type.__name__ == agent_name)
    model = ReviewModel(category)
    agent = agent_type(snapshot, model=model, transmission_approval=claimed_execution[0], prepared_context=False,
                       limits=RunLimits(max_model_calls=2, max_provider_retries=0, max_output_repairs=0))
    result = asyncio.run(agent.run(review_id=claimed_execution[1]))
    assert result.usage.model_calls == 2
    assert result.usage.tool_attempts == 2
    assert result.usage.retries == result.usage.repairs == 0
    assert len(result.validated_findings) == 1
    assert result.validated_findings[0].claim_status == "unverified"
    assert result.validated_findings[0].evidence_status == "validated"
    assert set(result.result.reviewed_files) == set(SAMPLES[SampleId.PORTFOLIO_RISK][1])
    assert len(model.bindings[0]) == 4 and model.bindings[-1] == ()


@pytest.mark.parametrize("identity", [None, ("other", "Qwen/Qwen3-30B-A3B-Instruct-2507"), ("nebius", "different")])
def test_agent_checks_actual_model_identity(claimed_execution, imported, identity):
    from northstar_codereview.agents.code_quality import CodeQualityAgent
    snapshot, receipt = imported
    model = ReviewModel()
    model.transmission_identity = identity
    with pytest.raises(IntakeRejected):
        CodeQualityAgent(snapshot, model=model, transmission_approval=claimed_execution[0])
    assert model.histories == [] and model.bindings == []


def test_real_source_does_not_enable_retries_or_repairs(claimed_execution, imported):
    import asyncio
    from northstar_codereview.agents.code_quality import CodeQualityAgent, RunLimits
    snapshot, receipt = imported
    permit = claimed_execution[0]
    with pytest.raises(IntakeRejected):
        CodeQualityAgent(snapshot, model=ReviewModel(), transmission_approval=permit, limits=RunLimits())
    model = ReviewModel(failure=True)
    agent = CodeQualityAgent(snapshot, model=model, transmission_approval=permit)
    result = asyncio.run(agent.run(review_id=claimed_execution[1]))
    assert len(model.histories) == 1
    assert result.usage.retries == result.usage.repairs == 0
    assert result.result.status == "partial"


def test_authorization_rechecked_before_followup_request(claimed_execution, imported, monkeypatch):
    import asyncio
    from northstar_codereview import real_source_authorization as auth
    from northstar_codereview.agents.code_quality import CodeQualityAgent
    snapshot, receipt = imported
    permit = claimed_execution[0]
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return permit.expires_at + timedelta(seconds=1)
    model = ReviewModel(after_first=lambda: monkeypatch.setattr(auth, "datetime", Later))
    result = asyncio.run(CodeQualityAgent(snapshot, model=model, transmission_approval=permit, prepared_context=False).run(review_id=claimed_execution[1]))
    assert len(model.histories) == 1
    assert not result.validated_findings
    assert result.result.status == "partial"
    assert not result.result.reviewed_files


def test_real_orchestrator_keeps_routing_and_independent_contexts(imported):
    import asyncio
    from northstar_codereview.agents.code_quality import RunLimits
    from northstar_codereview.orchestrator import ReviewOrchestrator
    snapshot, receipt = imported
    models = []
    def factory(name):
        model = ReviewModel({"code_quality": "correctness", "security": "security", "test_coverage": "tests"}[name.value])
        models.append(model)
        return model
    runner = ReviewOrchestrator(snapshot, model_factory=factory, transmission_approval=transmission_for(receipt, ("code_quality", "security", "test_coverage")),
                                specialist_limits=RunLimits(max_model_calls=2, max_provider_retries=0, max_output_repairs=0))
    output = asyncio.run(runner.run(review_id=uuid4()))
    assert len(models) == 3 and len({id(m) for m in models}) == 3
    assert all(len(m.histories) == 1 for m in models)
    assert len(output.validated_findings) == 3
    assert output.snapshot_id == snapshot.manifest.snapshot_id


def test_publication_approval_cannot_authorize_import_or_transmission(imported):
    from northstar_codereview.real_source_contracts import PublicationApproval
    from northstar_codereview.evidence_tools import build_snapshot_tools
    snapshot, receipt = imported
    publication = PublicationApproval(approval_id=uuid4(), manifest_digest=receipt.manifest.digest,
                                      state="approved", rights_confirmed=True)
    with pytest.raises(IntakeRejected):
        import_sample(SampleId.PORTFOLIO_RISK, approval=publication)
    with pytest.raises(IntakeRejected):
        build_snapshot_tools(snapshot, transmission_approval=publication)


def test_retention_expiry_disables_load(imported, monkeypatch):
    from northstar_codereview import real_source_contracts as contracts
    _, receipt = imported
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return receipt.manifest.retain_until + timedelta(seconds=1)
    monkeypatch.setattr(contracts, "datetime", Later)
    with pytest.raises(IntakeRejected):
        load_import(receipt.approval_id)


def test_no_automatic_dependency_discovery(local_intake):
    root, _ = local_intake
    extra = root / "src/stock_portfolio_analyzer/portfolio.py"
    extra.write_text("raise RuntimeError('out of approved scope')\n")
    receipt = approval_for()
    import_sample(SampleId.PORTFOLIO_RISK, approval=receipt)
    assert len(load_import(receipt.approval_id).sources) == 2


@pytest.mark.parametrize("operation", ["write", "fsync"])
def test_failed_claim_creation_is_cleaned(local_intake, monkeypatch, operation):
    _, intake = local_intake
    receipt = approval_for()
    original = getattr(intake.os, operation)
    calls = 0
    def fail_first(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic claim failure")
        return original(*args)
    monkeypatch.setattr(intake.os, operation, fail_first)
    with pytest.raises(IntakeRejected, match="storage_failure"):
        import_sample(SampleId.PORTFOLIO_RISK, approval=receipt)
    assert list((intake._STORE / "claims").iterdir()) == []
    assert list((intake._STORE / "imports").iterdir()) == []


@pytest.mark.parametrize("document,key,value", [
    ("receipts/import.json", "state", '"denied"'),
    ("manifests/intake.json", "purpose", '"wrong"'),
    ("snapshots/source.json", "tests/test_risk.py", '"changed"'),
])
def test_duplicate_json_authority_keys_rejected(imported, local_intake, document, key, value):
    import json
    _, receipt = imported
    _, intake = local_intake
    path = intake._STORE / "imports" / str(receipt.approval_id) / document
    text = path.read_text()
    path.write_text("{" + json.dumps(key) + ":" + value + "," + text[1:])
    with pytest.raises(IntakeRejected):
        load_import(receipt.approval_id)


@pytest.mark.parametrize("method", ["invoke", "run", "ainvoke", "arun"])
def test_standalone_real_tools_disable_ambient_tracing(claimed_execution, imported, monkeypatch, method):
    import asyncio
    from langchain_core.tools import StructuredTool
    from langsmith import get_tracing_context
    from northstar_codereview.evidence_tools import build_snapshot_tools
    snapshot, receipt = imported
    tool = build_snapshot_tools(snapshot, transmission_approval=claimed_execution[0])[0]
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    checks = []
    original_run, original_arun = StructuredTool.run, StructuredTool.arun
    def checked_run(self, *args, **kwargs):
        checks.append(get_tracing_context()["enabled"])
        assert checks[-1] is False
        return original_run(self, *args, **kwargs)
    async def checked_arun(self, *args, **kwargs):
        checks.append(get_tracing_context()["enabled"])
        assert checks[-1] is False
        return await original_arun(self, *args, **kwargs)
    monkeypatch.setattr(StructuredTool, "run", checked_run)
    monkeypatch.setattr(StructuredTool, "arun", checked_arun)
    result = getattr(tool, method)({})
    if method.startswith("a"):
        result = asyncio.run(result)
    assert checks and '"status":"ok"' in result


@pytest.mark.parametrize("method", ["invoke", "run", "ainvoke", "arun"])
def test_real_tools_reject_external_callbacks(claimed_execution, imported, method):
    import asyncio
    from langchain_core.callbacks import BaseCallbackHandler
    from northstar_codereview.evidence_tools import build_snapshot_tools
    snapshot, receipt = imported
    tool = build_snapshot_tools(snapshot, transmission_approval=claimed_execution[0])[0]
    class Sink(BaseCallbackHandler):
        def on_tool_start(self, *args, **kwargs):
            pytest.fail("callback received source-bearing operation")
    options = {"config": {"callbacks": [Sink()]}} if "invoke" in method else {"callbacks": [Sink()]}
    with pytest.raises(ValueError, match="external callbacks"):
        result = getattr(tool, method)({}, **options)
        if method.startswith("a"):
            asyncio.run(result)


@pytest.mark.parametrize("text", ["api_key = 'fictional-sensitive-example'\n", "a" * (1024**2 + 1), "\x00binary"])
def test_constructed_real_snapshot_cannot_skip_content_policy(text):
    from northstar_codereview.local_source import CollectedSnapshot
    from northstar_codereview.real_source_contracts import RealSourceContext
    from northstar_codereview.evidence_tools import build_snapshot_tools
    receipt = approval_for(text=text)
    snapshot = CollectedSnapshot(receipt.manifest.snapshot,
        {("head", p): text for p in SAMPLES[SampleId.PORTFOLIO_RISK][1]}, RealSourceContext(import_approval=receipt))
    with pytest.raises(IntakeRejected):
        build_snapshot_tools(snapshot, transmission_approval=transmission_for(receipt))


@pytest.mark.parametrize("sample", list(SampleId))
def test_each_sample_import_uses_only_generated_local_data(local_intake, sample):
    root, intake = local_intake
    assert root.is_relative_to(intake.PROJECT_ROOT)
    intake._SOURCE_ROOTS[SAMPLES[sample][0]] = root
    for path in SAMPLES[sample][1]:
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"value = 1\r\n")
    receipt = approval_for(sample)
    import_sample(sample, approval=receipt)
    assert load_import(receipt.approval_id).manifest == receipt.manifest.snapshot


def test_destination_cannot_escape_checkout(local_intake, monkeypatch):
    _, intake = local_intake
    monkeypatch.setattr(intake, "_STORE", intake.PROJECT_ROOT.parent / "forbidden-destination")
    monkeypatch.setattr(intake.os, "mkdir", lambda *a, **kw: pytest.fail("attempted outside write"))
    with pytest.raises(IntakeRejected):
        import_sample(SampleId.PORTFOLIO_RISK, approval=approval_for())


def test_long_source_tokens_scan_without_pathological_backtracking():
    from northstar_codereview.real_source_privacy import privacy_check
    from northstar_codereview.output_safety import sensitive_text
    privacy_check("a" * (1024**2))
    assert sensitive_text("https://fictional-user:fictional-password@example.invalid")


@pytest.mark.parametrize("prefix", ["http://a:b/", "http%3A%2F%2Fa%3Ab%2F", "custom://a:b/"])
def test_repeated_url_indicators_rejected_before_credential_scan(monkeypatch, prefix):
    from northstar_codereview import real_source_privacy as privacy
    monkeypatch.setattr(privacy, "sensitive_text", lambda value: pytest.fail("URL reached credential regex"))
    with pytest.raises(IntakeRejected, match="external_url_indicator"):
        privacy.privacy_check(prefix * 4096)


def test_actual_one_mib_file_ceiling(local_intake):
    root, intake = local_intake
    (root / "tests/test_risk.py").write_bytes(b"#" * (1024**2 + 1))
    with pytest.raises(IntakeRejected, match="size_limit"):
        import_sample(SampleId.PORTFOLIO_RISK, approval=approval_for())
    assert not intake._STORE.exists()


@pytest.mark.parametrize("setting", ["get_debug", "get_verbose", "tool_verbose", "call_verbose"])
def test_real_tools_fail_closed_on_debug_printing(claimed_execution, imported, monkeypatch, capsys, setting):
    from northstar_codereview import real_source_authorization as auth
    from northstar_codereview.evidence_tools import build_snapshot_tools
    snapshot, receipt = imported
    tool = build_snapshot_tools(snapshot, transmission_approval=claimed_execution[0])[0]
    options = {}
    if setting.startswith("get_"):
        monkeypatch.setattr(auth, setting, lambda: True)
    elif setting == "tool_verbose":
        tool.verbose = True
    else:
        options["verbose"] = True
    with pytest.raises(ValueError):
        tool.run({}, **options)
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


@pytest.mark.parametrize("kind", ["specialist", "orchestrator"])
def test_debug_is_rejected_before_graph_execution(claimed_execution, imported, monkeypatch, kind):
    import asyncio
    from northstar_codereview import real_source_authorization as auth
    from northstar_codereview.agents.code_quality import CodeQualityAgent
    from northstar_codereview.orchestrator import ReviewOrchestrator
    snapshot, receipt = imported
    permit = claimed_execution[0]
    runner = (CodeQualityAgent(snapshot, model=ReviewModel(), transmission_approval=permit) if kind == "specialist"
              else ReviewOrchestrator(snapshot, model_factory=lambda name: ReviewModel(), transmission_approval=permit))
    monkeypatch.setattr(auth, "get_debug", lambda: True)
    async def forbidden(*a, **kw):
        pytest.fail("graph entered with debug enabled")
    monkeypatch.setattr(runner.graph, "ainvoke", forbidden)
    with pytest.raises(IntakeRejected):
        asyncio.run(runner.run(review_id=claimed_execution[1]))


@pytest.fixture
def claimed_execution(imported, request):
    from northstar_codereview.transmission_claims import execution_scope
    snapshot, receipt = imported
    from northstar_codereview.orchestrator import SPECIALIST_SPECS
    agent_name = getattr(request.node, 'callspec', None)
    agent_name = agent_name.params.get('agent_name', 'CodeQualityAgent') if agent_name else 'CodeQualityAgent'
    specialist = next(spec.name for spec in SPECIALIST_SPECS if spec.agent_type.__name__ == agent_name)
    approval, run_id = transmission_for(receipt, specialists=(specialist,)), uuid4()
    with execution_scope(snapshot, approval, run_id=run_id, identity=("nebius", approval.model), specialists=(specialist,)):
        yield approval, run_id


def test_prepared_reads_recheck_expiry_before_model(claimed_execution, imported, monkeypatch):
    import asyncio
    from northstar_codereview import real_source_authorization as auth
    from northstar_codereview.agents.code_quality import CodeQualityAgent
    snapshot,_=imported
    permit=claimed_execution[0]
    model=ReviewModel()
    agent=CodeQualityAgent(snapshot,model=model,transmission_approval=permit)
    original=agent.tools['read_source_lines'].invoke
    count=[]
    class Later(datetime):
        @classmethod
        def now(cls,tz=None): return permit.expires_at+timedelta(seconds=1)
    def expire_after_read(*args,**kwargs):
        result=original(*args,**kwargs)
        count.append(True)
        monkeypatch.setattr(auth,'datetime',Later)
        return result
    # The wrapper type is shared; delegate unrelated instances unchanged.
    kind=type(agent.tools['read_source_lines'])
    invoke=kind.invoke
    monkeypatch.setattr(kind,'invoke',lambda tool,*a,**kw:expire_after_read(*a,**kw)
                        if tool is agent.tools['read_source_lines'] else invoke(tool,*a,**kw))
    result=asyncio.run(agent.run(review_id=claimed_execution[1]))
    assert count==[True] and not model.histories
    assert result.result.status=='partial' and result.usage.tool_attempts==1
    assert not result.validated_findings
