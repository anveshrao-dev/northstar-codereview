"""Production entry point capacity and claim ordering, with generated intake only."""
import asyncio

from northstar_codereview import real_source as intake
from northstar_codereview.real_source_contracts import SAMPLES
from .test_real_source import local_intake, approval_for
from .test_real_review import service, captured, Fake, ENV, permit


def test_capacity_denial_precedes_provider_and_consumes_claim(service,local_intake):
    root,_=local_intake
    text='value = 1\n'*401
    receipt=approval_for(text=text)
    for path in SAMPLES[receipt.manifest.sample][1]: (root/path).write_text(text)
    intake.import_sample(receipt.manifest.sample,approval=receipt)
    approval=permit(service,receipt)
    starts=[]
    def provider(*args):
        starts.append(True)
        return Fake()
    def execute():
        return asyncio.run(service.run_review(receipt.manifest.sample,receipt.approval_id,
            transmission_id=approval.approval_id,allow_live=True,environ=ENV,provider_factory=provider))
    result=execute()
    assert result.status=='blocked' and result.category=='capacity'
    assert result.capacity.required_tool_attempts==10
    assert result.usage.model_attempts==result.usage.tool_attempts==0 and starts==[]
    assert result.persisted and service.load_review(receipt.approval_id,result.run_id)==result
    assert execute().category=='transmission_consumed' and starts==[]


def test_default_real_path_reports_analyzed_coverage(service,captured):
    model=Fake()
    approval=permit(service,captured)
    result=asyncio.run(service.run_review(captured.manifest.sample,captured.approval_id,
        transmission_id=approval.approval_id,allow_live=True,environ=ENV,
        provider_factory=lambda *a:model))
    assert result.status=='completed' and result.cleanup=='closed'
    d=result.report.specialists[0].diagnostics
    assert d.analysis.analyzed_lines==d.analysis.prepared_lines==2
    assert result.usage.model_attempts==1
    assert service.load_review(captured.approval_id,result.run_id)==result


def test_cancelled_second_unit_retains_first_and_consumed_claim(service,local_intake):
    root,_=local_intake
    text=('#'+'x'*28+'\n')*200
    receipt=approval_for(text=text)
    for path in SAMPLES[receipt.manifest.sample][1]: (root/path).write_text(text)
    intake.import_sample(receipt.manifest.sample,approval=receipt)
    approval=permit(service,receipt)
    class Cancel(Fake):
        async def ainvoke(self,messages,**kwargs):
            if self.histories:
                self.histories.append(messages)
                raise asyncio.CancelledError()
            return await super().ainvoke(messages,**kwargs)
    model=Cancel()
    result=asyncio.run(service.run_review(receipt.manifest.sample,receipt.approval_id,
        transmission_id=approval.approval_id,allow_live=True,environ=ENV,provider_factory=lambda *a:model))
    assert result.status=='partial' and result.category=='cancelled' and model.closed
    assert result.report and len(result.report.findings)==1
    a=result.report.specialists[0].diagnostics.analysis
    assert 0<a.analyzed_lines<a.planned_lines and a.prepared_lines>=a.analyzed_lines
    assert result.usage.model_attempts==2 and result.usage.unknown_usage_attempts==2
    assert service.load_review(receipt.approval_id,result.run_id)==result
    again=asyncio.run(service.run_review(receipt.manifest.sample,receipt.approval_id,
        transmission_id=approval.approval_id,allow_live=True,environ=ENV,provider_factory=lambda *a:None))
    assert again.category=='transmission_consumed'


def test_scope_notes_reach_model_and_persist_even_when_clean(service,captured):
    class Empty(Fake):
        async def ainvoke(self,messages,**kwargs):
            self.histories.append(messages)
            from langchain_core.messages import AIMessage
            return AIMessage(content='{"findings":[]}')
    model=Empty()
    approval=permit(service,captured)
    result=asyncio.run(service.run_review(captured.manifest.sample,captured.approval_id,
        transmission_id=approval.approval_id,allow_live=True,environ=ENV,provider_factory=lambda *a:model))
    assert result.status=='completed'
    assert service.SCOPE_NOTES[captured.manifest.sample] in model.histories[0][0].content
    assert any('Cross-unit' in note for note in result.report.limitations)


def test_ui_separates_prepared_and_analyzed_coverage(service,captured,monkeypatch):
    from .test_real_review_ui import app,check,click
    from langchain_core.messages import AIMessage
    class Invalid(Fake):
        async def ainvoke(self,*a,**kw): return AIMessage(content='{}')
    original=service.run_review
    async def run(*a,**kw): return await original(*a,**kw,environ=ENV,provider_factory=lambda *a:Invalid())
    monkeypatch.setattr(service,'run_review',run)
    at=app()
    check(at,'I authorize disclosure of this exact snapshot to Nebius for code review.')
    click(at,'Authorize model transmission')
    check(at,'I authorize one bounded live review now.')
    click(at,'Start live real-code review')
    assert not at.exception
    assert next(m.value for m in at.metric if m.label=='Analyzed coverage')=='0/2 lines'
    assert any('Prepared: 2' in t.value and 'Validated model analysis: 0/2' in t.value for t in at.text)
    assert any('Incomplete review' in w.value for w in at.warning)


def test_ui_capacity_denial_has_no_provider_start(service,local_intake,monkeypatch):
    from .test_real_review_ui import app,check,click
    root,_=local_intake
    text='value = 1\n'*401
    receipt=approval_for(text=text)
    for path in SAMPLES[receipt.manifest.sample][1]: (root/path).write_text(text)
    intake.import_sample(receipt.manifest.sample,approval=receipt)
    original=service.run_review
    starts=[]
    def provider(*args): starts.append(True); return Fake()
    async def run(*a,**kw): return await original(*a,**kw,environ=ENV,provider_factory=provider)
    monkeypatch.setattr(service,'run_review',run)
    at=app()
    check(at,'I authorize disclosure of this exact snapshot to Nebius for code review.')
    click(at,'Authorize model transmission')
    check(at,'I authorize one bounded live review now.')
    click(at,'Start live real-code review')
    assert not at.exception and starts==[]
    assert any('10 source-tool attempts required / 6 available' in w.value for w in at.warning)
    at.run()
    assert starts==[]
