"""Developer workspace interactions; generated imports and scripted graphs only."""
from dataclasses import replace
import pytest
from streamlit.testing.v1 import AppTest
from .test_real_source import local_intake
from .test_real_review import service, captured, Fake, ENV, permit
from .test_ui_pipeline import result
from .test_ui_interaction_acceptance import csv_reviews, open_csv, click, select_tab, tab, active_tab
from .test_readability_consistency import isolated_ui
from .test_enterprise_ui import screen


@pytest.mark.parametrize('start,end,total,expected',[(80,90,200,(75,95)),(1,2,9,(1,7)),(99,100,100,(94,100)),(20,119,200,(20,119))])
def test_citation_context_is_bounded_and_exact(start,end,total,expected):
    from northstar_codereview.ui_source import citation_window
    assert citation_window(start,end,total)==expected


@pytest.mark.parametrize('values',[(1,101,200),(0,3,10),(8,7,10),(1,11,10)])
def test_invalid_or_oversized_citation_never_silently_truncated(values):
    from northstar_codereview.ui_source import citation_window
    with pytest.raises(ValueError):citation_window(*values)


def test_absolute_numbers_and_citation_markers_preserve_physical_lines():
    from northstar_codereview.ui_source import numbered_source
    assert numbered_source('a\u2028b\rc\n<script>x</script>\n',80,(81,81))=='  80 | a\u2028b\rc\n> 81 | <script>x</script>'


@pytest.mark.parametrize('selected,status,calls,tools,expected',[(False,'skipped',0,0,'Not selected'),(True,'completed',2,2,'Selected and executed'),(True,'partial',2,2,'Selected and executed · Incomplete'),(True,'failed',0,0,'Selected but incomplete'),(True,'completed',None,None,'Execution metadata unavailable')])
def test_selection_does_not_imply_execution(selected,status,calls,tools,expected):
    from northstar_codereview.ui_models import SpecialistCard,activity_label
    row=SpecialistCard(agent='code_quality',name='Code Quality',selected=selected,status=status,files=(),synthesis='not_started',model_attempts=calls,tool_attempts=tools)
    assert expected in activity_label(row)


def test_saved_header_activity_and_no_model_call(csv_reviews):
    at=open_csv(csv_reviews)
    assert any(s.value=='Who reviewed this code?' for s in at.subheader)
    text='\n'.join(t.value for t in at.text)
    assert 'Recorded provider: nebius' in text and 'Qwen/Qwen3-30B-A3B-Instruct-2507' in text
    assert 'Code Quality · Selected and executed' in text
    assert text.count('· Not selected')==4
    assert any(m.label=='Analyzed coverage' and m.value=='12/12 lines' for m in at.metric)


def test_native_stage_cards_are_the_only_full_pipeline(csv_reviews):
    at=open_csv(csv_reviews);click(at,'Walk through this review')
    panel=tab(at,'Pipeline')
    assert not any('<ol ' in h.proto.body for h in panel.get('html'))
    assert len([b for b in panel.button if b.key and ':source' in b.key])==1
    assert any('Saved review walkthrough — reconstructed from recorded results.'==c.value for c in panel.caption)
    click(at,'Next stage');assert next(s for s in at.selectbox if s.label=='Select pipeline stage').value=='authorization'
    assert 'Sharing approved · Completed' in '\n'.join(t.value for t in tab(at,'Pipeline').text)
    assert not any('Running' in c.value for c in tab(at,'Pipeline').caption)


def test_scripted_finding_opens_exact_source_without_new_graph(isolated_ui,monkeypatch):
    from northstar_codereview import streamlit_ui
    calls=[];run=streamlit_ui.run_baseline
    def once(s):calls.append(s);return run(s)
    monkeypatch.setattr(streamlit_ui,'run_baseline',once)
    at=screen();click(at,'Explore scripted demonstration');click(at,'Run scripted review');click(at,'Inspect Findings')
    next(b for b in tab(at,'Findings').button if b.label=='Inspect code').click().run()
    assert active_tab(at)=='Source Files' and not at.exception
    assert tab(at,'Source Files').code
    assert '> ' in tab(at,'Source Files').code[0].value
    assert any('Selected citation:' in t.value for t in tab(at,'Source Files').text)
    assert any('SCRIPTED FALSE-POSITIVE' in w.value for w in at.warning)
    assert calls==['java_backend']
    at.run();assert not at.code and calls==['java_backend']


def test_human_decision_requires_an_actual_choice(isolated_ui):
    at=screen();click(at,'Explore scripted demonstration');click(at,'Run scripted review')
    select_tab(at,'Human Review');click(at,'Start human review')
    choice=next(s for s in at.selectbox if s.label=='Local report decision')
    assert choice.value is None
    assert next(b for b in at.button if b.label=='Record manual decision').disabled


def test_source_range_controls_are_advanced_and_no_initial_read(csv_reviews,service,monkeypatch):
    reads=[];read=service.source_excerpt
    def counted(*a,**k):reads.append(k);return read(*a,**k)
    monkeypatch.setattr(service,'source_excerpt',counted)
    at=open_csv(csv_reviews);click(at,'View Source Files')
    panel=tab(at,'Source Files')
    assert not reads and not panel.code
    assert any('Go to line' in e.label and not e.proto.expanded for e in panel.expander)
    assert next(b for b in panel.button if b.label=='Previous range').disabled
    assert next(b for b in panel.button if b.label=='Next range').disabled
    click(at,'Read selected source lines');assert len(reads)==1 and at.code
    at.radio(key='appearance:theme').set_value('Dark').run()
    assert not at.code and len(reads)==1


def test_real_citation_resolves_only_from_saved_finding(result,service):
    from northstar_codereview.ui_source import real_citation
    finding=result.report.findings[0]
    title,ref=real_citation(result.import_id,result.run_id,str(finding.finding_id),0)
    assert title==finding.title and ref==finding.evidence[0]
    with pytest.raises(Exception):real_citation(result.import_id,result.run_id,'00000000-0000-0000-0000-000000000000',0)


def generated_browser():
    from types import SimpleNamespace
    import streamlit as st
    from northstar_codereview.ui_source import render_source_browser
    from northstar_codereview.models import SourceSide
    lines=[f'value_{i} = {i}' for i in range(1,206)]
    lines[80]='# <script>never_run()</script>'
    entry=SimpleNamespace(file_path='generated.py',side=SourceSide.HEAD,sha256='a'*64,line_count=205,byte_count=4000)
    def read(e,start,end):
        st.session_state['read-count']=st.session_state.get('read-count',0)+1
        return '\n'.join(lines[start-1:end])+'\n'
    render_source_browser('generated:','workspace',lambda:[entry],read,lambda *a:None)


def test_paging_and_advanced_absolute_line_numbers():
    at=AppTest.from_function(generated_browser).run()
    assert not at.code
    click(at,'Read selected source lines');assert at.code[0].value.startswith('1 |')
    click(at,'Next range');assert at.code[0].value.startswith('21 |') and '40 |' in at.code[0].value
    click(at,'Previous range');assert at.code[0].value.startswith('1 |')
    next(n for n in at.number_input if n.label=='First source line').set_value(80).run()
    next(n for n in at.number_input if n.label=='Last source line').set_value(100).run()
    click(at,'Read selected source lines')
    assert at.code[0].value.startswith('80 |') and '\n100 |' in at.code[0].value
    assert '81 | # <script>never_run()</script>' in at.code[0].value
    assert len(at.code[0].value.split('\n'))==21
    assert 'never_run' not in repr(at.session_state.to_dict())
    assert not any('never_run' in h.proto.body for h in at.get('html'))
    count=at.session_state['read-count'];at.run();assert not at.code and at.session_state['read-count']==count
    next(n for n in at.number_input if n.label=='First source line').set_value(1).run()
    next(n for n in at.number_input if n.label=='Last source line').set_value(101).run()
    assert next(b for b in at.button if b.label=='Read selected source lines').disabled


def _two_citation_record(result):
    """Only the generated test artifact; existing approved source bytes untouched."""
    import json
    from northstar_codereview import real_source as intake
    from northstar_codereview.evaluation import fingerprint
    from northstar_codereview.reporting import _digest
    path=intake._STORE/'imports'/str(result.import_id)/'review_artifacts'/f'{result.run_id}.json'
    data=json.loads(path.read_text());body=data['result'];report=body['report']
    first=report['findings'][0]['evidence'][0]
    other=next(f for f in intake.load_import(result.import_id).manifest.files if f.file_path!=first['file_path'])
    report['findings'][0]['evidence'].append({**first,'file_path':other.file_path,'source_sha256':other.sha256})
    report.pop('report_id');report['report_id']=_digest(report)
    path.write_text(json.dumps({'digest':fingerprint(body),'result':body}))
    return other.file_path


def test_real_multi_citation_choice_revalidates_and_keeps_only_identifiers(result,service,monkeypatch,tmp_path):
    from northstar_codereview import real_review_ui,streamlit_ui
    from northstar_codereview.ui_service import LocalReview
    local=LocalReview(database=tmp_path/'decisions.sqlite3')
    monkeypatch.setattr(real_review_ui,'LocalReview',lambda:local);monkeypatch.setattr(streamlit_ui,'LocalReview',lambda:local)
    other=_two_citation_record(result)
    calls=[];read=service.source_excerpt
    def tracked(*a,**k):calls.append(k);return read(*a,**k)
    monkeypatch.setattr(service,'source_excerpt',tracked)
    monkeypatch.setattr(service,'run_review',lambda *a,**k:pytest.fail('Inference'))
    monkeypatch.setattr(service,'authorize_transmission',lambda *a,**k:pytest.fail('Authorization'))
    at=screen();at.button(key=f'open:{result.import_id}:{result.run_id}').click().run()
    click(at,'Inspect Findings')
    next(s for s in at.selectbox if s.label=='Evidence reference').set_value(1).run()
    click(at,'Inspect code')
    assert active_tab(at)=='Source Files' and not at.exception
    assert next(s for s in at.selectbox if s.label=='Snapshot source file').value==other
    assert calls[-1]['file_path']==other and calls[-1]['end_line']-calls[-1]['start_line']<100
    assert at.code[0].value.startswith('> 1 | value = 1')
    assert 'value = 1' not in repr(at.session_state.to_dict())
    at.run();assert len(calls)==1 and not at.code
    # A stale finding identifier cannot select unrelated valid source.
    workspace='review-workspace:result:'+str(result.import_id)+':'+str(result.run_id)
    at.session_state[workspace+':citation']=('00000000-0000-0000-0000-000000000000',0)
    at.session_state[workspace+':source-read']='citation'
    at.run();assert not at.code and len(calls)==1
    assert any('Cited evidence unavailable' in w.value for w in at.warning)


@pytest.mark.parametrize('category',['expired','hash_mismatch','missing'])
def test_pending_citation_failure_does_not_fallback_or_leak(result,service,monkeypatch,tmp_path,category):
    from northstar_codereview import real_review_ui,streamlit_ui
    from northstar_codereview.ui_service import LocalReview
    local=LocalReview(database=tmp_path/'decisions.sqlite3')
    monkeypatch.setattr(real_review_ui,'LocalReview',lambda:local);monkeypatch.setattr(streamlit_ui,'LocalReview',lambda:local)
    at=screen();at.button(key=f'open:{result.import_id}:{result.run_id}').click().run();click(at,'Inspect Findings')
    def fail(*a,**k):raise ValueError('PRIVATE_'+category)
    monkeypatch.setattr(service,'source_excerpt',fail)
    click(at,'Inspect code')
    assert not at.code and not at.exception
    assert any('Source range unavailable' in w.value for w in at.warning)
    assert not any('PRIVATE_' in str(e.value) for e in at if hasattr(e,'value'))


def test_legacy_provider_metadata_stays_unavailable(isolated_ui):
    from northstar_codereview.ui_service import run_baseline
    bundle=run_baseline('java_backend').model_copy(update={'provenance':'recorded_live','evaluation':None})
    def render(b):
        from northstar_codereview.streamlit_ui import _results
        _results(b)
    at=AppTest.from_function(render,args=(bundle,)).run()
    assert any('Recorded provider: Unavailable · Recorded model: Unavailable'==t.value for t in at.text)
    assert not any(b.label=='Inspect code' for b in at.button)
    assert not at.code


def test_missing_report_does_not_claim_agents_were_unselected():
    from northstar_codereview.ui_models import ReviewBundle,present_review,activity_label
    view=present_review(ReviewBundle(scenario_id='portfolio_risk',provenance='real_source_live',run_status='failed'))
    assert all(activity_label(row)=='Selection and execution unavailable' for row in view.specialists)


def test_native_connectors_do_not_depend_on_streamlit_wrapper_siblings():
    from northstar_codereview.ui_style import _PIPELINE_STYLE
    assert '[class*="st-key-pipeline-card-status-"][class*="-connector-"]::after' in _PIPELINE_STYLE


def test_native_stage_titles_are_closed_application_labels():
    from northstar_codereview.ui_pipeline import pipeline_stages
    from tests.test_review_walkthrough import render_stages
    rows=list(pipeline_stages(None));rows[0]=replace(rows[0],title='UNTRUSTED_MARKDOWN')
    at=AppTest.from_function(render_stages,args=(rows,)).run()
    assert any(b.label=='Source verified' for b in at.button)
    assert not any('UNTRUSTED' in b.label for b in at.button)


def test_stage_subjects_are_recorded_sources_not_invented_coverage(result):
    from northstar_codereview.ui_pipeline import recorded_stage_details
    details=recorded_stage_details(result)
    assert any('Code Quality' in item for item in details['analysis'].subjects)
    observed={r.file_path for r in result.report.specialists[0].diagnostics.analysis.ranges if r.outcome=='analyzed'}
    assert observed and all(any(path in item for item in details['analysis'].subjects) for path in observed)
    assert not recorded_stage_details(None)['analysis'].subjects
