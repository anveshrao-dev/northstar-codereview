"""Fictional evidence cannot be substituted with live or mismatched source."""
import pytest
from northstar_codereview.ui_service import run_baseline, scripted_evidence, UIError


@pytest.mark.parametrize('change',['live','dataset','file_hash','finding','index'])
def test_scripted_evidence_rejects_untrusted_binding(change):
    from uuid import uuid4
    from northstar_codereview.reporting import ReviewReport, _digest
    bundle=run_baseline('java_backend');finding=bundle.report.findings[0].finding_id;index=0
    if change=='live':bundle=bundle.model_copy(update={'provenance':'recorded_live','evaluation':None})
    if change=='dataset':bundle=bundle.model_copy(update={'evaluation':bundle.evaluation.model_copy(update={'dataset_id':'0'*64})})
    if change=='file_hash':
        body=bundle.report.model_dump(mode='json',exclude={'report_id'})
        body['findings'][0]['evidence'][0]['source_sha256']='0'*64
        report=ReviewReport.model_validate({**body,'report_id':_digest(body)})
        bundle=bundle.model_copy(update={'report':report,'evaluation':bundle.evaluation.model_copy(update={'report_id':report.report_id})})
    if change=='finding':finding=uuid4()
    if change=='index':index=True
    with pytest.raises(UIError) as error:scripted_evidence(bundle,finding,index)
    assert str(error.value)=='Local UI operation unavailable.'
