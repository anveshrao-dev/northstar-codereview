"""UI reads are explicit, bounded, local, and separate from review authorization."""
import pytest
from .test_enterprise_ui import screen
from .test_real_source import local_intake
from .test_real_review import service, captured, Fake, ENV, permit
from .test_ui_pipeline import result


@pytest.fixture(autouse=True)
def isolated(local_intake): pass


def open_result():
    at=screen();next(b for b in at.button if b.label=='Open review').click().run();return at


def test_select_stage_changes_recorded_panel_without_inference(result, service, monkeypatch):
    monkeypatch.setattr(service,'run_review',lambda *a,**k:pytest.fail('inference'))
    at=open_result()
    select=next(s for s in at.selectbox if s.label=='Select pipeline stage')
    assert len(select.options)==7
    select.set_value('analysis').run()
    assert any(s.value=='Recorded stage details' for s in at.subheader)
    assert any('Analyzed lines: 2' in t.value for t in at.text)
    assert any('not chronological' in c.value for c in at.caption)
    assert not at.code and not at.exception


def test_source_read_requires_click_and_is_not_kept_on_rerun(result,service,monkeypatch):
    calls=[];original=service.source_excerpt
    def observed(*a,**k):calls.append(True);return original(*a,**k)
    monkeypatch.setattr(service,'source_excerpt',observed)
    at=open_result();assert not calls and not at.code
    next(s for s in at.selectbox if s.label=='Snapshot source file').set_value('tests/test_risk.py').run()
    assert not calls
    next(b for b in at.button if b.label=='Read selected source lines').click().run()
    assert len(calls)==1 and at.code[0].value=='1 | value = 1\r'
    assert len(at.get('download_button'))==1  # Existing sanitized report, no source download.
    at.run();assert len(calls)==1 and not at.code
    assert service.load_review(result.import_id,result.run_id)==result


def test_source_is_plain_text_and_unavailable_errors_are_sanitized(result,service,monkeypatch):
    marker='<script>throw new Error("untrusted source")</script>'
    monkeypatch.setattr(service,'source_excerpt',lambda *a,**k:marker)
    at=open_result();next(b for b in at.button if b.label=='Read selected source lines').click().run()
    assert at.code[0].value=='1 | '+marker
    assert not any(marker in h.proto.body for h in at.get('html'))
    def fail(*a,**k):raise ValueError('PRIVATE_ERROR_PAYLOAD')
    monkeypatch.setattr(service,'source_file_catalog',fail)
    at.run()
    assert not at.code
    assert any('Source files unavailable' in w.value for w in at.warning)
    assert not any('PRIVATE_ERROR_PAYLOAD' in str(e.value) for e in at if hasattr(e,'value'))


def test_human_review_context_and_existing_scripted_evidence_demo(service,monkeypatch):
    monkeypatch.setattr(service,'run_review',lambda *a,**k:pytest.fail('inference'))
    at=screen();at.button(key='nav:Test Lab').click().run()
    next(b for b in at.button if b.label=='Run scripted review').click().run()
    assert any('SCRIPTED EXPECTED MATCH' in c.value for c in at.caption)
    assert any('SCRIPTED FALSE-POSITIVE' in w.value for w in at.warning)
    assert any('does not approve a GitHub pull request' in m.value for m in at.markdown)
    assert not at.code
    next(b for b in at.button if b.label.startswith('Show fictional source:')).click().run()
    assert at.code and not at.exception
    assert any('SCRIPTED INTEGRATION' in i.value for i in at.info)
    assert not any(b.label=='Start live real-code review' for b in at.button)
