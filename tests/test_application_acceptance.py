"""Generated imports -> real graph/SQLite/UI/export. No live provider or source execution."""
import json
import stat

import pytest
from langchain_core.messages import AIMessage

from northstar_codereview import real_source as intake, transmission_claims
from northstar_codereview.ui_service import LocalReview
from northstar_codereview.real_source_contracts import IntakeRejected
from .test_real_review import service, captured, Fake, ENV
from .test_real_source import local_intake
from .test_real_review_ui import app, click, check, button


@pytest.mark.parametrize('malformed',[False,True])
def test_complete_application_consent_claim_review_decision_export(service,captured,monkeypatch,tmp_path,malformed):
    from northstar_codereview import streamlit_ui, real_review_ui, approval_store
    monkeypatch.setattr(approval_store,"PROJECT_ROOT",tmp_path)
    local=LocalReview(database=tmp_path/'.runtime/approval/reviews.sqlite3')
    monkeypatch.setattr(streamlit_ui,'LocalReview',lambda:local)
    monkeypatch.setattr(real_review_ui,'LocalReview',lambda:local)
    outputs=[];exports=[]
    class Provider(Fake):
        async def ainvoke(self,messages,**kwargs):
            response=await super().ainvoke(messages,**kwargs)
            return AIMessage(content='{}',response_metadata={'finish_reason':'stop'}) if malformed else response
    original=service.run_review
    async def controlled(*args,**kwargs):
        result=await original(*args,**kwargs,environ=ENV,provider_factory=lambda *a:Provider())
        outputs.append(result)
        return result
    monkeypatch.setattr(service,'run_review',controlled)
    original_download=real_review_ui.st.download_button
    def capture_download(label,*args,**kwargs):
        if label=='Export sanitized prioritized report': exports.append(json.loads(kwargs['data']))
        return original_download(label,*args,**kwargs)
    monkeypatch.setattr(real_review_ui.st,'download_button',capture_download)
    at=app()
    assert not outputs and button(at,'Start live real-code review').disabled
    check(at,'I authorize disclosure of this exact snapshot to Nebius for code review.')
    click(at,'Authorize model transmission')
    assert not outputs
    check(at,'I authorize one bounded live review now.')
    click(at,'Start live real-code review')
    assert not at.exception and not at.error and len(outputs)==1
    result=outputs[0]
    assert result.status==('partial' if malformed else 'completed')
    assert result.persisted and result.cleanup=='closed'
    assert result.usage.model_attempts==1 and result.usage.tool_attempts==2
    summary=next(s for s in result.report.specialists if s.selected)
    assert summary.agent_name=='code_quality'
    assert summary.diagnostics.fully_inspected_files==2
    assert summary.diagnostics.preparation.read_attempts==2
    assert len(result.report.findings)==(0 if malformed else 1)
    assert summary.synthesis==('malformed_output' if malformed else 'succeeded')
    permit=service.read_transmission(captured.approval_id,result.transmission_id,specialists=('code_quality',))
    with pytest.raises(IntakeRejected): transmission_claims.ensure_unconsumed(permit)
    assert service.load_review(captured.approval_id,result.run_id)==result
    assert exports[-1]['human_decision'] is None
    path=intake._STORE/'imports'/str(captured.approval_id)/'review_artifacts'/f'{result.run_id}.json'
    assert stat.S_IMODE(path.stat().st_mode)==0o600
    if not malformed:
        finding=result.report.findings[0]
        assert service.evidence_excerpt(captured.approval_id,result.run_id,finding.finding_id,0)=='value = 1\r\n'
        assert finding.claim_status=='unverified'
    else:
        assert any('Incomplete review' in w.value for w in at.warning)
        assert 'missing_required_fields' in str(result.report.limitations)
    click(at,'Start human review')
    assert local.get(local.thread_for(result.report),result.report).outcome is None
    if malformed:
        assert button(at,'Record manual decision').disabled
        check(at,'I acknowledge that this review is incomplete.')
    assert button(at,'Record manual decision').disabled
    decision=next(s for s in at.selectbox if s.label=='Local report decision')
    assert decision.value is None
    decision.set_value('reject').run()
    click(at,'Record manual decision')  # Explicit reject; no automatic approval.
    assert not at.error and not at.exception
    assert exports[-1]['human_decision']['decision']=='reject'
    assert exports[-1]['publication_authorized'] is False
    assert exports[-1]['semantic_correctness']=='not_automatically_established'
    assert 'value = 1' not in json.dumps(exports) and 'synthetic-test-secret' not in json.dumps(exports)
    at.run()
    assert len(outputs)==1 and button(at,'Start live real-code review').disabled
    assert local.get(local.thread_for(result.report),result.report).outcome.decision=='reject'
