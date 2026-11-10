"""Real graph, generated snapshots, fake model transport; no original sources."""
import asyncio
import importlib
import json
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from northstar_codereview import real_source as intake
from northstar_codereview.real_source_contracts import IntakeRejected, SampleId
from northstar_codereview.ui_service import LocalReview
from .test_real_source import local_intake, approval_for, ReviewModel

ENV = {'NEBIUS_API_KEY': 'synthetic-test-secret', 'NEBIUS_MODEL': 'Qwen/Qwen3-30B-A3B-Instruct-2507'}


@pytest.fixture
def service():
    assert importlib.util.find_spec('northstar_codereview.real_review'), 'real review workflow missing'
    return importlib.import_module('northstar_codereview.real_review')


@pytest.fixture
def captured(local_intake):
    receipt = approval_for()
    intake.import_sample(receipt.manifest.sample, approval=receipt)
    return receipt


class Fake(ReviewModel):
    async def __aexit__(self, *args): self.closed = True

    async def ainvoke(self, messages, **kwargs):
        if any('PREPARED_SOURCE_DATA=' in str(m.content) for m in messages):
            from .test_prepared_review import PreparedModel
            self.histories.append(list(messages))
            return await PreparedModel('valid').ainvoke(messages,**kwargs)
        if not self.histories:
            return await super().ainvoke(messages, **kwargs)
        self.histories.append(list(messages))
        if self.tools:
            return AIMessage(content='', tool_calls=[{'name': 'get_source_metadata',
                'id': 'metadata-' + str(len(self.histories)),
                'args': {'file_path': 'src/stock_portfolio_analyzer/risk.py'}}])
        assert kwargs['synthesis_json'] is True
        observations = [m for m in messages if isinstance(m, ToolMessage)]
        assert observations[0].tool_call_id == 'read-0' and observations[1].tool_call_id == 'read-1'
        proposal = dict(title='Synthetic workflow proposal', category='correctness', severity='low', confidence=0.5,
            explanation='A fictional contract test claim; requires human judgment.',
            suggested_remediation='Confirm behavior before changing code.', evidence=json.loads(observations[0].content)['evidence'])
        return AIMessage(content=json.dumps({'findings': [proposal]}))


def permit(service, receipt):
    return service.authorize_transmission(receipt.manifest.sample, receipt.approval_id,
        expected_digest=receipt.manifest.digest, allow_transmission=True)


def execute(service, receipt, model, **kw):
    # Historical model-directed characterization; product defaults are tested
    # separately through prepared service/UI/CLI executions.
    kw.setdefault('prepared_context',False)
    approval = permit(service, receipt)
    return asyncio.run(service.run_review(receipt.manifest.sample, receipt.approval_id,
        transmission_id=approval.approval_id, allow_live=True, environ=ENV,
        provider_factory=lambda settings, timeout: model, **kw))


def test_complete_graph_report_persistence_human_decision_export(service, captured, tmp_path, monkeypatch):
    model = Fake()
    result = execute(service, captured, model)
    assert result.status == 'completed' and result.report
    assert result.usage.model_attempts == 4 and result.usage.tool_attempts == 4
    assert len(result.report.findings) == 1 and model.closed
    assert model.bindings[-1] == ()
    assert result.cost is None and result.report.findings[0].claim_status == 'unverified'
    assert result.transmission_id is not None
    assert result.manifest_digest == captured.manifest.digest
    assert result.limits == service.profile(captured.manifest.sample)
    assert result.synthesis_input_hold == 30000
    loaded = service.load_review(captured.approval_id, result.run_id)
    assert loaded == result
    assert 'value = 1' not in result.model_dump_json()
    assert service.evidence_excerpt(captured.approval_id, result.run_id, result.report.findings[0].finding_id, 0) == 'value = 1\r\n'
    from northstar_codereview import approval_store
    monkeypatch.setattr(approval_store, 'PROJECT_ROOT', tmp_path)
    local = LocalReview(database=tmp_path / '.runtime/decisions.sqlite3')
    view = local.start(result.report)
    decided = local.submit(view.thread_id, result.report, 'reject')
    exported = service.export_review(captured.approval_id, result.run_id, decision=decided.outcome)
    assert json.loads(exported)['human_decision']['decision'] == 'reject'
    assert 'value = 1' not in exported
    assert 'synthetic-test-secret' not in exported


def test_catalog_is_metadata_only_and_never_authorizes(service, captured, monkeypatch):
    monkeypatch.setattr(service, 'load_import', lambda *a: pytest.fail('source loaded'))
    rows = service.snapshot_catalog(captured.manifest.sample)
    assert len(rows) == 1 and rows[0].import_id == captured.approval_id
    assert rows[0].snapshot_id == captured.manifest.snapshot.snapshot_id
    assert not list((intake._STORE / 'imports' / str(captured.approval_id) / 'receipts').glob('transmission-*'))


def test_disabled_before_any_credential_or_snapshot_read(service, monkeypatch):
    monkeypatch.setattr(service, 'load_import', lambda *a: pytest.fail('source read'))
    monkeypatch.setattr(service, 'load_nebius_settings', lambda *a: pytest.fail('credential read'))
    output = asyncio.run(service.run_review('portfolio_risk', uuid4()))
    assert output.status == 'blocked' and output.category == 'live_opt_in_required'


def test_missing_transmission_never_constructs_provider(service, captured):
    result = asyncio.run(service.run_review(captured.manifest.sample, captured.approval_id, allow_live=True,
        provider_factory=lambda *a: pytest.fail('provider constructed')))
    assert result.status == 'blocked' and result.usage.model_attempts == 0


def test_transmission_requires_explicit_current_binding(service, captured):
    for options in ({}, {'allow_transmission': True, 'expected_digest': '0'*64}):
        options.setdefault('expected_digest', captured.manifest.digest)
        with pytest.raises(IntakeRejected):
            service.authorize_transmission(captured.manifest.sample, captured.approval_id, **options)


def test_wrong_sample_rejected(service, captured):
    with pytest.raises(IntakeRejected):
        service.authorize_transmission('csv_import', captured.approval_id,
            expected_digest=captured.manifest.digest, allow_transmission=True)


def test_wrong_provider_identity_is_blocked_and_closed(service, captured):
    model = Fake()
    model.transmission_identity = ('different', ENV['NEBIUS_MODEL'])
    result = execute(service, captured, model)
    assert result.status == 'blocked' and model.closed and not model.histories


def test_failed_response_no_retry_and_no_raw_leak(service, captured):
    class Bad(Fake):
        async def ainvoke(self, *args, **kwargs):
            self.histories.append(True)
            raise RuntimeError('PRIVATE_PROVIDER_RESPONSE')
    model = Bad()
    result = execute(service, captured, model)
    assert result.status == 'partial' and result.usage.model_attempts == 1
    assert len(model.histories) == 1 and model.closed
    assert 'PRIVATE_PROVIDER_RESPONSE' not in result.model_dump_json()


@pytest.mark.parametrize('content', ['not JSON PRIVATE_TEXT', '{}', '{"findings":[],"outcome":"insufficient_evidence"}'])
def test_malformed_or_incomplete_synthesis_never_clean(service, captured, content):
    class Final(Fake):
        async def ainvoke(self, messages, **kwargs):
            if not self.histories: return await super().ainvoke(messages, **kwargs)
            self.histories.append(messages)
            return AIMessage(content=content)
    result = execute(service, captured, Final())
    assert result.status == 'partial' and not result.report.findings
    assert 'PRIVATE_TEXT' not in result.model_dump_json()


def test_duplicate_run_id_cannot_repeat_inference(service, captured):
    approval = permit(service, captured)
    model = Fake()
    run_id = uuid4()
    args = dict(transmission_id=approval.approval_id, allow_live=True, environ=ENV, run_id=run_id,
                provider_factory=lambda *a: model)
    first = asyncio.run(service.run_review(captured.manifest.sample, captured.approval_id, **args))
    second = asyncio.run(service.run_review(captured.manifest.sample, captured.approval_id, **args))
    assert first.status == 'completed' and second.category == 'run_already_claimed'
    assert len(model.histories) == 1


@pytest.mark.parametrize('sample', list(SampleId))
def test_profiles_never_increase_existing_global_or_individual_limits(service, sample):
    limits = service.profile(sample)
    assert limits.max_model_requests == 4 and limits.max_tool_attempts == 6
    assert limits.max_input_tokens == 80000 and limits.max_output_tokens == 4000
    assert limits.output_tokens_per_call == 1000
    assert limits.request_timeout_seconds == 20 and limits.evaluation_timeout_seconds == 60
    with pytest.raises(ValueError): service.profile('../arbitrary')


@pytest.mark.parametrize('sample', list(SampleId))
def test_every_registered_sample_runs_actual_graph_on_generated_source(service, local_intake, sample, monkeypatch):
    from northstar_codereview.real_source_contracts import SAMPLES
    root, _ = local_intake
    for path in SAMPLES[sample][1]:
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b'value = 1\r\n')
    monkeypatch.setattr(intake, '_SOURCE_ROOTS', {SAMPLES[sample][0]: root})
    receipt = approval_for(sample)
    intake.import_sample(sample, approval=receipt)
    class AllFiles(Fake):
        async def ainvoke(self, messages, **kwargs):
            self.histories.append(list(messages))
            if len(self.histories) == 1:
                return AIMessage(content='', tool_calls=[{'name':'read_source_lines', 'id':str(i),
                    'args':{'file_path':p, 'start_line':1, 'end_line':1}} for i,p in enumerate(SAMPLES[sample][1])])
            if self.tools:
                return AIMessage(content='', tool_calls=[{'name':'get_source_metadata', 'id':'meta'+str(len(self.histories)),
                    'args':{'file_path':SAMPLES[sample][1][0]}}])
            return AIMessage(content='{"findings":[]}')
    result = execute(service, receipt, AllFiles())
    assert result.status == 'completed' and not result.report.findings
    assert result.usage.model_attempts <= 4 and result.usage.tool_attempts <= 6
    assert set(result.report.specialists[0].reviewed_files) == set(SAMPLES[sample][1])


def test_real_adapter_wire_format_and_full_replay(service, captured):
    import httpx
    from northstar_codereview.providers.nebius import NebiusModel
    from northstar_codereview.real_source_contracts import SAMPLES
    from .test_nebius import completion
    requests = []
    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            calls = [{'id':str(i), 'type':'function', 'function':{'name':'read_source_lines',
                'arguments':json.dumps({'file_path':p,'start_line':1,'end_line':1})}} for i,p in enumerate(SAMPLES[captured.manifest.sample][1])]
        elif len(requests) < 4:
            calls = [{'id':'m'+str(len(requests)), 'type':'function','function':{'name':'get_source_metadata',
                'arguments':json.dumps({'file_path':SAMPLES[captured.manifest.sample][1][0]})}}]
        else:
            return httpx.Response(200,json=completion({'content':'{"findings":[]}'}))
        return httpx.Response(200,json=completion({'content':None,'tool_calls':calls}))
    approval = permit(service, captured)
    output = asyncio.run(service.run_review(captured.manifest.sample,captured.approval_id,allow_live=True,
        transmission_id=approval.approval_id,environ=ENV,prepared_context=False,provider_factory=lambda settings,timeout:
        NebiusModel(settings,allow_live=True,timeout_seconds=timeout,transport=httpx.MockTransport(respond))))
    assert output.status == 'completed' and len(requests) == 4
    assert all('response_format' not in r and len(r['tools'])==4 for r in requests[:-1])
    assert requests[-1]['response_format']=={'type':'json_object'} and 'tools' not in requests[-1]
    calls = [c['id'] for m in requests[-1]['messages'] if m['role']=='assistant' for c in m.get('tool_calls',[])]
    observations = [m['tool_call_id'] for m in requests[-1]['messages'] if m['role']=='tool']
    assert calls == observations


def test_cancellation_accounts_attempt_and_closes_provider(service, captured):
    approval = permit(service,captured)
    async def cancel_running():
        started = asyncio.Event()
        class Cancelled(Fake):
            async def ainvoke(self,*a,**k):
                started.set()
                await asyncio.Event().wait()
        model = Cancelled()
        task = asyncio.create_task(service.run_review(captured.manifest.sample,captured.approval_id,
            allow_live=True,transmission_id=approval.approval_id,environ=ENV,provider_factory=lambda *a:model))
        await asyncio.wait_for(started.wait(),timeout=3)
        task.cancel()
        return await task, model
    result, model = asyncio.run(cancel_running())
    assert result.status == 'partial' and result.category == 'cancelled'
    assert result.usage.model_attempts == result.usage.unknown_usage_attempts == 1
    assert model.closed and result.persisted


def test_debug_blocks_before_source_or_provider(service, captured, monkeypatch):
    from northstar_codereview import real_source_authorization as auth
    monkeypatch.setattr(auth,'get_debug',lambda:True)
    result = asyncio.run(service.run_review(captured.manifest.sample,captured.approval_id,allow_live=True))
    assert result.category == 'authorization' and result.usage.model_attempts == 0


def test_wrong_environment_model_never_constructs_provider(service,captured):
    approval = permit(service,captured)
    result = asyncio.run(service.run_review(captured.manifest.sample,captured.approval_id,allow_live=True,
        transmission_id=approval.approval_id,environ={**ENV,'NEBIUS_MODEL':'other'},
        provider_factory=lambda *a:pytest.fail('provider started')))
    assert result.category == 'configuration' and result.status == 'blocked'


def test_expired_transmission_blocks_before_provider(service,captured,monkeypatch):
    from northstar_codereview import real_source_authorization as auth
    from datetime import datetime,timedelta
    approval=permit(service,captured)
    class Later(datetime):
        @classmethod
        def now(cls,tz=None):return approval.expires_at+timedelta(seconds=1)
    monkeypatch.setattr(auth,'datetime',Later)
    result=asyncio.run(service.run_review(captured.manifest.sample,captured.approval_id,allow_live=True,
        transmission_id=approval.approval_id,provider_factory=lambda *a:pytest.fail('provider started')))
    assert result.category=='authorization' and result.usage.model_attempts==0


def test_fabricated_evidence_is_not_retained(service,captured):
    class Fabricated(Fake):
        async def ainvoke(self,messages,**kwargs):
            response=await super().ainvoke(messages,**kwargs)
            if not self.tools:
                value=json.loads(response.content)
                value['findings'][0]['evidence'][0]['snippet']='PRIVATE_FABRICATION'
                response=AIMessage(content=json.dumps(value))
            return response
    result=execute(service,captured,Fabricated())
    assert result.status=='partial' and not result.report.findings
    assert 'exact_source_mismatch' in str(result.report.limitations)
    assert 'PRIVATE_FABRICATION' not in result.model_dump_json()


def test_report_tamper_and_symlink_rejected(service,captured):
    result=execute(service,captured,Fake())
    path=intake._STORE/'imports'/str(captured.approval_id)/'review_artifacts'/f'{result.run_id}.json'
    raw=path.read_bytes()
    path.write_bytes(raw.replace(b'completed',b'partial'))
    with pytest.raises(IntakeRejected):service.load_review(captured.approval_id,result.run_id)
    path.unlink()
    path.symlink_to(path.parent/'missing')
    with pytest.raises((OSError,IntakeRejected)):service.load_review(captured.approval_id,result.run_id)


def test_excerpt_uses_physical_lf_lines(service,local_intake,monkeypatch):
    from northstar_codereview.real_source_contracts import SAMPLES
    root,_=local_intake
    text='value = "first\u2028second"\r\n'
    for p in SAMPLES[SampleId.PORTFOLIO_RISK][1]: (root/p).write_bytes(text.encode())
    receipt=approval_for(text=text)
    intake.import_sample(receipt.manifest.sample,approval=receipt)
    result=execute(service,receipt,Fake())
    assert result.report and result.report.findings
    assert service.evidence_excerpt(receipt.approval_id,result.run_id,result.report.findings[0].finding_id,0)==text


def test_default_service_prepares_source_and_exports_numeric_diagnostics(service,captured):
    model=Fake()
    approval=permit(service,captured)
    result=asyncio.run(service.run_review(captured.manifest.sample,captured.approval_id,
        transmission_id=approval.approval_id,allow_live=True,environ=ENV,provider_factory=lambda *a:model))
    assert result.status=='completed'
    assert result.usage.model_attempts==1 and result.usage.tool_attempts==2
    diagnostic=next(s.diagnostics for s in result.report.specialists if s.selected)
    assert diagnostic.preparation.read_attempts==2
    assert diagnostic.preparation.proposal_count==1
    assert diagnostic.fully_inspected_files==2
    assert diagnostic.synthesis_context.admission=='admitted'
    assert diagnostic.synthesis_context.after.projected_units<=30000
    assert result.report.findings[0].claim_status=='unverified'
    assert service.load_review(captured.approval_id,result.run_id)==result
    exported=service.export_review(captured.approval_id,result.run_id)
    assert '"preparation"' in exported and 'value = 1' not in exported
    assert 'synthetic-test-secret' not in exported
