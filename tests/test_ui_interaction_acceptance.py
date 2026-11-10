"""Native control callbacks and reruns against generated, retained CSV reviews."""
import asyncio
from hashlib import sha256

import pytest
from langchain_core.messages import AIMessage

from northstar_codereview import real_source as intake
from northstar_codereview.real_source_contracts import SAMPLES, SampleId, IntakeRejected, FailureCategory
from .test_enterprise_ui import screen
from .test_real_source import local_intake, approval_for
from .test_real_review import service, Fake, ENV


class EmptyReview(Fake):
    async def ainvoke(self, messages, **kwargs):
        return AIMessage(content='{"findings":[]}', response_metadata={'finish_reason':'stop'})


@pytest.fixture
def csv_reviews(local_intake, service, tmp_path, monkeypatch):
    root, _ = local_intake
    text = '# fictional UI test\nmarker = "<script>never_execute()</script>"\nvalue = 2\n'
    for path in SAMPLES[SampleId.CSV_IMPORT][1]:
        file = root/path; file.parent.mkdir(parents=True, exist_ok=True); file.write_text(text)
    monkeypatch.setattr(intake, '_SOURCE_ROOTS', {SAMPLES[SampleId.CSV_IMPORT][0]:root})
    receipt = approval_for(SampleId.CSV_IMPORT, text)
    intake.import_sample('csv_import', approval=receipt)
    outputs = []
    for _ in range(2):
        permit = service.authorize_transmission('csv_import', receipt.approval_id,
            expected_digest=receipt.manifest.digest, allow_transmission=True, execution_profile='extended_csv')
        result = asyncio.run(service.run_review('csv_import',receipt.approval_id,
            transmission_id=permit.approval_id,allow_live=True,environ=ENV,
            execution_profile='extended_csv',provider_factory=lambda *a:EmptyReview()))
        assert result.status=='completed' and not result.report.findings
        outputs.append(result)
    from northstar_codereview import streamlit_ui, real_review_ui, approval_store
    from northstar_codereview.ui_service import LocalReview
    monkeypatch.setattr(approval_store, 'PROJECT_ROOT', tmp_path)
    local = LocalReview(database=tmp_path/'.runtime/isolated-human/reviews.sqlite3')
    monkeypatch.setattr(streamlit_ui,'LocalReview',lambda:local)
    monkeypatch.setattr(real_review_ui,'LocalReview',lambda:local)
    def denied(*a, **k): pytest.fail('UI attempted execution or authority mutation')
    for name in ('run_review','authorize_transmission','load_nebius_settings'):
        monkeypatch.setattr(service,name,denied)
    monkeypatch.setattr(real_review_ui,'renew_import',denied)
    monkeypatch.setattr(local,'start',denied); monkeypatch.setattr(local,'submit',denied)
    return receipt, outputs


def click(at,label):
    button=next(b for b in at.button if b.label==label)
    for section in at.tabs:
        if any(b.key==button.key for b in section.button):
            assert section.label==active_tab(at), 'Do not click a control in a hidden tab'
    button.click().run()
    assert not at.exception
    return at


def open_csv(values):
    receipt, outputs=values
    at=screen()
    at.button(key=f'open:{receipt.approval_id}:{outputs[0].run_id}').click().run()
    assert not at.exception
    return at


def select_tab(at,name):
    # Native frontend event: installed AppTest has no Tab.select().
    state=at._tree.get_widget_states()
    state.widgets.add(id=at.get('tab_container')[0].proto.tab_container.id,string_value=name)
    at._run(state)
    assert active_tab(at)==name and not at.exception
    return at


def tab(at,name): return next(t for t in at.tabs if t.label==name)
def texts(scope): return '\n'.join(t.value for t in scope.text)
def active_tab(at):
    group=at.get('tab_container')[0]
    return at.tabs[group.proto.tab_container.default_tab_index].label


def test_click_stages_changes_visible_details_and_preserves_active_tab(csv_reviews):
    at=open_csv(csv_reviews)
    click(at,'View Pipeline'); assert active_tab(at)=='Pipeline'
    click(at,'Code analyzed')
    p=tab(at,'Pipeline')
    assert active_tab(at)=='Pipeline'
    assert 'Code analyzed · Completed' in texts(p)
    assert 'schema=valid; finish=stop; truncated=False' in texts(p)
    assert 'Analyzed lines: 12' in texts(p)
    at.run(); assert active_tab(at)=='Pipeline' and 'Code analyzed · Completed' in texts(tab(at,'Pipeline'))
    click(at,'Source prepared')
    assert 'Source prepared · Completed' in texts(tab(at,'Pipeline'))
    assert 'Prepared lines: 12' in texts(tab(at,'Pipeline'))
    assert 'schema=valid' not in texts(tab(at,'Pipeline'))
    click(at,'Evidence checked')
    assert 'No retained citations' in texts(tab(at,'Pipeline'))
    assert 'Retained citations: 0' in texts(tab(at,'Pipeline'))
    assert not at.code


def test_review_switch_resets_stage_and_workspace_selection(csv_reviews):
    receipt, outputs=csv_reviews
    at=open_csv(csv_reviews); click(at,'View Pipeline'); click(at,'Code analyzed')
    at.button(key='nav:Review History').click().run()
    # History keeps the selected review until its explicit back control is used.
    if any(b.label=='Back to Review History' for b in at.button): click(at,'Back to Review History')
    else:
        back=next(b for b in at.button if 'Back' in b.label); back.click().run()
    at.button(key=f'open:{receipt.approval_id}:{outputs[1].run_id}').click().run()
    assert active_tab(at)=='Summary'
    click(at,'View Pipeline')
    assert next(s for s in at.selectbox if s.label=='Select pipeline stage').value=='source'
    assert 'Source verified · Completed' in texts(tab(at,'Pipeline'))


def test_source_tab_selection_numbering_escape_and_no_implicit_reads(csv_reviews, service, monkeypatch):
    calls=[]; original=service.source_excerpt
    def read(*a,**k): calls.append(k); return original(*a,**k)
    monkeypatch.setattr(service,'source_excerpt',read)
    at=open_csv(csv_reviews)
    click(at,'View Source Files'); assert active_tab(at)=='Source Files'
    source=tab(at,'Source Files')
    assert any('Go to line' in e.label and not e.proto.expanded for e in source.expander)
    assert not calls and not at.code
    next(s for s in source.selectbox if s.label=='Snapshot source file').set_value('tests/test_imports.py').run()
    next(n for n in at.number_input if n.label=='First source line').set_value(2).run()
    next(n for n in at.number_input if n.label=='Last source line').set_value(3).run()
    click(at,'Read selected source lines')
    assert active_tab(at)=='Source Files' and len(calls)==1
    code=tab(at,'Source Files').code[0].value
    assert '2 | marker = "<script>never_execute()</script>"' in code and '3 | value = 2' in code
    assert not any('never_execute()' in h.proto.body for h in at.get('html'))
    assert len(at.get('download_button'))==1
    assert 'never_execute()' not in repr(at.session_state.to_dict())
    at.run(); assert not at.code and len(calls)==1


def test_human_review_contains_context_and_working_source_findings_navigation(csv_reviews):
    at=open_csv(csv_reviews); click(at,'Open Human Review')
    assert active_tab(at)=='Human Review'
    human=tab(at,'Human Review'); text=texts(human)
    assert 'Review status: Completed' in text and 'Selected specialist: Code Quality' in text
    assert 'Validated analyzed coverage: 12/12 lines' in text
    assert any('No evidence-validated findings' in e.value for e in human.info)
    assert any('does not approve a GitHub pull request' in e.value for e in human.markdown)
    assert any('Review limitations' in e.value for e in human.caption)
    click(at,'Inspect source files'); assert active_tab(at)=='Source Files'
    select_tab(at,'Human Review'); click(at,'Inspect findings and evidence')
    assert active_tab(at)=='Findings'
    assert not at.code


@pytest.mark.parametrize('state,category',[('expired',FailureCategory.AUTHORIZATION),('invalid',FailureCategory.INTEGRITY),('unauthorized',FailureCategory.AUTHORIZATION),('missing',FailureCategory.STORAGE)])
def test_source_unavailable_states_have_no_read_controls(csv_reviews,service,monkeypatch,state,category):
    def unavailable(*a,**k): raise IntakeRejected(category)
    monkeypatch.setattr(service,'source_file_catalog',unavailable)
    at=open_csv(csv_reviews); click(at,'View Source Files')
    assert any('Source files unavailable' in e.value for e in tab(at,'Source Files').warning)
    assert not any(b.label=='Read selected source lines' for b in at.button)
    assert not at.code


def test_scripted_java_evidence_and_harness_status_remain_distinct(csv_reviews):
    at=screen(); at.button(key='nav:Test Lab').click().run(); click(at,'Run scripted review')
    click(at,'Inspect Findings'); assert active_tab(at)=='Findings'
    findings=tab(at,'Findings')
    assert any('SCRIPTED INTEGRATION' in e.value for e in at.info)
    assert any('SCRIPTED FALSE-POSITIVE' in e.value for e in findings.warning)
    button=next(b for b in findings.button if b.label.startswith('Show fictional source:'))
    assert ':' in button.label and '–' in button.label
    button.click().run()
    assert tab(at,'Findings').code and active_tab(at)=='Findings'
    assert any('HIGH' in c.value or 'MEDIUM' in c.value for c in findings.caption)
    assert any('PASSED' in h.proto.body for h in tab(at,'Pipeline').get('html'))
    assert not any('PASSED' in h.proto.body for h in tab(at,'Findings').get('html'))


def test_ui_navigation_does_not_modify_generated_saved_records(csv_reviews):
    before={str(p):sha256(p.read_bytes()).hexdigest() for p in intake._STORE.rglob('*') if p.is_file()}
    at=open_csv(csv_reviews); click(at,'View Pipeline'); click(at,'Code analyzed')
    select_tab(at,'Source Files'); click(at,'Read selected source lines'); select_tab(at,'Human Review')
    after={str(p):sha256(p.read_bytes()).hexdigest() for p in intake._STORE.rglob('*') if p.is_file()}
    assert before==after


def test_native_tab_header_event_and_stage_rerun_keep_pipeline_visible(csv_reviews):
    at=open_csv(csv_reviews)
    # Installed AppTest has no Tab.select(); feed the native frontend widget event.
    # This tests our real tabs binding, not a session-state assignment shortcut.
    select_tab(at,'Pipeline')
    assert active_tab(at)=='Pipeline' and not at.exception
    click(at,'Code analyzed')
    assert active_tab(at)=='Pipeline' and 'Analyzed lines: 12' in texts(tab(at,'Pipeline'))


def test_excerpt_not_retained_in_session_state_and_physical_line_numbers(csv_reviews,service,monkeypatch):
    # Isolate the renderer's physical-LF contract. The real service is covered by
    # the CSV interaction and bounded-source tests; these characters aren't executed.
    monkeypatch.setattr(service,'source_excerpt',lambda *a,**k:'a\u2028b\rc\nlast\n')
    at=open_csv(csv_reviews); click(at,'View Source Files')
    next(n for n in at.number_input if n.label=='First source line').set_value(2).run()
    click(at,'Read selected source lines')
    assert tab(at,'Source Files').code[0].value=='2 | a\u2028b\rc\n3 | last'
    assert repr('a\u2028b\rc')[1:-1] not in repr(at.session_state.to_dict())
    at.run(); assert not at.code


def test_legacy_metadata_is_unavailable_not_invented(csv_reviews):
    import json
    from northstar_codereview.evaluation import fingerprint
    from northstar_codereview.reporting import _digest
    receipt, outputs=csv_reviews; output=outputs[0]
    # Only a generated fixture artifact is edited to characterize legacy schema.
    path=intake._STORE/'imports'/str(receipt.approval_id)/'review_artifacts'/f'{output.run_id}.json'
    data=json.loads(path.read_text()); body=data['result']; report=body['report']
    for row in report['specialists']:
        if row['diagnostics']: row['diagnostics'].pop('analysis',None)
    report.pop('report_id'); report['report_id']=_digest(report)
    path.write_text(json.dumps({'digest':fingerprint(body),'result':body}))
    at=open_csv(csv_reviews); click(at,'View Pipeline'); click(at,'Code analyzed')
    panel=tab(at,'Pipeline')
    assert 'Analyzed lines: Unavailable' in texts(panel)
    assert 'Recorded units: Unavailable' in texts(panel)
    assert 'Unit 1:' not in texts(panel)
    select_tab(at,'Human Review')
    assert 'Validated analyzed coverage: Unavailable' in texts(tab(at,'Human Review'))
    assert not at.exception
