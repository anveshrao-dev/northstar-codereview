"""Sequential bounded units inside the specialist graph; no provider of its own."""
import asyncio
from time import monotonic

from pydantic import ValidationError
from langchain_core.messages import AIMessage

from .chunk_review import UnitEnvelope, admission, build_plan
from .final_output import final_response_diagnostics, parse_final_message
from .models import AgentName, PreparationDiagnostics, ToolResult, SynthesisContextDiagnostics, AnalysisUnitDiagnostics
from .prepared_source import context_units, resolve_proposal
from .proposal_validation import validate_observed_proposal
from .synthesis_context import measure_context


def update_ranges(session, indices, **changes):
    ranges=list(session.analysis.ranges)
    for i in indices:
        ranges[i]=ranges[i].model_copy(update=changes)
    session.analysis=session.analysis.model_copy(update={'ranges':tuple(ranges)})


def record_unit(session, unit_number, indices, classification, schema_status, message=None, reported_usage=None):
    usage = message.usage_metadata if message is not None else None
    tokens = (usage['input_tokens'], usage['output_tokens']) if usage else reported_usage
    item = AnalysisUnitDiagnostics(unit_id=unit_number+1, range_indices=indices,
        classification=classification, schema_status=schema_status,
        response=final_response_diagnostics(message) if message is not None else None,
        reported_input_tokens=tokens[0] if tokens is not None else None,
        reported_output_tokens=tokens[1] if tokens is not None else None)
    session.analysis=session.analysis.model_copy(update={'units':(*(session.analysis.units or ()), item)})


async def execute_units(agent, state):
    from .agents.code_quality import InspectionEvent
    s=state['session']
    agent._authorize_model()
    limits=agent.limits
    ledger=agent.execution_budget
    plan=build_plan(agent.snapshot, execution_profile=getattr(ledger, 'execution_profile', None))
    used=ledger.usage() if ledger else None
    allowance=ledger.synthesis_input_allowance(s.job_id,agent.synthesis_reservation) if ledger else 30000
    s.analysis=admission(plan,
        model_calls=min(limits.max_model_calls,ledger.limits.max_model_requests-used.model_attempts) if ledger else limits.max_model_calls,
        tool_attempts=min(limits.max_tool_attempts,ledger.limits.max_tool_attempts-used.tool_attempts) if ledger else limits.max_tool_attempts,
        input_units=min(limits.max_input_tokens,ledger.limits.max_input_tokens-used.input_reserved) if ledger else limits.max_input_tokens,
        output_units=min(limits.max_output_tokens,ledger.limits.max_output_tokens-used.output_reserved) if ledger else limits.max_output_tokens,
        output_per_call=min(limits.output_tokens_per_call,ledger.limits.output_tokens_per_call) if ledger else limits.output_tokens_per_call,
        protected_input=allowance)
    if s.analysis.admission=='denied':
        s.synthesis='budget_denied'
        s.stop('Complete analysis plan cannot fit authorized capacity; no model request dispatched.')
        return state
    failed=False
    max_context=0
    proposal_count=0
    rejected=0
    for unit_number,indices in enumerate(plan.units):
        observed=[]
        for i in indices:
            agent._authorize_model()
            if monotonic()>=s.deadline or s.tool_attempts>=limits.max_tool_attempts or not agent._reserve_tool(s):
                s.stop('Analysis preparation interrupted by execution budget; review incomplete.')
                failed=True
                break
            r=plan.ranges[i]
            ref=plan.references[i]
            name='get_source_metadata' if ref is None else 'read_source_lines'
            args=dict(file_path=r.file_path,side=r.side.value)
            if ref is not None: args.update(start_line=r.start_line,end_line=r.end_line)
            observation=agent.tools[name].invoke(dict(name=name,args=args,id=f'unit-read-{s.tool_attempts}',type='tool_call'),config={'callbacks':[]})
            result=ToolResult.model_validate_json(observation.content)
            s.events.append(InspectionEvent(tool_name=name,arguments=args,observation=result))
            # Require tools to reproduce the plan exactly; previews are not evidence.
            valid=result.status=='ok' and (tuple(result.evidence)==(ref,) if ref is not None else result.data.get('line_count')==0)
            if not valid:
                update_ranges(s,(i,),outcome='failed',reason='inspection_failed')
                s.stop('Analysis source observation did not validate; review incomplete.')
                failed=True
                break
            update_ranges(s,(i,),prepared=True)
            s.observed.extend(result.evidence)
            observed.extend(result.evidence)
            if ref is None: s.empty_files.add((r.side,r.file_path))
        if s.route=='finalize': break
        max_context=max(max_context,context_units(observed,agent._snapshot_id))
        # Preflight messages contain byte-identical references verified above.
        # No prior responses/history are repeated or summarized.
        s.messages=list(plan.messages[unit_number])
        s.chunk_final=unit_number==len(plan.units)-1
        measurement=measure_context(s.messages,[])
        s.synthesis_context=SynthesisContextDiagnostics(before=measurement,after=measurement,
            protected_allowance=allowance if s.chunk_final else 0,compaction_attempted=False)
        s.synthesizing=True
        s.synthesis='request_failed'
        s.final_response=None
        s.route='model'
        calls_before = s.model_calls
        usage_before = (s.reported_input_tokens, s.reported_output_tokens, s.usage_unknown_calls)
        try:
            agent.model=agent._model_source.bind_tools(())
            agent._tools_disabled=True
        except Exception:
            s.stop('Tool-free analysis binding unavailable; review incomplete.')
        else:
            try:
                await agent._model(state)
            except asyncio.CancelledError:
                agent._analysis_interruption='deadline' if monotonic()>=s.deadline or ledger and ledger.remaining()<=0 else 'cancelled'
                if ledger: ledger.stop(agent._analysis_interruption)
                s.stop('Analysis interrupted; earlier validated units retained; review incomplete.')
        if s.route!='parse':
            if s.model_calls > calls_before:
                # Only current-unit messages exist here; never reuse a previous response.
                rejected_message = s.messages[-1] if isinstance(s.messages[-1], AIMessage) else None
                known_usage = ((s.reported_input_tokens-usage_before[0], s.reported_output_tokens-usage_before[1])
                               if s.usage_unknown_calls == usage_before[2] else None)
                record_unit(s, unit_number, indices,
                    'invalid_output' if rejected_message is not None else
                    'rejected_before_parse' if known_usage is not None else 'request_failed',
                    'invalid' if rejected_message is not None else 'unavailable',
                    rejected_message, known_usage)
            update_ranges(s,indices,outcome='failed',reason='request_failed')
            failed=True
            break  # No retry, repair or request after transport/budget failure.
        message=s.messages[-1]
        s.final_response=final_response_diagnostics(message)
        envelope,error=parse_final_message(message)
        schema_status='invalid'
        accepted=[]
        if error is None:
            try:
                unit=UnitEnvelope.model_validate(envelope.model_dump())
            except ValidationError:
                error='invalid_proposal_structure'
            else:
                schema_status='valid'
                proposal_count+=len(unit.findings)
                if unit.outcome=='insufficient_evidence': error='insufficient_evidence'
                for data in unit.findings:
                    proposal,error=resolve_proposal(data.model_dump(mode='json'),agent.snapshot,observed)
                    if error: break
                    finding,error=validate_observed_proposal(proposal,agent.snapshot,observed,
                        categories={'correctness','maintainability'},agent_name=AgentName.CODE_QUALITY)
                    if error: break
                    accepted.append(finding)
        if error:
            rejected+=len(envelope.findings) if envelope is not None else 0
            reason=('insufficient_evidence' if error=='insufficient_evidence' else
                    'invalid_output' if error in ('empty_response','invalid_json','top_level_type_mismatch',
                        'missing_required_fields','invalid_field_types','unexpected_tool_call','truncated_response',
                        'unknown_validation_failure','invalid_proposal_structure') else 'invalid_evidence')
            update_ranges(s,indices,outcome='failed',reason=reason)
            record_unit(s, unit_number, indices, reason, schema_status, message)
            s.limitations.append(f'Analysis unit rejected: {error}; no findings retained from this unit.')
            failed=True
        else:
            record_unit(s, unit_number, indices, 'findings', schema_status, message)
            update_ranges(s,indices,outcome='analyzed')
            for finding in accepted:
                if not any(f.finding_id==finding.finding_id for f in s.validated): s.validated.append(finding)
        s.route='model'
    s.preparation=PreparationDiagnostics(selected_files=len(agent.snapshot.manifest.files),read_attempts=s.tool_attempts,
        context_units=max_context,proposal_count=proposal_count,rejected_proposals=rejected)
    if failed:
        if s.synthesis not in ('budget_denied','tool_call_rejected'):
            reasons={r.reason for r in s.analysis.ranges if r.outcome=='failed'}
            s.synthesis=('insufficient_evidence' if reasons=={'insufficient_evidence'} else
                         'malformed_output' if reasons & {'invalid_output','invalid_evidence'} else 'request_failed')
    else:
        s.synthesis='succeeded'
    s.route='finalize'
    return state
