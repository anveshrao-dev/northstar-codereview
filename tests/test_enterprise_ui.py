"""Native UI behavior with generated private records; no model/network side effects."""
import asyncio
import pytest
from streamlit.testing.v1 import AppTest
from northstar_codereview.local_source import PROJECT_ROOT
from .test_real_source import local_intake
from .test_real_review import service, captured, Fake, ENV, permit


@pytest.fixture(autouse=True)
def isolated(local_intake):pass


def screen():return AppTest.from_file(PROJECT_ROOT/'streamlit_app.py',default_timeout=30).run()


def test_four_steps_and_five_specialists_preserve_consent(service,captured,monkeypatch):
    monkeypatch.setattr(service,'run_review',lambda *a,**k:pytest.fail('unexpected execution'))
    at=screen();at.button(key='nav:New Review').click().run()
    assert not at.exception
    headings=[s.value for s in at.subheader]
    assert '2. Select reviewer' in headings
    assert '4. Run review' in headings
    assert next(s for s in at.selectbox if s.label=='Execution profile').value=='standard'
    assert next(b for b in at.button if b.label=='Authorize model transmission').disabled
    assert next(b for b in at.button if b.label=='Start live real-code review').disabled
    assert any('AI Code Review Platform' in c.value for c in at.sidebar.caption)
    assert sum('Unsupported in live source workflow' in t.value for t in at.text)==4


def test_saved_pipeline_and_profile_do_not_start_inference(service,captured,monkeypatch):
    approval=permit(service,captured)
    result=asyncio.run(service.run_review('portfolio_risk',captured.approval_id,transmission_id=approval.approval_id,
        allow_live=True,environ=ENV,provider_factory=lambda *a:Fake()))
    monkeypatch.setattr(service,'run_review',lambda *a,**k:pytest.fail('unexpected execution'))
    at=screen();at.button(key='nav:Review History').click().run()
    assert any('Validated analyzed coverage:' in c.value for c in at.caption)
    next(b for b in at.button if b.label=='Open review').click().run()
    assert not at.exception and not at.error
    assert any(s.value=='Review pipeline' for s in at.subheader)
    assert len(next(s for s in at.selectbox if s.label=='Select pipeline stage').options)==7
    assert any('Not a live event stream' in c.value for c in at.caption)
    assert any('Prepared coverage:' in c.value for c in at.caption)
    assert sum('· Not selected' in t.value for t in at.text)==4
    assert at.get('download_button')


def test_settings_profiles_are_read_only(service,monkeypatch):
    monkeypatch.setattr(service,'run_review',lambda *a,**k:pytest.fail('unexpected execution'))
    at=screen();at.button(key='nav:Settings').click().run()
    assert not at.exception
    assert any(s.value=='Execution profiles' for s in at.subheader)
    for name in ('standard','extended_csv','extended_bm25'):
        assert any(name in t.value for t in at.text)
    assert not any(b.label=='Save configuration' for b in at.button)


def test_mixed_legacy_and_current_catalog_keeps_all_eight_records(service,captured,monkeypatch):
    import json
    from langchain_core.messages import AIMessage
    from northstar_codereview import real_source as intake
    from northstar_codereview.reporting import _digest,ReviewReport
    from northstar_codereview.evaluation import fingerprint
    class Output(Fake):
        def __init__(self,text):super().__init__();self.text=text
        async def ainvoke(self,messages,**kwargs):
            return AIMessage(content=self.text,response_metadata={'finish_reason':'stop'})
    results=[]
    for i in range(8):
        profile='standard' if i<6 else 'extended_csv'
        approval=service.authorize_transmission('portfolio_risk',captured.approval_id,
            expected_digest=captured.manifest.digest,allow_transmission=True,execution_profile=profile)
        text='{"findings":[]}' if i in (0,7) else '{}' if i<6 else '{"findings":[],"outcome":"insufficient_evidence"}'
        result=asyncio.run(service.run_review('portfolio_risk',captured.approval_id,transmission_id=approval.approval_id,
            allow_live=True,environ=ENV,execution_profile=profile,provider_factory=lambda *a:Output(text)))
        if i<6:
            # Generated fixture only: characterize compatible pre-profile/pre-chunk records.
            body=result.model_dump(mode='json');body.pop('execution_profile')
            report=body['report'];report.pop('report_id')
            for row in report['specialists']:
                if row['diagnostics']:row['diagnostics'].pop('analysis',None)
            report['report_id']=_digest(report)
            ReviewReport.model_validate(report)
            path=intake._STORE/'imports'/str(captured.approval_id)/'review_artifacts'/f'{result.run_id}.json'
            path.write_text(json.dumps({'digest':fingerprint(body),'result':body}))
        results.append(result)
    monkeypatch.setattr(service,'run_review',lambda *a,**k:pytest.fail('unexpected execution'))
    at=screen()
    assert not at.exception and {m.label:m.value for m in at.metric}=={'Total reviews':'8','Completed':'2','Need attention':'6'}
    at.button(key='nav:Review History').click().run()
    assert len([b for b in at.button if b.label=='Open review'])==8
    at.button(key=f'open:{captured.approval_id}:{results[-1].run_id}').click().run()
    assert not at.exception and not at.error
    assert any(m.label=='Review status' and m.value=='Completed' for m in at.metric)
    assert any(m.label=='Analyzed coverage' and m.value=='2/2 lines' for m in at.metric)


def test_resumed_report_checkpoint_drives_pipeline_and_export(service,captured,monkeypatch,tmp_path):
    from uuid import uuid4
    import json
    from northstar_codereview import approval_store,streamlit_ui,real_review_ui
    from northstar_codereview.ui_service import LocalReview
    monkeypatch.setattr(approval_store,'PROJECT_ROOT',tmp_path)
    local=LocalReview(database=tmp_path/'.runtime/approval/reviews.sqlite3')
    monkeypatch.setattr(streamlit_ui,'LocalReview',lambda:local)
    monkeypatch.setattr(real_review_ui,'LocalReview',lambda:local)
    approval=permit(service,captured)
    result=asyncio.run(service.run_review('portfolio_risk',captured.approval_id,transmission_id=approval.approval_id,
        allow_live=True,environ=ENV,provider_factory=lambda *a:Fake()))
    custom=uuid4();local.workflow.start(result.report,thread_id=custom)
    exports=[];original=real_review_ui.st.download_button
    def capture(label,*args,**kwargs):
        if label=='Export sanitized prioritized report':exports.append(json.loads(kwargs['data']))
        return original(label,*args,**kwargs)
    monkeypatch.setattr(real_review_ui.st,'download_button',capture)
    at=screen();at.button(key='nav:Review History').click().run()
    next(b for b in at.button if b.label=='Open review').click().run()
    next(t for t in at.text_input if t.label=='Checkpoint ID').set_value(str(custom)).run()
    next(b for b in at.button if b.label=='Load existing checkpoint').click().run()
    assert not at.exception and not at.error
    next(s for s in at.selectbox if s.label=='Select pipeline stage').set_value('human').run()
    assert any(t.value=='Human review · Awaiting human review' for t in at.text)
    next(s for s in at.selectbox if s.label=='Local report decision').set_value('reject').run()
    next(b for b in at.button if b.label=='Record manual decision').click().run()
    assert any(t.value=='Human review · Completed' for t in at.text)
    assert exports[-1]['human_decision']['thread_id']==str(custom)
    assert exports[-1]['human_decision']['decision']=='reject'
    assert exports[-1]['publication_authorized'] is False


def test_missing_report_does_not_invent_specialist_execution():
    def render():
        from northstar_codereview.ui_style import specialist_cards
        specialist_cards()
    at=AppTest.from_function(render).run()
    assert not at.exception
    assert sum('Execution unavailable' in t.value for t in at.text)==5
    assert not any('Did not execute' in t.value for t in at.text)
