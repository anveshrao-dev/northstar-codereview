"""Local presentation helpers; static styles use scoped keys and Streamlit test IDs."""
from contextlib import contextmanager

import streamlit as st

from .ui_theme import PALETTES, style_for


_STYLE = """
<style>
.st-key-product-shell {
    min-width: 0; overflow-wrap: anywhere; padding: 1rem .25rem 2rem;
}
.st-key-product-shell p { overflow-wrap: anywhere; }
.st-key-product-shell [data-testid="stCaptionContainer"] p,
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p,
.st-key-product-shell [data-testid="stWidgetLabel"] p,
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p {
    font-size: .875rem; line-height: 1.5;
}
.st-key-product-shell [data-testid="stText"] { font-size: 1rem; line-height: 1.5; }
.st-key-product-shell :is(input, textarea) { font-size: 1rem; }
.st-key-product-shell button[role="tab"] { font-size: .875rem; }
.st-key-product-shell [data-testid="stExpander"] summary { font-size: 1rem; }
.st-key-product-shell [data-testid="stCode"] pre { font-size: .875rem; line-height: 1.5; }
.st-key-product-shell button, .st-key-product-navigation button { min-height: 2.75rem; }
.st-key-product-navigation button { text-align: left; }
.st-key-product-shell [class*="st-key-product-title-"] [data-testid="stText"] {
    font-size: 1.25rem; font-weight: 600; line-height: 1.4;
}
.st-key-product-pipeline { padding: .75rem; border: 1px solid var(--ns-border); border-radius: .75rem; }
@media (max-width: 760px) {
    .st-key-product-shell { padding: .5rem 0 1rem; }
}
@media (prefers-reduced-motion: reduce) {
    .stApp *, [data-testid="stSelectboxVirtualDropdown"] * { animation: none !important; transition: none !important; scroll-behavior: auto !important; }
}
</style>
"""

@contextmanager
def workspace():
    """Native widths shrink to their parent; no minimum desktop width is imposed."""
    st.html(style_for(st.session_state.get('appearance:theme', 'Light')))
    st.html(_STYLE)
    with st.container(horizontal=True, horizontal_alignment='center'):
        with st.container(key='product-shell', width=1280, gap='medium'):
            yield


def navigation(destinations, on_navigate):
    page = st.session_state.get('navigation', 'Overview')
    if page not in destinations:
        page = 'Overview'
        st.session_state['navigation'] = page
    with st.sidebar:
        st.markdown('## ✦ Northstar')
        st.caption('AI Code Review Platform')
        if st.session_state.get('appearance:theme') not in PALETTES:
            st.session_state['appearance:theme'] = 'Light'
        st.radio('Appearance', ('Light','Dark'), key='appearance:theme', horizontal=True)
        with st.container(key='product-navigation'):
            for destination in destinations:
                st.button(destination, key='nav:' + destination, width='stretch',
                          type='primary' if destination == page else 'secondary',
                          on_click=on_navigate, args=(destination,))
        st.caption('Current page: ' + page)
        st.divider()
        st.caption('Single-user local workspace · No publishing')
    return page


def themed_badge(label, *, color, key):
    """Keep native badge text/semantics with a closed semantic palette wrapper."""
    if color not in ('red','orange','yellow','green','blue','violet','gray'):
        color = 'gray'
    with st.container(key='theme-badge-' + color + '-' + key, width='content'):
        st.badge(label, color=color)


def plain_title(value, key):
    """Give untrusted finding titles visual weight without interpreting Markdown."""
    with st.container(key='product-title-' + key):
        st.text(value)


# Closed labels and static CSS only. User/model prose always uses native text APIs.
_STATUS_COLORS = {'pending':'gray', 'running':'blue', 'completed':'green', 'partial':'orange',
                  'failed':'red', 'blocked':'orange', 'unknown':'gray'}
SPECIALISTS = {
    'code_quality': ('Code Quality', 'Behavior, correctness and maintainability', 'Python, Java and TypeScript application code'),
    'security': ('Security', 'Potential security weaknesses and unsafe boundaries', 'Source-level security review; no execution or scanners'),
    'test_coverage': ('Test Coverage', 'Missing behavioral tests and regression coverage', 'Tests read as source; never executed'),
    'gitops': ('GitOps Platform', 'Configuration consistency and deployment risks', 'Kubernetes and GitOps configuration'),
    'cicd_groovy': ('CI/CD Platform', 'Pipeline correctness and delivery safety', 'Jenkins and Groovy source'),
}


_PIPELINE_STYLE = """
<style>
.nsp-pipeline { display:grid; grid-template-columns:repeat(7,minmax(0,1fr)); gap:1.2rem;
  list-style:none; padding:0; margin:1rem 0; color:var(--ns-primary); }
.nsp-pipeline li { position:relative; min-width:0; border:2px solid var(--stage-color);
  border-radius:.6rem; padding:.75rem .5rem; background:var(--ns-panel); text-align:center;
  display:flex; flex-direction:column; align-items:center; gap:.4rem; overflow-wrap:anywhere; }
.nsp-pipeline li:not(:last-child)::after { content:"→"; position:absolute; right:-1.1rem;
  top:1.25rem; color:var(--ns-metadata); font-size:1.1rem; }
.nsp-icon { color:var(--stage-color); font-size:1.5rem; font-weight:700; line-height:1; }
.nsp-name { font-weight:600; font-size:.95rem; }
.nsp-state { font-size:.875rem; color:var(--ns-primary); }
.nsp-completed { --stage-color:var(--ns-success); }
.nsp-failed { --stage-color:var(--ns-error); }
.nsp-partial, .nsp-blocked { --stage-color:var(--ns-partial); }
.nsp-running { --stage-color:var(--ns-running); }
.nsp-pending, .nsp-skipped, .nsp-not_started, .nsp-unknown { --stage-color:var(--ns-pending); }
.nsp-pipeline li.nsp-selected { outline:3px solid var(--ns-accent); outline-offset:3px; }
.nsp-selection { color:var(--ns-accent); font-size:.875rem; font-weight:600; }
.nsp-compact { gap:.8rem; margin:.5rem 0; }
.nsp-compact li { padding:.4rem .25rem; border-width:1px; gap:.25rem; }
.nsp-compact li:not(:last-child)::after { right:-.75rem; font-size:.875rem; }
.nsp-compact .nsp-name, .nsp-compact .nsp-state { font-size:.875rem; }
.nsp-job { display:inline-flex; align-items:center; gap:.5rem; color:var(--ns-primary);
  border-left:4px solid var(--stage-color); padding:.35rem .65rem; background:var(--ns-panel); }
[class*="st-key-pipeline-cards-"] { display:grid; grid-template-columns:repeat(7,minmax(0,1fr)); gap:1.2rem; }
[class*="st-key-pipeline-card-status-"] { position:relative; min-width:0; padding:.5rem;
  border:2px solid var(--ns-pending); border-radius:.6rem; background:var(--ns-panel); }
[class*="st-key-pipeline-card-status-"] button { min-height:4.5rem; }
[class*="st-key-pipeline-card-status-"][class*="-connector-"]::after { content:"→"; position:absolute;
  right:-1.1rem; top:1.5rem; color:var(--ns-metadata); }
[class*="st-key-pipeline-card-status-completed-"] { border-color:var(--ns-success); }
[class*="st-key-pipeline-card-status-failed-"] { border-color:var(--ns-error); }
[class*="st-key-pipeline-card-status-partial-"], [class*="st-key-pipeline-card-status-blocked-"] { border-color:var(--ns-partial); }
[class*="st-key-pipeline-card-status-running-"] { border-color:var(--ns-running); }
[class*="st-key-pipeline-card-status-"][class*="-selected-"] { outline:3px solid var(--ns-accent); outline-offset:3px; }
@media (max-width: 1000px) {
  [class*="st-key-pipeline-cards-"] { grid-template-columns:minmax(0,1fr); gap:1rem; }
  [class*="st-key-pipeline-card-status-"][class*="-connector-"]::after { content:"↓"; top:auto; right:1rem; bottom:-1.15rem; }
  [class*="st-key-pipeline-card-status-"] button { min-height:2.75rem; }
  .nsp-pipeline { grid-template-columns:minmax(0,1fr); gap:1rem; }
  .nsp-pipeline li { flex-direction:row; text-align:left; gap:.75rem; }
  .nsp-pipeline li:not(:last-child)::after { content:"↓"; top:auto; right:auto; bottom:-1rem; left:1rem; }
  .nsp-state { margin-left:auto; }
  .nsp-compact { display:flex; flex-wrap:wrap; gap:.4rem; }
  .nsp-compact li { flex:1 1 10rem; gap:.4rem; }
  .nsp-compact li:not(:last-child)::after { content:none; }
}
@media (prefers-reduced-motion: reduce) {
  .nsp-pipeline *, .nsp-job { animation:none !important; transition:none !important; }
}
</style>
"""
_ICONS = {'completed':'✓', 'failed':'×', 'partial':'!', 'blocked':'!', 'running':'▶',
          'pending':'○', 'skipped':'—', 'not_started':'○', 'unknown':'?'}


def pipeline_html(stages, *, compact=False, selected=None):
    """Only application-owned names/statuses enter HTML; details never do."""
    from .ui_pipeline import STAGES, PipelineStage, stage_label
    rows = {stage.key: stage for stage in stages}
    nodes = []
    for index, (key, title) in enumerate(STAGES, 1):
        row = rows.get(key)
        status = row.status if row and row.status in _ICONS else 'unknown'
        label = stage_label(PipelineStage(key, title, status, ''))
        active = not compact and selected == key
        current = ' aria-current="step"' if active else ''
        highlight = ' nsp-selected' if active else ''
        marker = '<span class="nsp-selection">Selected step</span>' if active else ''
        nodes.append(f'<li data-stage="{key}" data-status="{status}"{current} class="nsp-{status}{highlight}">'
                     + marker +
                     f'<span class="nsp-icon" aria-hidden="true">{_ICONS[status]}</span>'
                     f'<span class="nsp-name">{index}. {title}</span>'
                     f'<span class="nsp-state">{label}</span></li>')
    mode = ' nsp-compact' if compact else ''
    return _PIPELINE_STYLE + f'<ol class="nsp-pipeline{mode}" aria-label="Saved review pipeline">' + ''.join(nodes) + '</ol>'


def _select_stage(key, stage):
    st.session_state[key] = stage


def start_walkthrough(review_key, workspace_key=None):
    """Session-only navigation; never starts or modifies a review."""
    st.session_state['pipeline-stage:' + review_key] = 'source'
    st.session_state['pipeline-walkthrough:' + review_key] = True
    if workspace_key:
        st.session_state[workspace_key] = 'Pipeline'


def _move_stage(key, offset):
    from .ui_pipeline import STAGES
    keys = [stage for stage, _ in STAGES]
    current = st.session_state.get(key, keys[0])
    index = keys.index(current) if current in keys else 0
    st.session_state[key] = keys[max(0, min(len(keys)-1, index+offset))]


def render_pipeline(stages, *, compact=False, details=None, review_key='unsaved', allow_walkthrough=False):
    from .ui_pipeline import stage_label, STAGES, PipelineStage
    rows={row.key:row for row in stages}
    stages=tuple(PipelineStage(key,title,rows[key].status if key in rows and rows[key].status in _ICONS else 'unknown',
        rows[key].detail if key in rows else 'Recorded stage unavailable.') for key,title in STAGES)
    if compact:
        st.html(pipeline_html(stages, compact=True))
        return
    with st.container(key='product-pipeline'):
        st.subheader('Review pipeline')
        st.caption('Evidence from this saved run · Not a live event stream.')
        selection_key = 'pipeline-stage:' + review_key
        walkthrough_key = 'pipeline-walkthrough:' + review_key
        labels = {stage.key: stage.title for stage in stages}
        if st.session_state.get('pipeline:active-review') != review_key:
            st.session_state['pipeline:active-review'] = review_key
            st.session_state[selection_key] = 'source'
            st.session_state[walkthrough_key] = False
        if st.session_state.get(selection_key) not in labels:
            st.session_state[selection_key] = 'source'
        walking = allow_walkthrough and st.session_state.get(walkthrough_key, False)
        if allow_walkthrough:
            if walking:
                st.caption('Saved review walkthrough — reconstructed from recorded results.')
                st.caption('Steps are an inspection order, not a historical timeline. Human review shows the current report-bound checkpoint. Nothing is executing.')
                keys = list(labels)
                index = keys.index(st.session_state[selection_key])
                with st.container(horizontal=True):
                    st.button('Previous stage', key=walkthrough_key+':previous', disabled=index==0,
                              on_click=_move_stage, args=(selection_key,-1))
                    st.button('Next stage', key=walkthrough_key+':next', disabled=index==len(keys)-1,
                              on_click=_move_stage, args=(selection_key,1))
                    st.button('End walkthrough', key=walkthrough_key+':end',
                              on_click=_select_stage, args=(walkthrough_key,False))
                st.caption(f'Step {index+1} of {len(keys)} · '+labels[st.session_state[selection_key]])
            else:
                st.button('Start walkthrough', key=walkthrough_key+':start',
                          on_click=start_walkthrough, args=(review_key,))
        st.html(_PIPELINE_STYLE)
        st.caption('Select a stage card to inspect its recorded outcome. Selection does not mean execution is running.')
        with st.container(horizontal=True, key='pipeline-cards-'+review_key):
            for item in stages:
                status=item.status if item.status in _ICONS else 'unknown'
                active=st.session_state[selection_key]==item.key
                panel=f'pipeline-card-status-{status}-'+('selected-' if active else 'idle-')+item.key+('-connector-' if item.key!=stages[-1].key else '-terminal-')+review_key
                with st.container(key=panel):
                    st.button(item.title, key=selection_key+':'+item.key, width='stretch',
                        help=stage_label(item), on_click=_select_stage, args=(selection_key,item.key))
                    st.caption(_ICONS[status]+' '+stage_label(item))
                    if active: st.caption('Selected stage')
        with st.expander('Advanced · pipeline navigation and interpretation'):
            st.text('Unavailable means the record or checkpoint lookup cannot establish the stage. Not run means execution was explicitly skipped. Preparation is not analysis; a completed zero-finding review does not prove defect-free code.')
            st.text('Report saved refers to durable storage. Findings and the sanitized export contain the report; human decisions remain separate local report decisions.')
            selected = st.selectbox('Select pipeline stage', list(labels), format_func=labels.__getitem__, key=selection_key)
        stage = next(item for item in stages if item.key == selected)
        with st.container(border=True, key='theme-panel-stage-'+review_key):
            st.subheader('Recorded stage details')
            st.caption('Terminal facts and current local checks, not chronological logs or live provider events.')
            st.text(stage.title+' · '+stage_label(stage))
            st.text(stage.detail)
            record = details.get(selected) if details else None
            if record:
                st.caption('Evidence source: '+record.evidence_source)
                for subject in record.subjects: st.text(subject)
                for name, value in record.counts:
                    st.text(name+': '+('Unavailable' if value is None else str(value)))
                for diagnostic in record.diagnostics: st.text(diagnostic)
                with st.expander('Missing telemetry and interpretation'):
                    for note in record.unavailable: st.text(note)
            else:
                st.caption('Evidence source: stage projection only; additional sanitized telemetry unavailable.')


def job_status_html(status):
    status = status if status in _ICONS else 'unknown'
    labels = {'completed':'PASSED', 'failed':'FAILED', 'partial':'PARTIAL', 'running':'RUNNING',
              'skipped':'NOT RUN', 'not_started':'NOT RUN', 'pending':'PENDING',
              'blocked':'BLOCKED', 'unknown':'UNAVAILABLE'}
    return (_PIPELINE_STYLE + f'<span class="nsp-job nsp-{status}" role="status">'
            f'<span class="nsp-icon" aria-hidden="true">{_ICONS[status]}</span>{labels[status]}</span>')


def render_test_jobs(bundle):
    """Execution outcomes, fixture judgments and finding severity are independent."""
    from .ui_models import present_review
    view = present_review(bundle)
    scripted = bundle.provenance == 'scripted_integration'
    if scripted:
        st.markdown('**Harness execution**')
        st.html(job_status_html('completed' if bundle.evaluation and bundle.report else 'unknown'))
        st.caption('PASSED here means the harness returned a validated report and evaluation. It does not mean every specialist completed or every defect claim is correct.')
        if bundle.evaluation:
            judgments = bundle.evaluation.findings
            matches = sum(a.disposition == 'match' and a.judgment_origin == 'scripted_fixture' for a in judgments)
            false_positives = sum(a.disposition == 'false_positive' and a.judgment_origin == 'scripted_fixture' for a in judgments)
            st.caption(f'Scripted fixture judgments: {matches} expected matches · {false_positives} false-positive test cases. These are not a test-runner pass count or live-model accuracy.')
    else:
        st.caption('Recorded specialist outcomes, not offline test assertions. No inference runs here.')
    st.caption('Specialist PASSED means its review workflow completed. Severity is shown separately; finding a problem does not fail a test.')
    if bundle.report:
        st.caption(f'Critical findings: {bundle.report.severity_counts.critical}')
    with st.container(horizontal=True, gap='small'):
        for row in view.specialists:
            with st.container(border=True, width=290, key='theme-panel-job-'+row.agent):
                st.markdown('**' + SPECIALISTS[row.agent][0] + '**')
                status = row.status if row.selected else ('skipped' if bundle.report else 'unknown')
                st.html(job_status_html(status))
                st.caption('Review completion: ' + row.status.replace('_',' '))
                findings = [f for f in view.findings if f.specialist == row.name]
                st.caption(f'Evidence-validated findings: {len(findings)}' if row.selected else 'Not selected for this scenario')
                for severity in ('critical','high','medium','low'):
                    count = sum(f.severity == severity for f in findings)
                    if count:
                        themed_badge(f'{severity.title()} severity: {count}', color={'critical':'red','high':'orange','medium':'yellow','low':'blue'}[severity], key='job-'+row.agent+'-'+severity)


def specialist_cards(report=None, *, configuring=False, capabilities=False):
    """Five identities; actual selection/status only, no invented agent activity."""
    rows={s.agent_name.value:s for s in report.specialists} if report else {}
    context = 'capabilities' if capabilities else ('configuring' if configuring else 'report')
    with st.container(horizontal=True, gap='small'):
        for key,(name,purpose,scope) in SPECIALISTS.items():
            row=rows.get(key)
            selected=bool(row and row.selected)
            with st.container(border=True,width=290,key='theme-panel-specialist-'+context+'-'+key):
                st.markdown('**'+name+'**')
                st.caption(purpose)
                if capabilities:
                    st.text('Implemented · Offline tested')
                    st.caption('Live real-source workflow: supported' if key=='code_quality' else 'Live real-source workflow: not available')
                elif configuring:
                    st.text('Selected · Ready to configure' if key=='code_quality' else 'Not selected · Unsupported in live source workflow')
                elif report is None:
                    st.text('Selection unavailable · Execution unavailable')
                    st.caption('Findings: unavailable')
                else:
                    st.text(('Selected · '+row.status.value.replace('_',' ').title()) if selected else 'Not selected · Did not execute in this review')
                    st.caption('Evidence-validated findings: '+str(row.finding_count) if selected else 'Findings: not applicable')
                st.caption('Scope: '+scope)
                if row and row.limitations:
                    with st.expander('Limitations · '+name):
                        for note in row.limitations: st.text(note)
