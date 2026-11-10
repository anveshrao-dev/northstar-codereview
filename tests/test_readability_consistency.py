"""Defined color-pair math and native UI/state tests; no browser AA certification."""
from pathlib import Path
import re
import tomllib

import pytest

from .test_real_source import local_intake
from .test_enterprise_ui import screen
from .test_ui_interaction_acceptance import click, select_tab, tab

ROOT=Path(__file__).resolve().parents[1]


def luminance(color):
    values=[int(color[i:i+2],16)/255 for i in (1,3,5)]
    linear=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in values]
    return sum(v*w for v,w in zip(linear,(.2126,.7152,.0722)))


def contrast(a,b):
    low,high=sorted((luminance(a),luminance(b)))
    return (high+.05)/(low+.05)


def theme():return tomllib.loads((ROOT/'.streamlit/config.toml').read_text())['theme']
def tokens():
    from northstar_codereview.ui_theme import style_for
    return dict(re.findall(r'--ns-([a-z]+):\s*(#[\da-fA-F]{6})',style_for('Light')))


@pytest.mark.parametrize('foreground',['primary','secondary','metadata'])
@pytest.mark.parametrize('surface',['background','sidebar','panel'])
def test_rendered_palette_text_pairs_meet_normal_text_aa(foreground,surface):
    colors=tokens()
    assert contrast(colors[foreground],colors[surface])>=4.5


@pytest.mark.parametrize('color',['red','orange','yellow','blue','green','violet','gray'])
def test_native_warning_badge_and_status_text_pairs_meet_aa(color):
    t=theme()
    assert contrast(t[color+'TextColor'],t[color+'BackgroundColor'])>=4.5
    assert contrast(t[color+'Color'],t['secondaryBackgroundColor'])>=3


def test_native_body_sidebar_code_and_typography_are_readable():
    t=theme()
    assert t['textColor']=='#0F172A' and t['sidebar']['textColor']=='#0F172A'
    assert 14<=t['baseFontSize']<=16
    assert contrast(t['codeTextColor'],t['codeBackgroundColor'])>=4.5
    assert t['codeFontSize']=='0.875rem'
    assert contrast(t['textColor'],t['backgroundColor'])>=4.5
    assert contrast(t['sidebar']['textColor'],t['sidebar']['backgroundColor'])>=4.5


def test_primary_button_and_focus_are_readable_without_status_color_only():
    from northstar_codereview.ui_style import _PIPELINE_STYLE,_STYLE,_ICONS
    c=tokens()
    assert contrast(c['background'],c['accent'])>=4.5
    for surface in ('background','sidebar','panel'):
        assert contrast(c['accent'],c[surface])>=3
    from northstar_codereview.ui_theme import style_for
    assert ':focus-visible' in style_for('Light') and 'prefers-reduced-motion' in _STYLE
    indicators=[c[key] for key in re.findall(r'--stage-color:var\(--ns-([a-z]+)\)',_PIPELINE_STYLE)]
    assert indicators and all(contrast(color,c['panel'])>=3 for color in indicators)
    assert all(_ICONS.values())


def test_scoped_captions_form_states_and_code_have_explicit_readability_rules():
    from northstar_codereview.ui_style import _STYLE
    from northstar_codereview.ui_theme import style_for
    _STYLE += style_for('Light')
    assert '[data-testid="stCaptionContainer"] p' in _STYLE
    assert '[data-testid="stWidgetLabel"] p' in _STYLE
    assert '::placeholder' in _STYLE and ':disabled' in _STYLE and '-webkit-text-fill-color' in _STYLE
    assert 'button[role="tab"]' in _STYLE and '[aria-selected="true"]' in _STYLE
    assert 'font-size: .875rem' in _STYLE
    assert 'opacity: 1' in _STYLE
    assert 'linear-gradient' not in _STYLE
    assert 'https://' not in _STYLE and 'unsafe_allow_html' not in _STYLE
    assert '[class*="st-key-product-title-"] [data-testid="stText"]' in _STYLE
    assert 'font-size: 1.25rem; font-weight: 600; line-height: 1.4' in _STYLE


@pytest.fixture
def isolated_ui(local_intake,tmp_path,monkeypatch):
    from northstar_codereview import streamlit_ui,real_review_ui,approval_store,real_review
    from northstar_codereview.ui_service import LocalReview
    monkeypatch.setattr(approval_store,'PROJECT_ROOT',tmp_path)
    local=LocalReview(database=tmp_path/'.runtime/decisions.sqlite3')
    monkeypatch.setattr(streamlit_ui,'LocalReview',lambda:local)
    monkeypatch.setattr(real_review_ui,'LocalReview',lambda:local)
    def forbidden(*a,**k):pytest.fail('Unintended real execution or authorization')
    monkeypatch.setattr(real_review,'run_review',forbidden)
    monkeypatch.setattr(real_review,'authorize_transmission',forbidden)
    return local


@pytest.mark.parametrize('scenario,name,count,specialist',[
    ('java_backend','Java backend',3,'Code Quality'),
    ('angular_frontend','Angular / TypeScript',4,'Test Coverage'),
    ('jenkins_aks','Jenkins on AKS',5,'GitOps')])
def test_selected_scenario_description_and_results_never_cross(isolated_ui,scenario,name,count,specialist):
    at=screen();at.button(key='nav:Test Lab').click().run()
    next(s for s in at.selectbox if s.label=='Fictional scenario').set_value(scenario).run()
    description=next(c.value for c in at.caption if 'demonstration ·' in c.value)
    assert name in description and f'{count} fictional files' in description and specialist in description
    if scenario!='java_backend':assert 'Java backend' not in description
    assert not at.metric  # Selecting metadata did not run a graph.
    click(at,'Run scripted review')
    assert any('SCRIPTED INTEGRATION' in e.value for e in at.info)
    other='java_backend' if scenario!='java_backend' else 'jenkins_aks'
    next(s for s in at.selectbox if s.label=='Fictional scenario').set_value(other).run()
    assert not at.metric and not at.code and not at.exception


def test_guided_paths_are_visible_outside_expanders_even_without_real_history(isolated_ui,monkeypatch):
    from northstar_codereview import streamlit_ui
    monkeypatch.setattr(streamlit_ui,'run_baseline',lambda *a:pytest.fail('Automatic scripted execution'))
    at=screen()
    assert any(s.value=='Explore Northstar' for s in at.subheader)
    assert next(b for b in at.button if b.label=='Explore real review').disabled
    assert not next(b for b in at.button if b.label=='Explore scripted demonstration').disabled
    assert not any(b.label.startswith('Explore real review') or b.label.startswith('Explore scripted demonstration') for e in at.expander for b in e.button)
    click(at,'Explore scripted demonstration')
    assert next(s for s in at.selectbox if s.label=='Fictional scenario').value=='java_backend'
    assert not at.tabs and not at.exception


@pytest.mark.parametrize('decision',['approve','reject','request_changes'])
def test_persisted_decision_immediately_replaces_pending_and_survives_rerun(isolated_ui,monkeypatch,decision):
    from northstar_codereview import streamlit_ui
    calls=[];original=streamlit_ui.run_baseline
    def run(s):calls.append(s);return original(s)
    monkeypatch.setattr(streamlit_ui,'run_baseline',run)
    submits=[];submit=isolated_ui.submit
    def record(*args,**kwargs):
        submits.append(True)
        return submit(*args,**kwargs)
    monkeypatch.setattr(isolated_ui,'submit',record)
    local=isolated_ui;at=screen();click(at,'Explore scripted demonstration');click(at,'Run scripted review')
    select_tab(at,'Human Review');click(at,'Start human review')
    assert any(t.value=='Human report review: pending' for t in tab(at,'Human Review').text)
    next(s for s in at.selectbox if s.label=='Local report decision').set_value(decision).run()
    click(at,'Record manual decision')
    human=tab(at,'Human Review')
    assert any(t.value=='Human report review: finalized' for t in human.text)
    assert not any('Human report review: pending' in t.value for t in human.text)
    assert any('Manual decision: '+decision in t.value for t in human.text)
    assert any('Local decision recorded' in e.value for e in human.success)
    assert not any(b.label=='Record manual decision' for b in human.button)
    view=next(v for k,v in at.session_state.items() if k.startswith('approval:'))
    assert local.get(view.thread_id,view.report).outcome.decision==decision
    at.run()
    assert any(t.value=='Human report review: finalized' for t in tab(at,'Human Review').text)
    assert calls==['java_backend']
    assert submits==[True]


def test_failed_decision_never_shows_success_or_unpersisted_outcome(isolated_ui,monkeypatch):
    at=screen();click(at,'Explore scripted demonstration');click(at,'Run scripted review')
    select_tab(at,'Human Review');click(at,'Start human review')
    def fail(*a,**k):raise ValueError('PRIVATE_STORAGE_ERROR')
    monkeypatch.setattr(isolated_ui,'submit',fail)
    next(s for s in at.selectbox if s.label=='Local report decision').set_value('reject').run()
    click(at,'Record manual decision')
    human=tab(at,'Human Review')
    assert any(t.value=='Human report review: pending' for t in human.text)
    assert not human.success and any('Decision rejected' in e.value for e in human.error)
    assert not any('PRIVATE_STORAGE_ERROR' in str(e.value) for e in at if hasattr(e,'value'))


def test_refresh_uses_durable_decision_over_stale_session_view(isolated_ui):
    at=screen();click(at,'Explore scripted demonstration');click(at,'Run scripted review')
    select_tab(at,'Human Review');click(at,'Start human review')
    view=next(v for k,v in at.session_state.items() if k.startswith('approval:'))
    isolated_ui.submit(view.thread_id,view.report,'reject')
    at.run()
    human=tab(at,'Human Review')
    assert any(t.value=='Human report review: finalized' for t in human.text)
    assert not any(b.label=='Record manual decision' for b in human.button)
