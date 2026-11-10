"""Explicit human-facing profile selection using generated local intake."""
import json
import pytest
from .test_real_source import local_intake
from .test_real_review import service,captured,ENV,Fake
from .test_real_review_ui import app,check,click,button


def test_cli_default_and_explicit_profile_bindings(service,captured,capsys):
    from northstar_codereview.real_review_cli import main
    base=['authorize','--sample','portfolio_risk','--import-id',str(captured.approval_id),
          '--manifest-digest',captured.manifest.digest,'--allow-transmission']
    assert main(base+['--execution-profile','extended_csv'])==0
    payload=json.loads(capsys.readouterr().out)
    assert payload['execution_profile']['name']=='extended_csv'
    permit=service.read_transmission(captured.approval_id,payload['transmission_id'])
    assert permit.execution_profile.name=='extended_csv'
    assert main(base)==0
    assert json.loads(capsys.readouterr().out)['execution_profile']['name']=='standard'
    assert main(base+['--execution-profile','unbounded'])==2


def test_ui_profile_change_requires_new_consent_and_approval(service,captured,monkeypatch):
    calls=[]
    original=service.run_review
    async def fake(*a,**kw):
        calls.append(kw['execution_profile'])
        return await original(*a,**kw,environ=ENV,provider_factory=lambda *a:Fake())
    monkeypatch.setattr(service,'run_review',fake)
    at=app()
    choose=lambda:next(s for s in at.selectbox if s.label=='Execution profile')
    assert choose().value=='standard'
    check(at,'I authorize disclosure of this exact snapshot to Nebius for code review.')
    click(at,'Authorize model transmission')
    choose().set_value('extended_bm25').run()
    assert button(at,'Authorize model transmission').disabled
    assert button(at,'Start live real-code review').disabled
    assert not at.exception and not calls
    check(at,'I authorize disclosure of this exact snapshot to Nebius for code review.')
    click(at,'Authorize model transmission')
    check(at,'I authorize one bounded live review now.')
    click(at,'Start live real-code review')
    assert not at.exception and calls==['extended_bm25']
    assert any('extended_bm25' in t.value for t in at.text)
    at.run()
    assert calls==['extended_bm25']
