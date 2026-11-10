"""Saved-result exploration only: generated snapshots, no live providers or decisions."""
from dataclasses import replace
import re

import pytest
from streamlit.testing.v1 import AppTest

from .test_real_source import local_intake
from .test_real_review import service
from .test_ui_interaction_acceptance import csv_reviews, open_csv, click, select_tab, tab, active_tab
from .test_readability_consistency import isolated_ui, theme, tokens, contrast
from .test_enterprise_ui import screen


def render_stages(rows, saved=True):
    from northstar_codereview.ui_style import render_pipeline, workspace
    with workspace():
        render_pipeline(rows, review_key='generated-review', allow_walkthrough=saved)


def native_states(at):
    rows=[]
    for block in at.get('flex_container'):
        match=re.search(r'pipeline-card-status-([a-z_]+)-(selected|idle)-(source|authorization|preparation|analysis|evidence|report|human)-',block.proto.id)
        if match: rows.append((match[3],match[1]))
    return rows


def selected_card(at,key,status):
    return any(f'pipeline-card-status-{status}-selected-{key}-' in b.proto.id for b in at.get('flex_container'))


def test_light_theme_default_and_control_contrast():
    t=theme();c=tokens()
    assert t['base']=='light' and t['backgroundColor']=='#F5F7FB'
    assert t['sidebar']['backgroundColor']==t['secondaryBackgroundColor']=='#FFFFFF'
    assert t['textColor']=='#0F172A' and t['primaryColor']=='#0F766E'
    assert t['baseFontSize']==16
    for surface in ('background','sidebar','panel'):
        for text in ('primary','secondary','metadata'):
            assert contrast(c[text],c[surface])>=4.5
        assert contrast(c['accent'],c[surface])>=3
    assert contrast('#FFFFFF',t['primaryColor'])>=4.5
    assert contrast(t['borderColor'],t['secondaryBackgroundColor'])>=3
    from northstar_codereview.ui_style import _STYLE
    assert 'linear-gradient' not in _STYLE


def test_walkthrough_is_voluntary_and_not_available_on_unsaved_attempts():
    from northstar_codereview.ui_pipeline import pipeline_stages
    at=AppTest.from_function(render_stages,args=(pipeline_stages(None),False)).run()
    assert not any('walkthrough' in b.label.lower() for b in at.button)
    at=AppTest.from_function(render_stages,args=(pipeline_stages(None),)).run()
    assert not any(b.label in ('Next stage','Previous stage') for b in at.button)
    click(at,'Start walkthrough')
    assert any('Saved review walkthrough — reconstructed from recorded results.' in c.value for c in at.caption)
    assert next(b for b in at.button if b.label=='Previous stage').disabled
    assert selected_card(at,'source','unknown')
    assert 'Source verified · Unavailable' in '\n'.join(t.value for t in at.text)


@pytest.mark.parametrize('status',['completed','partial','failed','unknown'])
def test_walkthrough_keeps_recorded_outcomes_and_boundaries(status):
    from northstar_codereview.ui_pipeline import pipeline_stages,STAGES
    rows=tuple(replace(s,status=status) for s in pipeline_stages(None))
    at=AppTest.from_function(render_stages,args=(rows,)).run()
    before=native_states(at)
    click(at,'Start walkthrough')
    for key,title in STAGES:
        assert next(s for s in at.selectbox if s.label=='Select pipeline stage').value==key
        assert selected_card(at,key,status)
        if key!='human':click(at,'Next stage')
    assert next(b for b in at.button if b.label=='Next stage').disabled
    click(at,'Previous stage')
    assert next(s for s in at.selectbox if s.label=='Select pipeline stage').value=='report'
    at.run()
    assert next(s for s in at.selectbox if s.label=='Select pipeline stage').value=='report'
    click(at,'End walkthrough')
    assert not any(b.label=='Next stage' for b in at.button)
    assert native_states(at)==before


def test_saved_walkthrough_opens_pipeline_and_keeps_artifact_bytes(csv_reviews):
    from northstar_codereview import real_source as intake
    receipt,outputs=csv_reviews
    files={p:p.read_bytes() for p in intake._STORE.rglob('*') if p.is_file()}
    at=open_csv(csv_reviews)
    click(at,'Walk through this review')
    assert active_tab(at)=='Pipeline'
    click(at,'Next stage');click(at,'Next stage');click(at,'Next stage')
    p=tab(at,'Pipeline')
    assert any('schema=valid; finish=stop; truncated=False' in t.value for t in p.text)
    assert any('Analyzed lines: 12' in t.value for t in p.text)
    assert not at.code
    click(at,'Back to reviews')
    at.button(key=f'open:{receipt.approval_id}:{outputs[1].run_id}').click().run()
    click(at,'View Pipeline')
    assert not any(b.label=='Next stage' for b in at.button)
    assert next(s for s in at.selectbox if s.label=='Select pipeline stage').value=='source'
    assert files=={p:p.read_bytes() for p in intake._STORE.rglob('*') if p.is_file()}


def test_invalid_stage_session_state_resets_safely():
    def stale_session():
        import streamlit as st
        from northstar_codereview.ui_style import render_pipeline
        from northstar_codereview.ui_pipeline import pipeline_stages
        st.session_state['pipeline:active-review']='generated-review'
        st.session_state['pipeline-stage:generated-review']='invalid-stage'
        render_pipeline(pipeline_stages(None),review_key='generated-review',allow_walkthrough=True)
    at=AppTest.from_function(stale_session).run()
    assert not at.exception
    assert next(s for s in at.selectbox if s.label=='Select pipeline stage').value=='source'


def test_human_review_has_three_sections_and_advanced_bindings(isolated_ui):
    at=screen();click(at,'Explore scripted demonstration');click(at,'Run scripted review')
    select_tab(at,'Human Review');h=tab(at,'Human Review')
    assert [s.value for s in h.subheader]==['Human review','1. Report summary','2. Inspect evidence','3. Make a local decision']
    advanced=next(e for e in h.expander if e.label=='Advanced · checkpoint and resume')
    assert not advanced.proto.expanded
    assert any(t.label=='Checkpoint ID' for t in advanced.text_input)
    assert not any(t.label=='Checkpoint ID' for t in h.text_input if t not in advanced.text_input)
    click(at,'Inspect source files');assert active_tab(at)=='Source Files'
    select_tab(at,'Human Review');click(at,'Inspect findings and evidence');assert active_tab(at)=='Findings'


@pytest.mark.parametrize('value,label',[('approve','Accept report'),('request_changes','Request follow-up'),('reject','Reject report')])
def test_friendly_decisions_preserve_exact_backend_values(isolated_ui,value,label):
    at=screen();click(at,'Explore scripted demonstration');click(at,'Run scripted review')
    select_tab(at,'Human Review');click(at,'Start human review')
    choice=next(s for s in at.selectbox if s.label=='Local report decision')
    assert choice.value is None
    assert set(choice.options)=={'Accept report','Request follow-up','Reject report'}
    choice.set_value(value).run();click(at,'Record manual decision')
    h=tab(at,'Human Review')
    assert any('Recorded decision: '+label in t.value for t in h.text)
    view=next(v for k,v in at.session_state.items() if k.startswith('approval:'))
    assert isolated_ui.get(view.thread_id,view.report).outcome.decision==value
    assert any('Human report review: finalized' in t.value for t in h.text)
    at.run();assert not any(b.label=='Record manual decision' for b in at.button)


def test_snapshot_copy_explains_viewing_and_new_execution(csv_reviews):
    at=open_csv(csv_reviews)
    assert any('Opening this saved report does not rerun analysis' in c.value for c in at.caption)
    at.button(key='nav:New Review').click().run()
    copy='\n'.join(c.value for c in at.caption)
    assert 'new execution' in copy and 'consumed transmission approval cannot be reused' in copy
    assert 'renewal' in copy and 'fresh authorization' in copy
    next(s for s in at.selectbox if s.label=='Choose an approved source snapshot').set_value('csv_import').run()
    assert next(b for b in at.button if b.label=='Start live real-code review').disabled


def test_highlight_never_interpolates_untrusted_selection():
    from northstar_codereview.ui_style import pipeline_html
    from northstar_codereview.ui_pipeline import pipeline_stages
    html=pipeline_html(pipeline_stages(None),selected='<script>PRIVATE_SELECTION</script>')
    assert 'PRIVATE_SELECTION' not in html and '<script>' not in html
    assert 'aria-current="step"' not in html
