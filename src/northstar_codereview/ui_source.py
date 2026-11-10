"""Bounded local source presentation. No cached source, execution or transmission."""
from uuid import UUID

import streamlit as st
from langsmith import tracing_context


def citation_window(start, end, total):
    if not 1 <= start <= end <= total or end-start+1 > 100:
        raise ValueError('Citation cannot fit the bounded viewer')
    margin = min(5, (100-(end-start+1))//2)
    return max(1,start-margin), min(total,end+margin)


def numbered_source(text, start, citation=None):
    """Physical LF lines only; a literal > marker conveys citation without color."""
    lines = text.split('\n')
    if lines and lines[-1] == '': lines.pop()
    return '\n'.join((('> ' if citation[0] <= n <= citation[1] else '  ') if citation else '')
                     + f'{n} | {line}' for n,line in enumerate(lines,start))


def request_citation(workspace, finding_id, index):
    st.session_state[workspace+':citation'] = (str(finding_id),index)
    st.session_state[workspace+':source-read'] = 'citation'
    st.session_state[workspace] = 'Source Files'


def _clear_citation(workspace):
    st.session_state.pop(workspace+':citation',None)
    st.session_state.pop(workspace+':source-read',None)


def _page(workspace, start_key, end_key, start, end):
    st.session_state[start_key],st.session_state[end_key] = start,end
    st.session_state[workspace+':source-read'] = 'range'


def real_citation(import_id,run_id,finding_id,index):
    from . import real_review as service
    saved = service.load_review(import_id,run_id)
    finding = next(f for f in saved.report.findings if f.finding_id == UUID(str(finding_id)))
    if type(index) is not int or not 0 <= index < len(finding.evidence):
        raise ValueError('Citation unavailable')
    return finding.title,finding.evidence[index]


def _scripted_snapshot(bundle):
    from .ui_models import ReviewBundle
    from .ui_service import pinned_dataset
    from .evaluation import fingerprint
    from .evaluation_data import load_scenario
    bundle = ReviewBundle.model_validate_json(bundle.model_dump_json())
    dataset = pinned_dataset()
    if (bundle.provenance != 'scripted_integration' or not bundle.report or not bundle.evaluation
            or bundle.evaluation.dataset_id != fingerprint(dataset)):
        raise ValueError('Pinned fictional source unavailable')
    snapshot = load_scenario(next(s for s in dataset.scenarios if s.id==bundle.scenario_id))
    if (bundle.report.snapshot_id,bundle.report.snapshot_revision) != (snapshot.manifest.snapshot_id,snapshot.manifest.revision):
        raise ValueError('Pinned fictional identity mismatch')
    return bundle,snapshot


def render_scripted_source(bundle,workspace):
    """Use the same pinned identity and bounded tools as fictional evidence viewing."""
    from .evidence_tools import get_source_metadata,read_source_lines
    def catalog():
        _,snapshot = _scripted_snapshot(bundle)
        if len(snapshot.manifest.files)>50: raise ValueError('Too many files')
        result=[]
        for f in snapshot.manifest.files:
            item=get_source_metadata(snapshot,file_path=f.file_path,side=f.side)
            if item.status!='ok': raise ValueError('Metadata unavailable')
            result.append(item.data)
        return result
    def resolve(finding_id,index):
        checked,snapshot = _scripted_snapshot(bundle)
        finding=next(f for f in checked.report.findings if f.finding_id==UUID(str(finding_id)))
        if type(index) is not int or not 0<=index<len(finding.evidence): raise ValueError('Citation unavailable')
        ref=finding.evidence[index]
        if not any((f.file_path,f.side,f.sha256)==(ref.file_path,ref.side,ref.source_sha256) for f in snapshot.manifest.files):
            raise ValueError('Citation identity mismatch')
        return finding.title,ref
    def read(entry,start,end):
        _,snapshot = _scripted_snapshot(bundle)
        item=read_source_lines(snapshot,file_path=entry.file_path,side=entry.side,start_line=start,end_line=end)
        if item.status!='ok': raise ValueError('Bounded read unavailable')
        return item.data.text
    with tracing_context(enabled=False):
        render_source_browser('fictional-source-view:'+workspace+':',workspace,catalog,read,resolve,fictional=True)


def render_real_source(import_id,run_id,workspace):
    from . import real_review as service
    render_source_browser(f'source-view:{import_id}:{run_id}:',workspace,
        lambda:service.source_file_catalog(import_id,run_id),
        lambda entry,start,end:service.source_excerpt(import_id,run_id,file_path=entry.file_path,start_line=start,end_line=end),
        lambda finding,index:real_citation(import_id,run_id,finding,index))


def render_source_browser(prefix,workspace,catalog,read,resolve,*,fictional=False):
    st.subheader('Source files · local read-only viewer')
    st.caption('Choose one captured file and at most 100 lines. Local browsing sends nothing to a provider and does not change recorded analyzed coverage or human decisions.')
    if fictional: st.caption('Pinned fictional source · Scripted demonstration, not a live Nebius discovery.')
    command=st.session_state.pop(workspace+':source-read',None)
    try:
        files=catalog()
    except Exception:
        st.warning('Source files unavailable: retention, snapshot integrity or saved-review binding could not be verified.')
        return
    if not files:
        st.info('No readable files are available in this approved snapshot.')
        return
    def name(entry): return f'{entry.side.value} · {entry.file_path}' if fictional else entry.file_path
    options={name(f):f for f in files}
    citation=None
    intent=st.session_state.get(workspace+':citation')
    if intent:
        st.button('Browse files without a citation',key=prefix+'clear',on_click=_clear_citation,args=(workspace,))
        try:
            title,ref=resolve(*intent)
            entry=next(f for f in files if (f.file_path,f.side,f.sha256)==(ref.file_path,ref.side,ref.source_sha256))
            start,end=citation_window(ref.start_line,ref.end_line,entry.line_count)
            citation=(ref.start_line,ref.end_line)
            st.text('Finding: '+title)
            st.text(f'Selected citation: {ref.file_path}:{ref.start_line}–{ref.end_line} ({ref.side.value})')
            st.caption('Lines marked > are cited evidence. Exact source correspondence does not prove the defect claim.')
            if command=='citation':
                st.session_state[prefix+'file']=name(entry)
                st.session_state[prefix+name(entry)+':start']=start
                st.session_state[prefix+name(entry)+':end']=end
        except Exception:
            st.warning('Cited evidence unavailable, expired, changed or larger than the 100-line viewer bound. No substitute source is displayed.')
            return
    if st.session_state.get(prefix+'file') not in options:
        st.session_state[prefix+'file']=next(iter(options))
    selected=st.selectbox('Snapshot source file',list(options),key=prefix+'file',on_change=_clear_citation,args=(workspace,))
    entry=options[selected]
    if citation and (entry.file_path,entry.side)!=(ref.file_path,ref.side):
        st.warning('Selected citation does not match this file. No substitute source is displayed.')
        return
    st.caption(f'{entry.line_count} captured lines · {entry.byte_count} bytes · existing snapshot hash verified')
    if not entry.line_count:
        st.info('This captured file is empty.')
        return
    start_key,end_key=prefix+selected+':start',prefix+selected+':end'
    with st.expander('Advanced · Go to line / custom range'):
        start=st.number_input('First source line',min_value=1,max_value=entry.line_count,value=1,key=start_key)
        end=st.number_input('Last source line',min_value=1,max_value=entry.line_count,value=min(20,entry.line_count),key=end_key)
    valid=1<=end-start+1<=100
    st.text(f'Selected range: {start}–{end} of {entry.line_count}')
    if not valid: st.info('Choose an ordered range of at most 100 lines.')
    with st.container(horizontal=True):
        requested=st.button('Read selected source lines',disabled=not valid,key=prefix+'read')
        size=max(1,min(100,end-start+1))
        st.button('Previous range',disabled=not valid or start==1,key=prefix+'previous',on_click=_page,
            args=(workspace,start_key,end_key,max(1,start-size),max(1,start-1)))
        st.button('Next range',disabled=not valid or end==entry.line_count,key=prefix+'next',on_click=_page,
            args=(workspace,start_key,end_key,min(entry.line_count,end+1),min(entry.line_count,end+size)))
    if citation and (end<citation[0] or start>citation[1]):
        st.caption('The cited lines are outside the selected range. Use Inspect code to return to the citation.')
    if valid and (requested or command in ('citation','range')):
        try:
            text=read(entry,start,end)
            st.caption(f'Captured source lines {start}–{end}, inclusive. Plain text only; never executed. No source download is provided.')
            st.code(numbered_source(text,start,citation),language='text',line_numbers=False,wrap_lines=True)
        except Exception:
            st.warning('Source range unavailable or exceeds the existing output limit. Recheck retention and choose a smaller valid range.')
