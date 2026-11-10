"""Small product journeys over generated saved reviews; never external inference."""
from hashlib import sha256

import pytest

from .test_real_source import local_intake
from .test_real_review import service
from .test_ui_interaction_acceptance import csv_reviews, click, active_tab, tab, select_tab
from .test_enterprise_ui import screen


def test_real_exploration_opens_newest_valid_completed_csv(csv_reviews):
    receipt,outputs=csv_reviews
    at=screen()
    assert any(s.value=='Explore Northstar' for s in at.subheader)
    assert any(s.value=='Real AI Review' for s in at.subheader)
    assert any('Genuine Nebius Code Quality execution' in c.value for c in at.caption)
    assert any('12/12 validated analyzed lines' in t.value for t in at.text)
    assert any('0 retained findings' in t.value for t in at.text)
    assert any('not proof of defect-free code' in c.value for c in at.caption)
    click(at,'Explore real review')
    assert at.session_state['history:selected']==(str(receipt.approval_id),str(outputs[-1].run_id))
    assert any(m.label=='Analyzed coverage' and m.value=='12/12 lines' for m in at.metric)
    assert any('REAL-SOURCE NEBIUS REVIEW' in e.value for e in at.info)
    assert not at.code and not at.error


def test_newer_partial_cannot_replace_completed_csv_exploration(csv_reviews,monkeypatch):
    from northstar_codereview import review_history
    from northstar_codereview.review_history import RetainedReview
    receipt,outputs=csv_reviews
    rows=review_history.retained_reviews()
    # A failed cleanup can leave a complete report with partial overall status.
    newer=RetainedReview(rows[0].review.model_copy(update={'status':'partial','cleanup':'failed','category':'cleanup_failed'}),rows[0].file_modified)
    monkeypatch.setattr(review_history,'retained_reviews',lambda:(newer,rows[1]))
    at=screen();click(at,'Explore real review')
    assert at.session_state['history:selected']==(str(receipt.approval_id),str(outputs[0].run_id))


@pytest.mark.parametrize('state',['missing','expired','corrupt'])
def test_unavailable_csv_never_invents_results_or_enables_real_action(csv_reviews,monkeypatch,state):
    from datetime import datetime,timedelta
    from northstar_codereview import real_source as intake,real_source_contracts as contracts
    receipt,_=csv_reviews
    if state=='expired':
        class Later(datetime):
            @classmethod
            def now(cls,tz=None):return receipt.manifest.retain_until+timedelta(seconds=1)
        monkeypatch.setattr(contracts,'datetime',Later)
    else:
        for path in (intake._STORE/'imports'/str(receipt.approval_id)/'review_artifacts').glob('*.json'):
            if state=='missing': path.unlink()
            else: path.write_bytes(path.read_bytes().replace(b'completed',b'partial'))
    at=screen()
    assert next(b for b in at.button if b.label=='Explore real review').disabled
    assert any('Completed CSV review unavailable' in i.value for i in at.info)
    assert not any('validated analyzed lines' in t.value for t in at.text)
    assert not any('Genuine Nebius Code Quality execution' in c.value for c in at.caption)
    assert not at.exception and not at.error


def test_java_exploration_resets_stale_scenario_and_mode_without_autorun(csv_reviews,monkeypatch):
    from northstar_codereview import streamlit_ui
    at=screen();at.button(key='nav:Test Lab').click().run()
    next(s for s in at.selectbox if s.label=='Fictional scenario').set_value('angular_frontend').run()
    next(r for r in at.radio if r.label=='Evaluation mode').set_value('Saved live results').run()
    at.button(key='nav:Overview').click().run()
    original=streamlit_ui.run_baseline; calls=[]
    def baseline(scenario):calls.append(scenario);return original(scenario)
    monkeypatch.setattr(streamlit_ui,'run_baseline',baseline)
    click(at,'Explore scripted demonstration')
    assert at.session_state['navigation']=='Test Lab'
    assert next(s for s in at.selectbox if s.label=='Fictional scenario').value=='java_backend'
    assert next(r for r in at.radio if r.label=='Evaluation mode').value=='Scripted integration'
    assert not calls and not at.tabs
    click(at,'Run scripted review')
    assert calls==['java_backend']
    assert any('SCRIPTED INTEGRATION' in e.value for e in at.info)
    assert any('SCRIPTED FALSE-POSITIVE' in e.value for e in at.warning)
    click(at,'Inspect Findings'); assert active_tab(at)=='Findings'
    next(b for b in tab(at,'Findings').button if b.label.startswith('Show fictional source:')).click().run()
    assert tab(at,'Findings').code and not at.exception


def test_three_primary_actions_precede_long_context_and_keep_tab_state(csv_reviews):
    at=screen();click(at,'Explore real review')
    actions=('View Pipeline','View Source Files','Inspect Findings')
    tree=list(at.main)
    last_action=max(next(i for i,e in enumerate(tree) if e.type=='button' and e.label==label) for label in actions)
    info_index=next(i for i,e in enumerate(tree) if e.type=='info' and 'REAL-SOURCE NEBIUS REVIEW' in e.value)
    assert last_action<info_index
    for label,target in zip(actions,('Pipeline','Source Files','Findings')):
        click(at,label); assert active_tab(at)==target
        at.run(); assert active_tab(at)==target
    assert all(not e.proto.expanded for e in at.expander if e.label in ('Coverage and review context','Technical details · run'))
    assert not at.code


def test_guided_journeys_preserve_records_and_human_decisions(csv_reviews):
    from northstar_codereview import real_source as intake
    before={str(p):sha256(p.read_bytes()).hexdigest() for p in intake._STORE.rglob('*') if p.is_file()}
    at=screen();click(at,'Explore real review');select_tab(at,'Human Review')
    assert any('No decision has been made' in e.value for e in at.markdown)
    at.button(key='nav:Overview').click().run();click(at,'Explore scripted demonstration')
    after={str(p):sha256(p.read_bytes()).hexdigest() for p in intake._STORE.rglob('*') if p.is_file()}
    assert before==after and not at.exception
