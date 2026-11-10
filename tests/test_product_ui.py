"""Product navigation and history, using generated private records only."""
import asyncio
import pytest
from streamlit.testing.v1 import AppTest
from northstar_codereview.local_source import PROJECT_ROOT
from .test_real_source import local_intake
from .test_real_review import service, captured, Fake, ENV, permit


@pytest.fixture(autouse=True)
def isolated(local_intake): pass


def screen(): return AppTest.from_file(PROJECT_ROOT/'streamlit_app.py',default_timeout=30).run()
def navigate(at,page): return at.button(key='nav:'+page).click().run()
def click(at,label): return next(b for b in at.button if b.label==label).click().run()


def test_overview_and_all_destinations_never_start_inference(service,monkeypatch):
    monkeypatch.setattr(service,'run_review',lambda *a,**k:pytest.fail('inference started'))
    monkeypatch.setattr(service,'authorize_transmission',lambda *a,**k:pytest.fail('approval created'))
    at=screen()
    assert [b.label for b in at.sidebar.button]==['Overview','New Review','Review History','Test Lab','Settings']
    assert any(b.label=='New review' for b in at.button)
    assert any('No available retained reviews' in x.value for x in at.info)
    for page in [b.label for b in at.sidebar.button]:
        navigate(at,page)
        assert not at.exception and not at.error
    assert not at.code


def test_history_selects_generated_record_without_uuid_input(service,captured,monkeypatch):
    approval=permit(service,captured)
    result=asyncio.run(service.run_review('portfolio_risk',captured.approval_id,
        transmission_id=approval.approval_id,allow_live=True,environ=ENV,provider_factory=lambda *a:Fake()))
    monkeypatch.setattr(service,'run_review',lambda *a,**k:pytest.fail('inference repeated'))
    at=screen()
    navigate(at,'Review History')
    assert any(b.label=='Open review' for b in at.button)
    assert not any(t.label=='Real review run ID' for t in at.text_input)
    click(at,'Open review')
    assert not at.exception and not at.error
    assert any('Claim: unverified' in t.value for t in at.text)
    assert at.get('download_button') and not at.code
    assert not any('Security'==m.value or 'GitOps'==m.value for m in at.markdown)
    assert any('Run ID:' in t.value for t in at.text)
    at.run()
    assert not at.exception


def test_history_catalog_rejects_tampered_and_symlink_records(service,captured):
    from northstar_codereview.review_history import retained_reviews
    from northstar_codereview import real_source as intake
    approval=permit(service,captured)
    result=asyncio.run(service.run_review('portfolio_risk',captured.approval_id,
        transmission_id=approval.approval_id,allow_live=True,environ=ENV,provider_factory=lambda *a:Fake()))
    assert len(retained_reviews())==1
    folder=intake._STORE/'imports'/str(captured.approval_id)/'review_artifacts'
    path=folder/f'{result.run_id}.json'
    original=path.read_bytes(); path.write_bytes(original.replace(b'completed',b'partial'))
    assert retained_reviews()==()
    path.unlink();path.symlink_to(folder/'missing')
    assert retained_reviews()==()


def test_primary_new_review_action_and_supported_scope(captured):
    at=screen()
    click(at,'New review')
    assert at.session_state['navigation']=='New Review'
    profile=next(s for s in at.selectbox if s.label=='Specialist profile')
    assert profile.options==['Code Quality'] and profile.disabled
    assert next(b for b in at.button if b.label=='Authorize model transmission').disabled
    assert next(b for b in at.button if b.label=='Start live real-code review').disabled
    assert any('External disclosure' in i.value for i in at.info)
    assert all(not e.proto.expanded for e in at.expander)


def test_valid_zero_finding_report_remains_completed_and_unscored(service,captured,monkeypatch):
    from langchain_core.messages import AIMessage
    class Empty(Fake):
        async def ainvoke(self,messages,**kw):
            await super().ainvoke(messages,**kw)
            return AIMessage(content='{"findings":[]}',response_metadata={'finish_reason':'stop'})
    approval=permit(service,captured)
    result=asyncio.run(service.run_review('portfolio_risk',captured.approval_id,
        transmission_id=approval.approval_id,allow_live=True,environ=ENV,provider_factory=lambda *a:Empty()))
    at=screen();navigate(at,'Review History');click(at,'Open review')
    assert not at.exception and not at.error
    assert next(m for m in at.metric if m.label=='Review status').value=='Completed'
    assert next(m for m in at.metric if m.label=='Evidence-validated findings').value=='0'
    assert any('No evidence-validated findings' in i.value for i in at.info)
    assert not any('Incomplete review' in w.value for w in at.warning)
    assert not any('clean'==i.value.lower() for i in at.info)
    assert not at.code


def test_exact_source_is_only_rendered_on_explicit_evidence_request(service,captured,monkeypatch):
    approval=permit(service,captured)
    result=asyncio.run(service.run_review('portfolio_risk',captured.approval_id,
        transmission_id=approval.approval_id,allow_live=True,environ=ENV,provider_factory=lambda *a:Fake()))
    original=service.evidence_excerpt;reads=[]
    def observed(*args): reads.append(True);return original(*args)
    monkeypatch.setattr(service,'evidence_excerpt',observed)
    at=screen();navigate(at,'Review History');click(at,'Open review')
    assert not reads and not at.code
    next(b for b in at.button if b.label.startswith('Show evidence:')).click().run()
    assert reads==[True] and len(at.code)==1
    assert at.code[0].value=='1 | value = 1\r' # Absolute source line; original CR is preserved.
    at.run()
    assert reads==[True] and not at.code


def test_expired_history_is_withheld_and_never_renewed(service,captured,monkeypatch):
    from northstar_codereview import real_source_contracts as contracts
    from northstar_codereview.review_history import retained_reviews
    from datetime import datetime,timedelta
    approval=permit(service,captured)
    asyncio.run(service.run_review('portfolio_risk',captured.approval_id,
        transmission_id=approval.approval_id,allow_live=True,environ=ENV,provider_factory=lambda *a:Fake()))
    assert len(retained_reviews())==1
    class Later(datetime):
        @classmethod
        def now(cls,tz=None):return captured.manifest.retain_until+timedelta(seconds=1)
    monkeypatch.setattr(contracts,'datetime',Later)
    assert retained_reviews()==()
    at=screen();navigate(at,'Review History')
    assert not at.get('download_button') and not at.metric


def test_history_ignores_unapproved_files_and_preserves_private_records(service,captured):
    from northstar_codereview.review_history import retained_reviews
    from northstar_codereview import real_source as intake
    folder=intake._STORE/'imports'/str(captured.approval_id)/'review_artifacts'
    (folder/'PRIVATE_SECRET.txt').write_text('do not read')
    before={p.relative_to(intake._STORE):p.read_bytes() for p in intake._STORE.rglob('*') if p.is_file()}
    assert retained_reviews()==()
    after={p.relative_to(intake._STORE):p.read_bytes() for p in intake._STORE.rglob('*') if p.is_file()}
    assert before==after


def test_navigation_clears_unsubmitted_disclosure_checkboxes(captured):
    from .test_real_review_ui import check
    at=screen();navigate(at,'New Review')
    check(at,'I authorize disclosure of this exact snapshot to Nebius for code review.')
    navigate(at,'Test Lab');navigate(at,'New Review')
    assert not next(c for c in at.checkbox if c.label.startswith('I authorize disclosure')).value
    assert next(b for b in at.button if b.label=='Start live real-code review').disabled


def test_history_navigation_opens_requested_run_after_previous_selection(service,captured):
    from northstar_codereview.review_history import retained_reviews
    results=[]
    for _ in range(2):
        approval=permit(service,captured)
        results.append(asyncio.run(service.run_review('portfolio_risk',captured.approval_id,
            transmission_id=approval.approval_id,allow_live=True,environ=ENV,provider_factory=lambda *a:Fake())))
    records=retained_reviews()
    at=screen();navigate(at,'Review History')
    # Open the newest result first, then navigate directly to the older run.
    click(at,'Open review')
    navigate(at,'New Review')
    at.session_state[f'real:{captured.approval_id}:{captured.manifest.digest}:result']=records[1].review
    at.run();click(at,'Open in Review History')
    assert not at.exception and not at.error
    assert any(t.value=='Run ID: '+str(records[1].review.run_id) for t in at.text)
    assert not any(t.value=='Run ID: '+str(records[0].review.run_id) for t in at.text)


def test_history_directory_scan_stops_at_bound(service,captured,monkeypatch):
    from northstar_codereview import review_history
    from contextlib import contextmanager
    from types import SimpleNamespace
    consumed=[]
    @contextmanager
    def scan(folder):
        def entries():
            for n in range(5000):
                consumed.append(n)
                yield SimpleNamespace(name='not-a-record')
        yield entries()
    monkeypatch.setattr(review_history.os,'scandir',scan)
    assert review_history.retained_reviews()==()
    assert len(consumed)==4097


def test_disappearing_selected_history_record_cannot_keep_export_visible(service,captured):
    from northstar_codereview import real_source as intake
    from northstar_codereview.review_history import retained_reviews
    for _ in range(2):
        approval=permit(service,captured)
        asyncio.run(service.run_review('portfolio_risk',captured.approval_id,
            transmission_id=approval.approval_id,allow_live=True,environ=ENV,provider_factory=lambda *a:Fake()))
    record=retained_reviews()[0].review
    at=screen();navigate(at,'Review History');click(at,'Open review')
    assert at.get('download_button')
    path=intake._STORE/'imports'/str(captured.approval_id)/'review_artifacts'/f'{record.run_id}.json'
    path.unlink()  # Generated test record only.
    at.run()
    assert not at.exception and not at.error and not at.get('download_button')
    assert not any(t.value=='Run ID: '+str(record.run_id) for t in at.text)
