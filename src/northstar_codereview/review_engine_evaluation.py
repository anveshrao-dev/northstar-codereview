"""Fixed offline prepared-engine cases. Scripted results are not model accuracy.

No provider, credentials, arbitrary sources, live option, source execution or
writes. Case expectations stay in this harness, never in specialist prompts.
"""
import argparse
import asyncio
import json
from time import monotonic
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage

from .agents.code_quality import CodeQualityAgent, RunLimits
from .evaluation_data import load_dataset, load_scenario
from .evaluation_runner import _offline_only, load_scripts
from .execution_budget import ExecutionBudget, GlobalLimits
from .local_source import _assemble


class _Script:
    def __init__(self, mode, claim=None):
        self.mode, self.claim = mode, claim

    def bind_tools(self, tools):
        self.tools = tools
        return self

    async def ainvoke(self, messages, **kwargs):
        if self.mode == 'truncated':
            return AIMessage(content='{"findings":[', response_metadata={'finish_reason':'length'})
        if self.mode == 'missing':
            return AIMessage(content='{}', response_metadata={'finish_reason':'stop'})
        data = json.loads(next(m.content.split('PREPARED_SOURCE_DATA=',1)[1] for m in messages
                              if isinstance(m,HumanMessage) and 'PREPARED_SOURCE_DATA=' in m.content))
        findings = []
        if self.mode in ('claim','fabricated'):
            refs = data['references']
            if self.claim is not None:
                pointers=[]
                for loc in self.claim.locations:
                    ref=next(r for r in refs if r['file_path']==loc.file_path and r['side']==loc.side.value
                             and r['start_line']<=loc.start_line<=loc.end_line<=r['end_line'])
                    pointers.append(dict(reference_id=ref['reference_id'],start_line=loc.start_line,end_line=loc.end_line))
                proposal=self.claim.model_dump(mode='json',exclude={'id','agent_name','locations'})
            else:
                ref=refs[0]
                pointers=[dict(reference_id=ref['reference_id'],start_line=ref['start_line'],end_line=ref['end_line'])]
                proposal=dict(title='Exact-age boundary',category='correctness',severity='low',confidence=.5,
                    explanation='The scripted policy allows age eighteen, but the predicate excludes equality.',
                    suggested_remediation='Include equality at the specified age boundary.',assumptions=['External dependencies are unverified.'])
            if self.mode=='fabricated': pointers[0]['reference_id']='f'*64
            findings=[dict(**proposal,evidence=pointers)]
        payload={'findings':findings}
        if self.mode=='insufficient': payload['outcome']='insufficient_evidence'
        return AIMessage(content=json.dumps(payload), response_metadata={'finish_reason':'stop'})


def _cases():
    defect=_assemble({('head','eligibility.py'):'# Policy: eighteen and older are eligible.\ndef eligible(age):\n    return age > 18\n'})
    clean=_assemble({('head','eligibility.py'):'# Policy: eighteen and older are eligible.\ndef eligible(age):\n    return age >= 18\n'})
    for name,snapshot,mode in (
        ('known_boundary',defect,'claim'),('clean',clean,'empty'),('fabricated',defect,'fabricated'),
        ('truncated',defect,'truncated'),('missing_envelope',defect,'missing'),
        ('missing_dependency',_assemble({('head','consumer.py'):'from unavailable import policy\nvalue = policy()\n'}),'insufficient'),
        ('large',_assemble({('head',f'module_{n}.py'):'value = 1\n'*500 for n in range(4)}),'empty')):
        yield name,snapshot,_Script(mode),False
    scenario=next(s for s in load_dataset().scenarios if s.id=='java_backend')
    script=next(s for s in load_scripts().scenarios if s.id==scenario.id)
    assertion=next(a for a in script.assertions if a.verdict=='false_positive')
    claim=next(c for c in script.claims if c.id==assertion.claim_id)
    # The verdict comes from the existing predefined fixture assertion, not
    # citation overlap or anything a model says. Never supplied in agent prompts.
    yield 'existing_negative_control',load_scenario(scenario),_Script('claim',claim),True


async def _run_case(name,snapshot,model,false_positive):
    ledger=ExecutionBudget(GlobalLimits(max_model_requests=4,max_tool_attempts=6,max_input_tokens=80000,max_output_tokens=4000))
    job=uuid4()
    agent=CodeQualityAgent(snapshot,model=model,approved_synthetic=True,prepared_context=True,execution_budget=ledger,
        limits=RunLimits(max_model_calls=4,max_tool_rounds=3,max_tool_attempts=6,max_input_tokens=80000,max_output_tokens=4000,
                         max_provider_retries=0,max_output_repairs=0,request_timeout_seconds=20,run_timeout_seconds=60))
    agent.synthesis_reservation=ledger.reserve_synthesis((job,),input_units=30000)[job]
    start=monotonic()
    run=await agent.run(review_id=uuid4(),job_id=job)
    elapsed=monotonic()-start
    diagnostics=run.result.diagnostics
    usage=ledger.usage()
    final=diagnostics.final_response
    return dict(case=name,provenance='scripted_integration',independent_accuracy_established=False,
        status=run.result.status.value,fully_inspected_files=diagnostics.fully_inspected_files,
        total_files=diagnostics.total_files,observed_lines=diagnostics.observed_lines,total_lines=diagnostics.total_lines,
        synthesis_admitted=diagnostics.synthesis_context.admission=='admitted' if diagnostics.synthesis_context else False,
        schema_valid=final.failure_stage=='none' if final else False,
        proposal_count=diagnostics.preparation.proposal_count,evidence_validated=len(run.validated_findings),
        rejected_proposals=diagnostics.preparation.rejected_proposals,
        scripted_false_positive_retained=len(run.validated_findings) if false_positive else 0,
        model_attempts=usage.model_attempts,tool_attempts=usage.tool_attempts,input_reserved=usage.input_reserved,
        output_reserved=usage.output_reserved,reported_tokens=None,elapsed_seconds=round(elapsed,6),network_attempts=0)


def run_matrix():
    with _offline_only():
        async def run():
            return [await _run_case(*case) for case in _cases()]
        return asyncio.run(run())


def main(argv=None):
    argparse.ArgumentParser(description='Fixed offline prepared-engine evaluation; no live option.').parse_args(argv)
    try:
        result=run_matrix()
    except Exception:
        print('Offline engine evaluation failed; no complete baseline established.')
        raise SystemExit(1) from None
    print(json.dumps(result,indent=2))
