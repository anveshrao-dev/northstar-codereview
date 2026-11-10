"""Session theme controls and native-widget style coverage, not browser certification."""
import pytest

from .test_real_source import local_intake
from .test_real_review import service, captured
from .test_readability_consistency import isolated_ui, contrast
from .test_ui_interaction_acceptance import csv_reviews, open_csv, click, select_tab, active_tab
from .test_enterprise_ui import screen


def change(at,mode):
    at.radio(key='appearance:theme').set_value(mode).run()
    assert not at.exception
    return at


def emitted(at):
    return next(h.proto.body for h in at.get('html') if 'data-northstar-theme=' in h.proto.body)


@pytest.mark.parametrize('mode',['Light','Dark'])
@pytest.mark.parametrize('foreground',['primary','secondary','metadata'])
@pytest.mark.parametrize('background',['background','sidebar','panel'])
def test_both_palette_text_pairs_are_readable(mode,foreground,background):
    from northstar_codereview.ui_theme import PALETTES
    p=PALETTES[mode]
    assert contrast(p[foreground],p[background])>=4.5


@pytest.mark.parametrize('mode',['Light','Dark'])
def test_controls_statuses_and_checked_marks_use_accessible_pairs(mode):
    from northstar_codereview.ui_theme import PALETTES
    p=PALETTES[mode]
    assert contrast(p['on-accent'],p['accent'])>=4.5
    for key in ('success','partial','error','running','pending','accent','control'):
        for surface in ('panel','background','sidebar'):
            assert contrast(p[key],p[surface])>=3
    for color in ('red','orange','yellow','green','blue','violet','gray'):
        assert contrast(p[color+'-text'],p[color+'-bg'])>=4.5


@pytest.mark.parametrize('mode',['Light','Dark'])
def test_css_targets_native_foregrounds_and_portals_not_only_token_definitions(mode):
    from northstar_codereview.ui_theme import style_for
    css=style_for(mode)
    for selector in ('[data-testid="stText"] span','[data-testid="stHeading"]',
        '[data-testid="stMarkdownContainer"]','[data-testid="stMetricValue"]',
        '[data-testid="stSidebar"]','[data-testid="stHeader"]',
        '[data-testid="stCaptionContainer"]','[data-testid="stTextInputRootElement"]',
        '[data-testid="stNumberInputContainer"]','[data-testid="stTextAreaRootElement"]',
        '[data-testid="stSelectboxVirtualDropdown"]','[role="option"]',
        '[data-testid="stTooltipContent"]','[data-testid="stCode"]',
        '[data-item-hl]','[data-hovered]','st-key-theme-panel-',
        '[data-testid="stSelectbox"] [role="group"]',
        '[data-testid="stTabsScrollLeft"]','[data-testid="stTooltipErrorContent"]',
        '[role="listbox"][data-empty] [role="option"] > span',
        '[data-testid="stAlertContentError"]','[data-testid="stAlertContentSuccess"]',
        '[data-focus-visible]','[data-selected]','::placeholder',':disabled'):
        assert selector in css
    assert 'color: var(--ns-primary) !important' in css
    assert 'opacity: 1 !important' in css
    assert 'color: var(--ns-on-accent) !important' in css
    assert 'background-color: var(--ns-on-accent) !important' in css  # Checked radio dot.
    assert 'st-key-theme-badge-red-' in css
    assert 'https://' not in css and '<script' not in css and 'linear-gradient' not in css


def test_default_light_switch_both_ways_and_independent_sessions(isolated_ui):
    at=screen()
    assert at.sidebar.radio(key='appearance:theme').value=='Light'
    assert 'data-northstar-theme="light"' in emitted(at)
    at.button(key='nav:Settings').click().run()
    change(at,'Dark')
    assert at.session_state['navigation']=='Settings'
    assert 'data-northstar-theme="dark"' in emitted(at) and '--ns-background: #0B1220' in emitted(at)
    at.run();assert at.radio(key='appearance:theme').value=='Dark'
    assert screen().radio(key='appearance:theme').value=='Light'
    change(at,'Light')
    assert at.session_state['navigation']=='Settings'
    assert '--ns-background: #F5F7FB' in emitted(at)


def test_theme_keeps_saved_review_tab_stage_source_and_private_records(csv_reviews,service,monkeypatch):
    from northstar_codereview import real_source as intake
    before={p:p.read_bytes() for p in intake._STORE.rglob('*') if p.is_file()}
    at=open_csv(csv_reviews)
    identity=at.session_state['workspace:active-review']
    click(at,'Walk through this review');click(at,'Next stage');click(at,'Next stage')
    change(at,'Dark')
    assert active_tab(at)=='Pipeline'
    assert next(s for s in at.selectbox if s.label=='Select pipeline stage').value=='preparation'
    select_tab(at,'Source Files')
    next(s for s in at.selectbox if s.label=='Snapshot source file').set_value('tests/test_imports.py').run()
    calls=[];original=service.source_excerpt
    def read(*a,**k):calls.append(True);return original(*a,**k)
    monkeypatch.setattr(service,'source_excerpt',read)
    click(at,'Read selected source lines');assert at.code
    change(at,'Light')
    assert active_tab(at)=='Source Files' and at.session_state['workspace:active-review']==identity
    assert next(s for s in at.selectbox if s.label=='Snapshot source file').value=='tests/test_imports.py'
    assert calls==[True] and not at.code  # Existing ephemeral-source privacy behavior.
    assert before=={p:p.read_bytes() for p in intake._STORE.rglob('*') if p.is_file()}


@pytest.mark.parametrize('finalized',[False,True])
def test_theme_preserves_pending_or_recorded_human_decision(isolated_ui,finalized):
    at=screen();click(at,'Explore scripted demonstration');click(at,'Run scripted review')
    select_tab(at,'Human Review');click(at,'Start human review')
    choice=next(s for s in at.selectbox if s.label=='Local report decision')
    choice.set_value('request_changes').run()
    if finalized:click(at,'Record manual decision')
    state=next(v for k,v in at.session_state.items() if k.startswith('approval:'))
    before=isolated_ui.get(state.thread_id,state.report)
    for mode in ('Dark','Light'):
        change(at,mode)
        assert active_tab(at)=='Human Review'
        after=isolated_ui.get(state.thread_id,state.report)
        assert after.status==before.status and after.outcome==before.outcome
        if not finalized:
            assert next(s for s in at.selectbox if s.label=='Local report decision').value=='request_changes'


def test_theme_never_runs_test_lab_or_changes_consent(csv_reviews,monkeypatch):
    from northstar_codereview import streamlit_ui
    monkeypatch.setattr(streamlit_ui,'run_baseline',lambda *a:pytest.fail('Automatic scripted execution'))
    at=screen();click(at,'Explore scripted demonstration');change(at,'Dark')
    assert next(s for s in at.selectbox if s.label=='Fictional scenario').value=='java_backend'
    assert not at.tabs
    at.button(key='nav:New Review').click().run()
    next(s for s in at.selectbox if s.label=='Choose an approved source snapshot').set_value('csv_import').run()
    next(c for c in at.checkbox if c.label.startswith('I authorize disclosure')).check().run()
    change(at,'Light')
    assert next(c for c in at.checkbox if c.label.startswith('I authorize disclosure')).value
    assert not next(c for c in at.checkbox if c.label=='I authorize one bounded live review now.').value
    assert next(b for b in at.button if b.label=='Start live real-code review').disabled


def test_unknown_theme_cannot_inject_css():
    from northstar_codereview.ui_theme import style_for
    assert style_for('</style><script>PRIVATE</script>')==style_for('Light')


@pytest.mark.parametrize('mode',['Dark','Light'])
def test_all_destinations_and_scripted_sections_render_with_active_palette(isolated_ui,mode):
    at=screen();change(at,mode)
    for page in ('New Review','Review History','Settings','Overview','Test Lab'):
        at.button(key='nav:'+page).click().run()
        assert not at.exception
        assert at.radio(key='appearance:theme').value==mode
        assert f'data-northstar-theme="{mode.lower()}"' in emitted(at)
    click(at,'Run scripted review')
    for section in ('Summary','Pipeline','Findings','Coverage','Source Files','Human Review','Technical Details'):
        select_tab(at,section)
        assert not at.exception and active_tab(at)==section
        assert at.radio(key='appearance:theme').value==mode
    assert any('SCRIPTED FALSE-POSITIVE TEST CASE' in w.value for w in at.warning)
    assert any('st-key-theme-badge-red-' in h.proto.body for h in at.get('html'))
