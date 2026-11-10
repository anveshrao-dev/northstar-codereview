"""Local Streamlit presentation with separately guarded real-source execution."""
from uuid import UUID

import streamlit as st
from langsmith import tracing_context

from .ui_models import AGENT_NAMES, SCENARIOS, SCENARIO_NAMES, present_review, activity_label
from .ui_service import LocalReview, run_baseline, saved_live, pinned_dataset
from .ui_style import navigation, workspace, plain_title, specialist_cards, render_pipeline, render_test_jobs, job_status_html, themed_badge


def _value(value):
    return 'Unavailable' if value is None else str(value)


DECISIONS = {'approve': 'Accept report', 'reject': 'Reject report', 'request_changes': 'Request follow-up'}


def _provenance(provenance):
    if provenance == 'scripted_integration':
        st.info('SCRIPTED INTEGRATION · Predetermined responses exercise real graphs and tools. These results are not live Nebius performance or independent model accuracy.')
    elif provenance == 'real_source_live':
        st.info('REAL-SOURCE NEBIUS REVIEW · Unverified model claims. Human review required.')
    else:
        st.info('RECORDED NEBIUS LIVE RESULT · Historical local artifact. No inference runs here. Integrity hashes are not provenance signatures.')


def _open_review_section(key, section):
    st.session_state[key] = section


def _approval(bundle, workspace_key=None):
    st.subheader('Human review')
    st.subheader('1. Report summary')
    view = present_review(bundle)
    selected = [row for row in view.specialists if row.selected]
    overall = bundle.run_status if bundle.provenance == 'real_source_live' else view.status
    st.text('Review status: '+overall.replace('_',' ').title())
    st.text('Selected specialist'+('s' if len(selected)!=1 else '')+': '+(', '.join(row.name for row in selected) or 'Unavailable'))
    for row in selected:
        d = row.diagnostics
        if d and d.analysis:
            st.text(f'Validated analyzed coverage: {d.analysis.analyzed_lines}/{d.analysis.planned_lines} lines · {row.name}')
        else:
            st.text('Validated analyzed coverage: Unavailable · '+row.name)
        if d and not d.analysis:
            st.caption(f'Legacy observed source: {d.observed_lines}/{d.total_lines} lines; not validated analyzed coverage.')
    if view.findings:
        st.text(f'Evidence-validated findings: {len(view.findings)} · defect claims remain unverified.')
    else:
        st.info('No evidence-validated findings were retained. This does not establish defect-free code or a successful negative control.')
    st.caption('Review limitations · inspect these before recording a report decision.')
    if overall != 'completed':
        st.warning('Incomplete review. A local decision does not complete missing analysis.')
    if view.limitations:
        with st.expander('Read report limitations'):
            for note in view.limitations: st.text(note)
    else:
        st.caption('No additional report-level limitations recorded; model claims still require independent assessment.')
    st.subheader('2. Inspect evidence')
    st.caption('Open Source Files or Findings before deciding. Viewing saved evidence sends nothing to a model and does not rerun analysis.')
    if workspace_key:
        with st.container(horizontal=True):
            st.button('Inspect source files', key=workspace_key+':human-source',
                on_click=_open_review_section, args=(workspace_key,'Source Files'))
            st.button('Inspect findings and evidence', key=workspace_key+':human-findings',
                on_click=_open_review_section, args=(workspace_key,'Findings'))
    st.subheader('3. Make a local decision')
    st.caption('Local review only · No GitHub publication.')
    st.write('Accept report accepts it for local use. Request follow-up records that revisions are needed; it does not rerun agents. Reject report declines it.')
    st.write('This report-level decision does not approve a GitHub pull request, confirm defect correctness, modify source or publish anything.')
    report = bundle.report
    if report is None or report.status not in ('completed', 'partial'):
        st.write('No eligible report is available for local approval.')
        return
    service = LocalReview()
    key = report.report_id
    state_key = f'approval:{key}'
    st.caption('Start a local review of this exact report. No decision is made automatically.')
    if st.button('Start human review', key=f'create:{key}'):
        try:
            st.session_state[state_key] = service.start(report)
        except Exception:
            st.error('Checkpoint creation was rejected. No approval was granted.')
    with st.expander('Advanced · checkpoint and resume') as advanced:
        st.text(f'Report: {report.report_id}\nRevision: {report.report_revision}')
        st.caption('The local operator is not an authenticated production identity. Checkpoints are bound to this exact report and revision.')
        checkpoint = st.text_input('Checkpoint ID', value=str(service.thread_for(report)), key=f'thread:{key}')
        st.caption('Paste a checkpoint ID for this report. A different report or revision cannot be substituted.')
        if st.button('Load existing checkpoint', key=f'load:{key}'):
            try:
                st.session_state[state_key] = service.get(UUID(checkpoint), report)
            except Exception:
                st.error('Checkpoint unavailable or does not match this report.')
    view = st.session_state.get(state_key)
    if view is None:
        st.write('Manual status: no checkpoint loaded. No decision has been made here.')
        return
    # Refresh durable state before presenting actions; concurrent decisions win.
    try:
        view = service.get(view.thread_id, report)
        st.session_state[state_key] = view
    except Exception:
        st.error('Checkpoint validation failed; decisions are disabled.')
        return
    st.text(f'Human report review: {view.status}')
    with advanced:
        st.text(f'Checkpoint: {view.thread_id}')
    if view.outcome:
        st.success('Local decision recorded. Nothing was published.')
        st.text(f'Recorded decision: {DECISIONS[view.outcome.decision]}\nRecorded: {view.outcome.recorded_at.isoformat()}')
        with advanced:
            st.text(f'Manual decision: {view.outcome.decision}\nScope: local report review')
        st.caption('Finding evidence and unverified claim status remain unchanged.')
        return
    decision = st.selectbox('Local report decision', ('reject', 'request_changes', 'approve'),
                           format_func=DECISIONS.__getitem__, index=None, placeholder='Choose a local decision', key=f'decision:{key}')
    partial = report.status == 'partial'
    acknowledged = st.checkbox('I acknowledge that this review is incomplete.', key=f'partial:{key}') if partial else False
    if st.button('Record manual decision', key=f'submit:{key}',
                 disabled=view.status != 'pending' or decision not in DECISIONS or (partial and not acknowledged)):
        if decision not in DECISIONS:
            return
        try:
            updated = service.submit(view.thread_id, report, decision, acknowledge_partial=acknowledged)
            st.session_state[state_key] = updated
            # Re-render status from the durable outcome, removing stale pending text.
            st.rerun()
        except Exception:
            st.error('Decision rejected; refresh the checkpoint before trying again.')


def _results(bundle, *, review_key=None):
    view = present_review(bundle)
    overall=bundle.run_status if bundle.provenance=='real_source_live' else view.status
    selected=[row for row in view.specialists if row.selected]
    if bundle.provenance != 'real_source_live':
        st.subheader('Reviewed source — '+SCENARIO_NAMES[bundle.scenario_id])
    st.text('Source type: '+('Imported source snapshot' if bundle.provenance=='real_source_live' else 'Scripted fixture' if bundle.provenance=='scripted_integration' else 'Fictional evaluation snapshot'))
    if bundle.provenance=='scripted_integration':
        st.text('Provider: None · Model responses: predetermined offline script')
    else:
        st.text('Recorded provider: '+(bundle.provider or 'Unavailable')+' · Recorded model: '+(bundle.model or 'Unavailable'))
    st.caption('Selected specialists: '+(', '.join(row.name for row in selected) or 'Unavailable'))
    top = st.columns(3)
    top[0].metric('Review status', (bundle.run_status if bundle.provenance=='real_source_live' else view.status).replace('_',' ').title())
    top[1].metric('Evidence-validated findings', len(view.findings))
    d=selected[0].diagnostics if len(selected)==1 else None
    top[2].metric('Analyzed coverage' if d and d.analysis else 'Source coverage',
        f'{d.analysis.analyzed_lines}/{d.analysis.planned_lines} lines' if d and d.analysis else
        f'{d.fully_inspected_files}/{d.total_files} files' if d else 'See specialists')
    identity = review_key or (bundle.provenance+':'+(view.report_id or bundle.scenario_id))
    workspace_key = 'review-workspace:'+identity
    if st.session_state.get('workspace:active-review') != identity:
        st.session_state['workspace:active-review'] = identity
        st.session_state[workspace_key] = 'Summary'
    with st.container(horizontal=True):
        for label,section in (('View Pipeline','Pipeline'),('View Source Files','Source Files'),('Inspect Findings','Findings')):
            st.button(label, key=workspace_key+':open:'+section,
                on_click=_open_review_section, args=(workspace_key,section))
    _provenance(view.provenance)
    if overall != 'completed':
        st.warning('Incomplete review. Empty findings do not establish a clean result or a passed negative control.')
    elif not view.findings:
        st.caption('Completed analysis does not establish defect-free code. No defect claim was retained.')
    with st.expander('Coverage and review context'):
        if d:
            st.caption(f'Prepared coverage: {d.analysis.prepared_lines}/{d.analysis.planned_lines} lines' if d.analysis else f'Observed source coverage: {d.observed_lines}/{d.total_lines} lines · separate prepared/analyzed telemetry unavailable')
        if overall!='completed':
            if any(row.synthesis=='malformed_output' for row in selected):
                st.write('The final response did not match the required format. No replacement findings were invented.')
            elif any(row.synthesis=='budget_denied' for row in selected):
                st.write('The execution budget could not admit final synthesis.')
            elif any(row.diagnostics and row.diagnostics.fully_inspected_files<row.diagnostics.total_files for row in selected):
                st.write('Some source lines were not inspected. Coverage remains incomplete.')
            else:
                st.write('A review step could not finish or validate. See Technical details before making a decision.')
        st.caption('Prepared or observed source is separate from validated model analysis. Evidence validation does not prove defect correctness.')
    st.subheader('Who reviewed this code?')
    st.caption('Recorded selection and execution, not automatic AI delegation. Only Code Quality supports live imported-source review; the other capabilities have offline scripted coverage.')
    with st.container(horizontal=True,gap='small'):
        for row in view.specialists:
            with st.container(border=True,width=230,key='theme-panel-activity-'+workspace_key+'-'+row.agent):
                st.text(row.name+' · '+activity_label(row)+(' · Offline scripted capability' if row.agent!='code_quality' else ''))
                if row.selected:
                    st.caption('Recorded outcome: '+row.status.replace('_',' ')+' · Model attempts: '+_value(row.model_attempts)+' · Tool attempts: '+_value(row.tool_attempts))
    if bundle.provenance=='scripted_integration':
        st.caption('Specialist selection follows the predefined scenario and deterministic applicability rules.')
    summary, pipeline, findings, specialists, source, approval, evaluation = st.tabs(
        ['Summary', 'Pipeline', 'Findings', 'Coverage', 'Source Files', 'Human Review', 'Technical Details'],
        key=workspace_key, on_change='rerun')
    with summary:
        st.subheader('Review summary')
        st.button('Open Human Review', key=workspace_key+':open:Human Review',
            on_click=_open_review_section, args=(workspace_key,'Human Review'))
        summary_slot = st.container()
        if bundle.provenance != 'real_source_live':
            st.caption('See Pipeline for recorded specialist job outcomes, Findings for claims, and Human Review for local decisions. No finding is approved automatically.')
        if view.limitations:
            with st.expander('Review limitations'):
                for note in view.limitations: st.text(note)
    with pipeline:
        pipeline_slot = st.container()
        if bundle.provenance != 'real_source_live':
            with pipeline_slot:
                st.subheader('Specialist jobs')
                render_test_jobs(bundle)
    with specialists:
        st.subheader('Specialist activity')
        st.subheader('Review coverage')
        st.caption('Inspected source is not proof of understanding or defect correctness.')
        for row in selected:
            with st.container(border=True,key='theme-panel-coverage-'+workspace_key+'-'+row.agent):
                st.markdown('**' + row.name + '**')
                st.text(f'{row.status.replace("_"," ").title()} · synthesis: {row.synthesis}')
                if row.diagnostics:
                    d=row.diagnostics
                    if d.analysis:
                        a=d.analysis
                        st.text(f'Validated model analysis: {a.analyzed_lines}/{a.planned_lines} lines · Prepared: {a.prepared_lines} · Failed: {a.failed_lines} · Skipped: {a.skipped_lines}')
                        st.caption('Validated analysis is not proof of defect correctness or absence.')
                    st.text(f'Observed source: {d.fully_inspected_files}/{d.total_files} files fully inspected · {d.observed_lines}/{d.total_lines} lines')
                else:
                    st.caption(f'{len(row.files)} files recorded as inspected. Detailed line coverage unavailable.')
                with st.expander('Inspection details · ' + row.name):
                    for path in row.files: st.text(path)
                    if not row.files: st.text('No inspected files recorded.')
                    st.text(f'Model calls: {_value(row.model_attempts)} | Tool attempts: {_value(row.tool_attempts)} | Successful tools: {_value(row.tool_successes)} | Recovered: {_value(row.recovered)} | Unresolved: {_value(row.unresolved)}')
                    if row.diagnostics:
                        d=row.diagnostics
                        if d.preparation:
                            p=d.preparation
                            st.text(f'Prepared context: {p.read_attempts} tool attempts · {p.context_units} reservation units · proposals: {_value(p.proposal_count)} · rejected: {p.rejected_proposals}')
                        if d.synthesis_context: st.text('Synthesis budget admission: '+d.synthesis_context.admission)
                        if d.final_response: st.text('Final response: '+d.final_response.finish_reason+' · failure stage: '+d.final_response.failure_stage)
                    for note in row.limitations: st.text(note)
        if not selected: st.info('No specialist results were retained.')
    with source:
        source_slot = st.container()
        if bundle.provenance == 'scripted_integration':
            from .ui_source import render_scripted_source
            with source_slot:
                render_scripted_source(bundle,workspace_key)
        elif bundle.provenance == 'recorded_live':
            st.info('Local source navigation is unavailable for this historical fictional result. No imported source is substituted.')

    with findings:
        st.subheader('Prioritized findings')
        st.caption('Prioritized by severity. Exact citations validate evidence, not defect correctness. Findings remain unconfirmed until independently assessed.')
        st.caption('Human decisions apply to the whole report. Individual-finding confirmation or rejection is not supported.')
        if not view.findings:
            st.info('No evidence-validated findings were retained. Rejected proposals are never promoted here.')
        if bundle.report:
            counts=bundle.report.severity_counts
            with st.container(horizontal=True):
                for severity in ('critical','high','medium','low'):
                    themed_badge(severity.title()+': '+str(getattr(counts,severity)), color={'critical':'red','high':'orange','medium':'yellow','low':'blue'}[severity], key=workspace_key+'-'+severity)
        for finding_index, finding in enumerate(view.findings):
            with st.container(border=True,key='theme-panel-finding-'+workspace_key+'-'+str(finding_index)):
                st.caption(f'{finding.severity.upper()} · {finding.specialist} · Model confidence {finding.confidence_percent:.0f}%')
                plain_title(finding.title, str(finding_index))
                st.caption('Category: ' + finding.category)
                for location in finding.locations: st.text(location)
                if bundle.provenance in ('real_source_live','scripted_integration'):
                    from .ui_source import request_citation
                    citation_index=0
                    if len(finding.locations)>1:
                        citation_index=st.selectbox('Evidence reference',range(len(finding.locations)),
                            format_func=lambda i:'Citation '+str(i+1),key=workspace_key+':citation-choice:'+str(finding.finding_id))
                    st.button('Inspect code',key=workspace_key+':inspect:'+str(finding.finding_id),
                        on_click=request_citation,args=(workspace_key,finding.finding_id,citation_index))
                if finding.scripted_case == 'false_positive':
                    st.warning('SCRIPTED FALSE-POSITIVE TEST CASE · Deliberately incorrect claim identified by a predeclared fixture judgment. The citation passed evidence checks; the defect claim did not pass semantic evaluation. This is not a Nebius model failure.')
                elif finding.scripted_case == 'match':
                    st.caption('SCRIPTED EXPECTED MATCH · Matches a predeclared test assertion; not independently established model accuracy.')
                st.text('Evidence: ' + finding.evidence_status + ' | Claim: ' + finding.claim_status)
                with st.expander(f'Advanced · exact source excerpts · {len(finding.locations)} location(s)'):
                    for location in finding.locations:
                        st.text(location)
                    if bundle.provenance == 'scripted_integration':
                        from .ui_service import scripted_evidence
                        for index, location in enumerate(finding.locations):
                            if st.button('Show fictional source: '+location, key=f'fictional-source:{view.report_id}:{finding.finding_id}:{index}'):
                                try:
                                    from .ui_source import numbered_source
                                    ref=next(f for f in bundle.report.findings if f.finding_id==finding.finding_id).evidence[index]
                                    st.code(numbered_source(scripted_evidence(bundle,finding.finding_id,index),ref.start_line), language='text')
                                    st.caption('Pinned fictional source · Scripted demonstration, not a live Nebius discovery.')
                                except Exception:
                                    st.warning('Fictional evidence unavailable or failed pinned-source validation.')
                st.text(finding.explanation)
                st.markdown('**Suggested remediation**')
                st.text(finding.remediation)
                if finding.assumptions:
                    with st.expander('Claim assumptions and uncertainty'):
                        for assumption in finding.assumptions:
                            st.text(assumption)
        citations_slot = st.container()
    with approval:
        _approval(bundle, workspace_key)
    with evaluation:
        with st.expander('Technical details · execution and evaluation'):
            st.subheader('Evaluation context')
            st.write('Evidence validation checks captured source identity, ranges and exact quotations. Semantic evaluation asks whether the claimed defect is correct. A negative control is a predefined safe case that should not be flagged.')
            st.text(f'Independent model accuracy established: No\nPending semantic judgments: {_value(view.pending_judgments)}')
            st.text('Elapsed: ' + ('Unavailable' if bundle.elapsed_seconds is None else f'{bundle.elapsed_seconds:.2f} s'))
            st.text(f'Reported input tokens: {_value(bundle.input_tokens)}\nReported output tokens: {_value(bundle.output_tokens)}\nUnknown-usage attempts: {_value(bundle.unknown_usage)}')
            st.caption('Estimated cost: unavailable. No verified pricing table is configured; reservations are not billable token measurements.')
            for control in view.controls:
                st.text(f'Negative control {control.control_id}: {control.status}')
                if control.status == 'violated' and view.provenance == 'scripted_integration':
                    st.warning('SCRIPTED NEGATIVE-CONTROL VIOLATION · The trusted evaluation marks a safe control as incorrectly flagged. This tests false-positive detection; it is not live-model accuracy.')
                elif control.status == 'not_evaluated':
                    st.caption('Not evaluated is not a pass. An incomplete review cannot establish a clean negative control.')
                elif control.status == 'pending':
                    st.caption('Pending semantic judgment; no clean-control conclusion yet.')
            if bundle.evaluation:
                st.text('Evaluation limitations: ' + ', '.join(bundle.evaluation.limitations))
                st.caption('Expected-defect scoring requires semantic judgment. Citation overlap alone earns no true positive; incomplete reviews do not pass controls.')
            for note in view.limitations:
                st.text(note)
            with st.expander('Report and artifact identifiers'):
                if bundle.artifact_name:
                    st.text('Artifact: .runtime/evaluation/' + bundle.artifact_name)
                if view.report_id:
                    st.text('Report identity: ' + view.report_id)
    return summary_slot, pipeline_slot, source_slot, citations_slot

DESTINATIONS=('Overview','New Review','Review History','Test Lab','Settings')


def _navigate(page):
    st.session_state['navigation']=page


def _back_to_history():
    st.session_state.pop('history:selected', None)
    _navigate('Review History')


def _explore_java():
    st.session_state['testlab:scenario'] = 'java_backend'
    st.session_state['testlab:mode'] = 'Scripted integration'
    _navigate('Test Lab')


def _explore_northstar(records):
    """Navigate only; history validation and ordering remain owned by the catalog."""
    from .real_review_ui import _open_history
    eligible = next((row.review for row in records
        if row.review.sample.value == 'csv_import' and row.review.status == 'completed'
        and row.review.report and row.review.report.status == 'completed'
        and row.review.provider == 'nebius' and row.review.usage.model_attempts > 0
        and [s.agent_name.value for s in row.review.report.specialists if s.selected] == ['code_quality']), None)
    st.subheader('Explore Northstar')
    real, scripted = st.columns(2)
    with real, st.container(border=True,key='theme-panel-explore-real'):
        st.subheader('Real AI Review', anchor=False)
        if eligible:
            st.write('CSV Import and Validation')
            st.caption('Genuine Nebius Code Quality execution · Saved result; opening it makes no model calls.')
            selected = next(s for s in eligible.report.specialists if s.selected)
            analysis = selected.diagnostics.analysis if selected.diagnostics else None
            st.text((f'{analysis.analyzed_lines}/{analysis.planned_lines} validated analyzed lines' if analysis else
                'Validated analyzed coverage unavailable in this record') + f' · {len(eligible.report.findings)} retained findings')
            st.caption('Zero findings is not proof of defect-free code.')
            st.button('Explore real review', key='explore:real', on_click=_open_history,
                args=(eligible.import_id,eligible.run_id),
                help='Newest eligible completed CSV record in retained history, ordered by artifact modification time. Local checks verify retention and integrity, not independent provider-provenance authentication.')
        else:
            st.info('Completed CSV review unavailable. It may be missing, expired or invalid; no result is substituted.')
            st.button('Explore real review', key='explore:real', disabled=True)
    with scripted, st.container(border=True,key='theme-panel-explore-scripted'):
        st.subheader('Multi-Agent Test Demonstration', anchor=False)
        st.write('Java backend · Offline / scripted')
        st.caption('Predetermined responses exercise multiple specialists and include an intentional false positive. These findings are not live Nebius discoveries.')
        st.button('Explore scripted demonstration', key='explore:scripted', on_click=_explore_java,
            help='Select Java scripted mode in Test Lab. Run the existing offline harness only when you choose.')


def _review_card(row, position):
    from .real_review_ui import NAMES, _open_history, saved_pipeline_state
    review = row.review
    with st.container(border=True,key='theme-panel-review-'+str(review.import_id)+'-'+str(review.run_id)):
        with st.container(horizontal=True, horizontal_alignment='distribute', vertical_alignment='center'):
            with st.container(width=660):
                st.subheader('Imported source sample — '+NAMES[review.sample.value], anchor=False)
                st.caption(f'Saved review {position} · Code Quality · ' + review.status.replace('_', ' ').title() +
                           f' · {len(review.report.findings) if review.report else 0} evidence-validated findings')
                st.caption('Imported demonstration source code')
            st.button('Open review', key=f'open:{review.import_id}:{review.run_id}',
                      help=f'Open saved review {position}: ' + NAMES[review.sample.value] + '; no inference.',
                      on_click=_open_history, args=(review.import_id, review.run_id))
        if review.report and not review.report.findings:
            st.caption(review.status.replace('_',' ').title() + ' · Zero findings · ' +
                       ('Analysis completed; not proof of defect-free code.' if review.status=='completed' else
                        'Review incomplete; not proof of defect-free code.'))
        detailed = False
        if review.report:
            for specialist in review.report.specialists:
                if specialist.selected and specialist.diagnostics:
                    d = specialist.diagnostics
                    if d.analysis:
                        st.caption(f'Validated analyzed coverage: {d.analysis.analyzed_lines}/{d.analysis.planned_lines} lines · Prepared: {d.analysis.prepared_lines}/{d.analysis.planned_lines}')
                    else:
                        st.caption('Validated analyzed coverage: unavailable in this legacy record')
                    st.caption(f'Coverage: {d.fully_inspected_files}/{d.total_files} files fully inspected · {d.observed_lines}/{d.total_lines} lines')
                    detailed = True
        if not detailed:
            st.caption('Coverage: unavailable')
        stages,_ = saved_pipeline_state(review)
        from collections import Counter
        from .ui_pipeline import stage_label
        counts = Counter(stage_label(stage) for stage in stages)
        st.caption('Recorded stages: '+ ' · '.join(f'{count} {label.lower()}' for label,count in counts.items()))
        with st.expander(f'Pipeline summary · saved review {position}'):
            render_pipeline(stages, compact=True)
        with st.expander(f'Record details · saved review {position}'):
            st.caption('Execution profile: '+(review.execution_profile.name if review.execution_profile else 'Unavailable · legacy record'))
            st.caption('File modified ' + row.file_modified.strftime('%Y-%m-%d %H:%M:%S UTC') +
                       ' · filesystem metadata, not review completion time')


def _history(overview=False):
    from .review_history import retained_reviews
    from .real_review_ui import render_real_result
    records = retained_reviews()
    if overview:
        _explore_northstar(records)
        counts = st.columns(3)
        counts[0].metric('Total reviews', len(records))
        counts[1].metric('Completed', sum(r.review.status == 'completed' for r in records))
        counts[2].metric('Need attention', sum(r.review.status != 'completed' for r in records))
        st.caption('Summary of available validated records in the bounded local history; not lifetime totals.')
        st.subheader('Recent reviews')
    if not records:
        st.info('No available retained reviews. Expired, invalid or unavailable records may be withheld. Start a review of approved source to build your local history.')
        return
    if not overview:
        identity = st.session_state.get('history:selected')
        selected = next((row for row in records
                         if (str(row.review.import_id), str(row.review.run_id)) == identity), None)
        if selected:
            st.button('Back to reviews', on_click=_back_to_history)
            render_real_result(selected.review.import_id, selected.review)
            return
        st.session_state.pop('history:selected', None)
    for position, row in enumerate(records[:3] if overview else records, start=1):
        _review_card(row, position)
    if overview:
        st.button('Browse review history', on_click=_back_to_history)
    else:
        st.caption('Only valid, retained records are shown. Listing is bounded to 32 recent artifact candidates; expired or invalid records are withheld.')


def _settings():
    from .real_review_ui import render_retention
    from .real_source_contracts import MODEL
    st.title('Settings')
    st.caption('Local workspace preferences and supported capabilities.')
    with st.container(border=True,key='theme-panel-settings'):
        st.subheader('Application')
        from .local_application import readiness
        local_status=readiness()
        st.text('Local checks: '+('Passed' if local_status['application_ready'] else 'Attention required'))
        st.caption('Single-user local application · No remote availability check')
        st.text('Provider: Nebius Token Factory')
        st.text('Required model: '+MODEL)
        st.caption('The environment is validated at explicit review startup. Credentials are never displayed or loaded from .env here.')
    from .execution_profiles import PROFILE_NAMES, get_profile
    st.subheader('Execution profiles')
    st.caption('Read-only fixed limits. Standard is the default; a different profile requires separate explicit consent.')
    with st.container(horizontal=True):
        for name in PROFILE_NAMES:
            p=get_profile(name)
            with st.container(border=True,width=290,key='theme-panel-profile-'+name):
                st.text(name)
                st.caption(f'{p.max_model_requests} model attempts · {p.max_tool_attempts} tools · {p.max_input_tokens:,} input units · {p.evaluation_timeout_seconds}s deadline')
                st.caption('4,000 output reservations · 1,000 tokens/request · 20s timeout · no retries or repairs')
    st.caption('Tracing configuration: '+('Disabled' if local_status['checks']['tracing_disabled'] else 'Attention required')+' · No external publishing')
    with st.expander('Source retention'):
        render_retention()
    with st.expander('Security and privacy'):
        st.write('This unauthenticated application is for one local operator. Do not expose it publicly. Source disclosure needs separate consent; import and report approval do not authorize transmission or publication.')
        st.write('A report decision is not an individual finding verdict. Exact source evidence does not prove a defect. No reviewed source is executed.')
    with st.expander('Advanced application details'):
        for check,passed in local_status['checks'].items(): st.text(check.replace('_',' ').title()+': '+('Passed' if passed else 'Needs attention'))
        st.text('Five available specialist identities: '+', '.join(AGENT_NAMES.values()))
        st.caption('Real-source execution currently supports Code Quality only. Cost is unavailable without verified pricing and usage.')
        st.text('.venv/bin/python -B examples/local_app.py check')
    with st.expander('GitHub integration · unavailable'):
        st.text_input('GitHub owner',value='',disabled=True)
        st.text_input('Repository',value='',disabled=True)
        st.number_input('Pull request number',min_value=0,value=0,disabled=True)
        st.caption('PR ingestion and publishing are not supported.')


def _test_lab():
    st.title('Test Lab')
    st.caption('Fictional source and scripted integration checks. Separate from real application reviews.')
    with st.expander('Five specialist capabilities'):
        st.caption('Five LangGraph specialist designs are implemented and tested offline. Only Code Quality is supported for live reviews of imported real source. Offline tests do not establish live-model accuracy.')
        specialist_cards(capabilities=True)
    scenario=st.selectbox('Fictional scenario',SCENARIOS,format_func=SCENARIO_NAMES.__getitem__,key='testlab:scenario')
    mode=st.radio('Evaluation mode',('Scripted integration','Saved live results'),horizontal=True,key='testlab:mode')
    st.subheader(SCENARIO_NAMES[scenario])
    st.caption('APPROVED FICTIONAL INPUT • ' + mode.upper())
    state_key = f'review:{scenario}:{mode}'
    if mode == 'Scripted integration':
        st.caption('OFFLINE TEST DEMONSTRATION · Fictional source and fake model responses.')
        try:
            definition = next(s for s in pinned_dataset().scenarios if s.id == scenario)
            st.caption(f'{SCENARIO_NAMES[scenario]} demonstration · {len(definition.snapshot.files)} fictional files · '
                       'Planned specialists: ' + ', '.join(AGENT_NAMES[a.value] for a in definition.specialists))
        except Exception:
            st.caption('Scenario metadata unavailable. No source scope or specialist selection is inferred.')
        st.caption('Inspect this scenario’s scripted findings and exact fictional evidence in Findings; these are not live Nebius discoveries.')
        if st.button('Run scripted review', type='primary'):
            st.session_state.pop(state_key, None)
            st.session_state.pop(state_key+':failed', None)
            with st.status('RUNNING · Offline scripted harness', expanded=True) as progress:
                try:
                    st.session_state[state_key] = run_baseline(scenario)
                    progress.update(label='PASSED · Harness returned a validated result', state='complete', expanded=False)
                except Exception:
                    st.session_state[state_key+':failed'] = True
                    progress.update(label='FAILED · Scripted harness execution', state='error', expanded=False)
                    st.error('The scripted review could not complete. No live model was contacted.')
        st.caption('Runs the existing integration harness with fake model responses and real inspection tools. No reviewed source is executed.')
        if st.session_state.get(state_key+':failed'):
            st.markdown('**Harness execution**')
            st.html(job_status_html('failed'))
            st.caption('No current result is available for the failed attempt. Previous findings are not presented as its result.')
    else:
        try:
            records = saved_live(scenario)
        except Exception:
            records = ()
            st.warning('Saved results are unavailable or failed validation.')
        if records:
            selected = st.selectbox('Saved live artifact', range(len(records)),
                format_func=lambda i: f'{records[i].run_status} · {records[i].artifact_name[:12]}')
            if st.button('Load saved live result', type='primary'):
                st.session_state[state_key] = records[selected]
        else:
            st.info('No compatible validated live artifact is available for this scenario.')
        st.caption('Historical results only. Unsupported, unsafe or outdated artifact schemas are withheld. Up to 64 recent artifact candidates are inspected.')
    bundle = st.session_state.get(state_key)
    if bundle is not None and bundle.scenario_id == scenario:
        _results(bundle)
    else:
        st.info('Choose a scenario and run the scripted baseline or load a saved live result to inspect its findings and review status.')
    st.divider()
    st.caption('Evidence integrity ≠ defect correctness ≠ human authorization. External integrations are deferred.')



def render_app():
    st.set_page_config(page_title='Northstar CodeReview', page_icon='✦', layout='wide')
    page = navigation(DESTINATIONS, _navigate)
    with workspace():
        if page == 'Overview':
            st.title('Northstar CodeReview')
            st.caption('AI-Powered Multi-Agent Code Review')
            with st.container(width=860):
                st.write('Review approved imported source-code snapshots, inspect evidence-grounded findings, and make a local human decision. This demo does not ingest or publish GitHub pull requests.')
            with st.container(horizontal=True):
                st.button('View existing reviews', type='primary', on_click=_back_to_history)
                st.button('New review', help='Start a review by choosing approved code; no source is sent by navigation.', on_click=_navigate, args=('New Review',))
            with st.expander('Explore the review pipeline'):
                from .ui_pipeline import STAGES
                st.caption('A process guide, not progress from a live run. Open a saved review for its recorded stage outcomes. Missing telemetry stays unavailable.')
                explanations = (
                    'Check the frozen snapshot identity and approved file scope.',
                    'Require separate permission to share code and to start one execution.',
                    'Prepare bounded source ranges; prepared does not mean analyzed.',
                    'Record what the selected specialist actually analyzed.',
                    'Validate retained citations against exact source; this does not prove defect correctness.',
                    'Keep a prioritized report, including partial outcomes, with a sanitized export.',
                    'Let a person approve, reject or request changes to the local report. Nothing is published.',
                )
                for index, ((_, title), explanation) in enumerate(zip(STAGES, explanations), 1):
                    st.markdown(f'**{index}. {title}** — {explanation}')
                st.button('See specialist capabilities', on_click=_navigate, args=('Test Lab',))
            _history(overview=True)
        elif page == 'New Review':
            from .real_review_ui import render_real_workspace
            render_real_workspace()
        elif page == 'Review History':
            st.title('Review history')
            st.caption('Imported demonstration source code · Retained real Nebius reviews. Viewing a report never starts inference. Each execution keeps its own scope, coverage and limitations.')
            _history()
        elif page == 'Test Lab':
            _test_lab()
        else:
            _settings()


def main():
    with tracing_context(enabled=False):
        try:
            render_app()
        except Exception:
            st.error('The local application could not render this view. Details withheld; inspect retained run state before starting another action.')
