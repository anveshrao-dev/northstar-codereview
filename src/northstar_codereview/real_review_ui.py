"""Explicit UI events only. No credentials, source or providers on page load."""
import asyncio
from uuid import UUID, uuid4

import streamlit as st

from . import real_review as service
from .real_source import renew_import
from .real_source_contracts import SampleId, MODEL
from .models import AgentName
from .execution_profiles import PROFILE_NAMES, get_profile
from .transmission_claims import ensure_unconsumed
from .ui_models import ReviewBundle
from .ui_service import LocalReview
from .ui_style import render_pipeline, specialist_cards, start_walkthrough, themed_badge
from .ui_pipeline import pipeline_stages, recorded_stage_details

NAMES = {'portfolio_risk': 'Portfolio Risk', 'csv_import': 'CSV Import and Validation',
         'bm25_retrieval': 'BM25 Retrieval'}


def _selection():
    sample = st.selectbox('Choose an approved source snapshot', list(NAMES),
        format_func=lambda name: 'Imported source sample — '+NAMES[name])
    try:
        choices = service.snapshot_catalog(sample)
    except Exception:
        st.error('Approved sources are unavailable. Check local storage before continuing.')
        return None
    if not choices:
        st.info('No approved source is available for this sample. Import it through the existing authorized intake workflow first.')
        return None
    selected = st.selectbox('Imported snapshot', [str(c.import_id) for c in choices],
        format_func=lambda value: next(f'Version {i+1} · {len(c.files)} files · {"Available" if c.usable else "Expired / unavailable"}'
                                     for i,c in enumerate(choices) if str(c.import_id)==value))
    choice = next(c for c in choices if str(c.import_id)==selected)
    return sample,choice,f'real:{selected}:{choice.manifest_digest}'


def render_retention():
    selection=_selection()
    if selection is None: return
    sample,choice,key=selection
    st.caption('Retain until: '+choice.retain_until.isoformat())
    st.caption('Replacement preserves exact source bytes and the previous snapshot. It does not extend transmission permission.')
    days=st.number_input('Retention days',min_value=1,max_value=365,value=30,key=key+':days')
    renew=st.checkbox('I authorize a local retention replacement; no transmission approval is extended.',key=key+':renew-consent')
    if st.button('Renew local retention',disabled=not renew,key=key+':renew'):
        try:
            renew_import(choice.import_id,expected_digest=choice.manifest_digest,days=int(days),allow_renewal=True)
            st.success('Retention replacement created. Select the new version in New Review.')
        except Exception:
            st.error('Retention replacement rejected. No authorization was extended.')


def render_real_workspace():
    st.title('New review')
    st.caption('Review an approved, frozen copy of your code. Nothing is sent until you approve sharing and start the review.')
    st.caption('Starting an authorized review of an existing snapshot creates a new execution. A consumed transmission approval cannot be reused. Expired snapshots require supported retention renewal and fresh authorization.')
    with st.container(horizontal=True):
        for index,label in enumerate(('1 · Choose code', '2 · Select reviewer', '3 · Approve code sharing', '4 · Run review')):
            themed_badge(label, color='gray', key='setup-'+str(index))
    source,profile=st.columns(2,gap='large')
    with source:
        st.subheader('1. Choose code')
        st.caption('An imported snapshot is a saved copy of approved files. It stays frozen so every finding refers to the same code, even if the original project changes. Selecting it sends nothing.')
        st.caption('These are source-code examples, not CSV datasets or GitHub pull requests.')
        selection=_selection()
    if selection is None: return
    sample,choice,key=selection
    result_key=key+':result'  # Historical navigation is per import, never authorization.
    with profile:
        st.subheader('2. Select reviewer')
        with st.container(border=True,key='theme-panel-source-choice'):
            st.subheader('Imported source sample — '+NAMES[sample])
            st.text(f'{len(choice.files)} approved files · '+('Available' if choice.usable else 'Unavailable'))
            st.selectbox('Specialist profile',['Code Quality'],disabled=True,help='Only Code Quality is supported by this live source workflow.')
            st.caption('Code Quality evaluates behavior and maintainability using observed source. It does not execute your code or prove defect absence.')
            with st.expander('Advanced · execution profile and limits'):
                profile_name=st.selectbox('Execution profile',PROFILE_NAMES,key=key+':profile-selection',
                    help='Explicit resource ceilings; names describe evaluated workload sizes, not permitted source samples.')
                selected_profile=get_profile(profile_name)
                st.caption(f'{selected_profile.max_model_requests} model attempts · {selected_profile.max_tool_attempts} tool attempts · {selected_profile.max_input_tokens:,} input units · {selected_profile.evaluation_timeout_seconds}s deadline')
                st.caption('30,000 synthesis hold · 4,000 output reservations · 1,000 output tokens/request · 20 s request timeout · no retries or repairs.')
                st.caption('Limits are ceilings, not promised usage or a cost quote. Extended profiles require explicit selection and new authorization.')
            previous=st.session_state.get(key+':active-profile')
            if previous is not None and previous!=profile_name:
                for candidate in PROFILE_NAMES:
                    for suffix in (':permit',':consent',':live-consent'):
                        st.session_state.pop(key+':profile:'+candidate+suffix,None)
            st.session_state[key+':active-profile']=profile_name
            key=key+':profile:'+profile_name
            st.caption('Selected execution profile: '+profile_name+' · Provider: Nebius Token Factory')
            st.caption(service.SCOPE_NOTES[SampleId(sample)])
            with st.expander('Source files and technical details'):
                st.caption('Only these approved snapshot files may be transmitted with review instructions and context:')
                for path in choice.files: st.text(path)
                st.text(f'Snapshot: {choice.snapshot_id}\nManifest: {choice.manifest_digest}\nRetain until: {choice.retain_until.isoformat()}')
                st.text('Required model: '+MODEL)
    with st.expander('Specialist availability · five capabilities, one supported live specialist'):
        specialist_cards(configuring=True)
    if not choice.usable:
        st.warning('This source is unavailable or expired. Manage retention in Settings before starting a review.')
    with st.container(width=920):
        st.subheader('3. Approve code sharing')
        st.info('External disclosure: selected source and conversation context will be sent to Nebius. Confirm you have permission to share this code. Local import does not grant transmission permission.')
        consent = st.checkbox('I authorize disclosure of this exact snapshot to Nebius for code review.', key=key+':consent')
        st.caption('Transmission approval is for Code Quality only. Legacy approvals without specialist scope require fresh explicit authorization.')
        if st.button('Authorize model transmission', disabled=not consent or not choice.usable, key=key+':authorize'):
            try:
                permit = service.authorize_transmission(sample, choice.import_id,
                    expected_digest=choice.manifest_digest, allow_transmission=True, execution_profile=profile_name)
                st.session_state[key+':permit'] = str(permit.approval_id)
                st.success('Transmission approval recorded for up to 30 minutes, bounded by retention. No inference has started.')
            except Exception:
                st.error('Transmission authorization rejected; no inference started.')
        permit_id = st.session_state.get(key+':permit')
        if permit_id:
            try:
                permit = service.read_transmission(choice.import_id, UUID(permit_id), specialists=(AgentName.CODE_QUALITY,), execution_profile=profile_name)
                ensure_unconsumed(permit)
                st.caption('Transmission approval valid until ' + permit.expires_at.isoformat())
            except Exception:
                permit_id = None
                st.warning('Transmission approval is consumed, expired or unavailable; a fresh explicit approval is required.')
        st.subheader('4. Run review')
        st.caption('Sharing approval permits this code to leave the local workspace. Execution approval starts one bounded review. These are separate choices; authorizing transmission alone does not start a review.')
        live_consent = st.checkbox('I authorize one bounded live review now.', key=key+':live-consent')
        if st.button('Start live real-code review', disabled=not (choice.usable and permit_id and live_consent), key=key+':start', type='primary'):
            with st.status('Review attempt running · waiting for a durable outcome', expanded=True) as progress:
                st.write('Authorization, preparation and analysis happen inside the existing guarded service. Intermediate stage events are unavailable; no live percentage or current stage is inferred.')
                st.caption('Do not start another attempt. The single-use authorization remains consumed if execution fails after claiming it.')
                try:
                    output = asyncio.run(service.run_review(sample, choice.import_id, transmission_id=UUID(permit_id),
                        run_id=uuid4(), allow_live=True, execution_profile=profile_name))
                    st.session_state[result_key] = output
                    progress.update(label='Review attempt finished: ' + output.status, state='complete' if output.status == 'completed' else 'error')
                except Exception:
                    progress.update(label='Review could not start or finish; details withheld.', state='error')
                    st.error('Review unavailable. Do not retry automatically; inspect the private run state.')
    output=st.session_state.get(result_key)
    if output is not None:
        st.divider()
        if output.persisted:
            st.button('Open in Review History',on_click=_open_history,args=(choice.import_id,output.run_id))
        render_real_result(choice.import_id,output)


def _open_history(import_id,run_id):
    st.session_state['history:selected']=(str(import_id),str(run_id))
    st.session_state['navigation']='Review History'


def saved_pipeline_state(output):
    """One report-bound lookup for mini and detail views; never creates a checkpoint."""
    from .approval_models import ApprovalError
    human_view = None
    missing = False
    if output.report:
        try:
            local = LocalReview()
            selected = st.session_state.get('approval:' + output.report.report_id)
            thread = selected.thread_id if selected is not None else local.thread_for(output.report)
            human_view = local.get(thread, output.report)
        except ApprovalError as error:
            missing = error.code == 'missing_checkpoint'
        except Exception:
            pass  # Unreadable/unverified state is unavailable, never not-started.
    return (pipeline_stages(output, human_review=human_view, human_review_missing=missing),
            human_view.outcome if human_view else None)


def render_source_viewer(import_id, run_id):
    from .ui_source import render_real_source
    workspace=f'review-workspace:result:{import_id}:{run_id}'
    render_real_source(import_id,run_id,workspace)


def render_real_result(import_id, output):
    sample=output.sample.value
    selected=str(import_id)
    key=f'result:{selected}:{output.run_id}'
    if output.category:
        st.error('This review did not finish successfully. No automatic retry was attempted.')
        with st.expander('Technical details · failure'):
            st.text('Review diagnostic: '+output.category)
    if output.capacity is not None:
        a=output.capacity
        st.text('Capacity limits: '+', '.join(a.denial_reasons))
        st.text(f'Projected input: {sum(a.projected_input_units)} / {a.available_input_units} units; protected final allowance: {a.protected_input_units}.')
        st.warning(f'Complete review cannot fit this profile: {a.required_tool_attempts} source-tool attempts required / {a.available_tool_attempts} available; {a.required_model_attempts} model attempts required / {a.available_model_attempts} available. No inference was sent. Approval remains consumed.')
    if not output.persisted:
        render_pipeline(pipeline_stages(output), review_key=key)
        st.warning('No durable report was saved for this attempt.')
        return
    try:
        output = service.load_review(import_id, output.run_id)
    except Exception:
        st.error('Stored review failed integrity or retention checks; viewing and export are blocked.')
        return
    st.subheader('Imported source sample — '+NAMES[sample])
    st.caption('Reviewed source: imported demonstration source code · Frozen approved snapshot')
    st.caption('A source snapshot is a frozen approved set of files. Opening this saved report does not rerun analysis or authorize a new execution.')
    if output.report and output.status != output.report.status:
        st.warning('Report completion does not establish successful execution cleanup. The report below retains its graph outcome; the overall run remains ' + output.status + '.')
    bundle = ReviewBundle(scenario_id=sample, provenance='real_source_live', report=output.report,
        run_status=output.status, model_attempts=output.usage.model_attempts, tool_attempts=output.usage.tool_attempts,
        input_tokens=output.usage.reported_input_tokens, output_tokens=output.usage.reported_output_tokens,
        unknown_usage=output.usage.unknown_usage_attempts, elapsed_seconds=output.elapsed_seconds,
        provider=output.provider if 'provider' in output.model_fields_set else None,
        model=output.model if 'model' in output.model_fields_set else None)
    from .streamlit_ui import _results
    summary_slot,pipeline_slot,source_slot,citations_slot = _results(bundle, review_key=key)
    with source_slot:
        render_source_viewer(import_id, output.run_id)
    with st.expander('Technical details · run'):
        if output.execution_profile is not None:
            p=output.execution_profile
            st.text(f'Execution profile: {p.name} · {p.max_model_requests} model attempts · {p.max_tool_attempts} tools · {p.max_input_tokens:,} input units · {p.evaluation_timeout_seconds}s deadline')
        st.text('Run ID: ' + str(output.run_id))
        st.text('Private artifact: .runtime/real_source/imports/' + selected + '/review_artifacts/' + str(output.run_id) + '.json')
        st.text('Overall execution: ' + output.status + ' · Provider cleanup: ' + output.cleanup)
    export_slot=st.container()
    if output.report:
        with citations_slot, st.expander('Inspect exact cited source locally'):
            st.caption('Only cited ranges are displayed on request. Source is untrusted text and is never executed. Exports omit these excerpts.')
            for finding in output.report.findings:
                for index, ref in enumerate(finding.evidence):
                    if st.button(f'Show evidence: {ref.file_path}:{ref.start_line}–{ref.end_line}', key=f'{key}:{output.run_id}:{finding.finding_id}:{index}'):
                        try:
                            from .ui_source import numbered_source
                            st.code(numbered_source(service.evidence_excerpt(import_id, output.run_id, finding.finding_id, index),ref.start_line), language='text')
                        except Exception:
                            st.error('Evidence unavailable or identity verification failed.')
    try:
        stages,decision = saved_pipeline_state(output)
        with summary_slot:
            st.button('Walk through this review', key=key+':walkthrough',
                      on_click=start_walkthrough, args=(key,'review-workspace:'+key))
            render_pipeline(stages, compact=True)
            human = stages[-1]
            if human.status == 'completed':
                st.caption('Next action: read the recorded decision in Human Review before using or sharing this report.')
            elif human.status == 'unknown':
                st.caption('Next action: inspect Human Review; local decision state is unavailable.')
            else:
                st.caption('Next action: open Human Review to start or resume a local report decision. Nothing is approved automatically.')
        with pipeline_slot:
            render_pipeline(stages, details=recorded_stage_details(output), review_key=key, allow_walkthrough=True)
        data = service.export_review(import_id, output.run_id, decision=decision)
        with export_slot:
            st.download_button('Export sanitized prioritized report', data=data,
                file_name=f'review-{output.run_id}.json', mime='application/json', key=key+':export')
            st.caption('Human report review: '+(decision.decision.replace('_',' ') if decision else 'Not recorded')+' · Individual findings remain unconfirmed')
    except Exception:
        st.error('Export unavailable; no unvalidated data was exported.')
