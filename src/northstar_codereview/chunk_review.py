"""Pure bounded planning and compact analysis contracts. Never executes source.

Planning reads only an already authorized in-memory snapshot. It credits no
coverage. Runtime tools must reproduce each reference before model disclosure.
"""
import json
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from typing import Annotated

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import Field, StringConstraints

from . import prepared_source as source
from .final_output import FinalEnvelope
from .models import AnalysisDiagnostics, AnalysisRange, AgentName, EvidenceReference
from .synthesis_context import input_reservation
from .real_source_contracts import SampleId
from .execution_profiles import checked_profile

SCOPE_NOTES = {
    SampleId.PORTFOLIO_RISK: 'Portfolio contracts and supporting test helpers are excluded. Arithmetic review only; no application execution.',
    SampleId.CSV_IMPORT: 'Actual CSV datasets are excluded; assess supplied application and test source only.',
    SampleId.BM25_RETRIEVAL: 'Ingestion, embeddings, indexes and additional helpers are excluded; assess supplied source only.',
}


ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]


class UnitFinding(source.CompactFinding):
    title: ShortText = Field(max_length=200)
    explanation: ShortText
    suggested_remediation: ShortText
    assumptions: tuple[ShortText, ...] = Field(default=(), max_length=2)
    evidence: tuple[source.EvidencePointer, ...] = Field(min_length=1, max_length=2)


class UnitEnvelope(FinalEnvelope):
    findings: list[UnitFinding] = Field(max_length=2)


@dataclass(frozen=True)
class AnalysisPlan:
    ranges: tuple[AnalysisRange, ...]
    references: tuple[EvidenceReference | None, ...]
    units: tuple[tuple[int, ...], ...]
    messages: tuple[tuple[BaseMessage, ...], ...]
    input_units: tuple[int, ...]


def unit_messages(snapshot_id, refs, empty_paths=(), scope_note="Dependencies outside this snapshot are unavailable.", execution_profile=None):
    skill = files('northstar_codereview').joinpath('skills/code-quality.md').read_text(encoding='utf-8')
    skill = skill.replace('approved fictional', 'approved application')
    profile_note = ''
    if execution_profile is not None:
        p = checked_profile(execution_profile)
        profile_note = (f'Authorized profile: {p.name}; model attempts <= {p.max_model_requests}; '
                        f'tool attempts <= {p.max_tool_attempts}. Runtime admission and the budget ledger enforce limits; '
                        'this text grants no capacity or additional access.\n')
    instruction = (
        '\nBounded analysis phase: tools are disabled. Source below has already been read by guarded tools. '
        'These unit-mode instructions supersede tool-discovery and whole-snapshot completion directions above. '
        'Analyze only the supplied approved ranges; whole-repository completeness is not required for a local result. '
        'Return findings:[] when the supplied source is adequately inspectable and no supported issue is identified. '
        'Use insufficient_evidence only when missing information materially prevents responsible analysis of this unit, '
        'not merely because no defect is found or unrelated dependencies are unavailable. '
        'Missing dependencies and cross-unit behavior are unverified; identify specific missing dependencies in '
        'existing proposal assumptions when relevant. Do not invent claims or add envelope fields to explain abstention. '
        'The following compact schema overrides quotation-format instructions only: cite reference_id and '
        'inclusive observed start_line/end_line; the runtime resolves exact source quotations. '
        'Return a JSON object with findings, including when empty. At most two concise proposals; '
        'no extra fields, Markdown, retries or repair. The output ceiling is 1,000 tokens, not a guarantee of fit.\n'
        +profile_note+'Trusted scope limitation: '+scope_note+'\nFinal response JSON schema:\n'+json.dumps(UnitEnvelope.model_json_schema(),separators=(',', ':')))
    return (SystemMessage(content=skill+instruction),
            HumanMessage(content='Approved snapshot '+str(snapshot_id)+'. Empty files (data): '+json.dumps(empty_paths)),
            source.source_message(refs,snapshot_id))


def build_plan(snapshot, *, execution_profile=None):
    """Cover every approved identity; unsupported/oversized ranges stay explicit."""
    entries = tuple(sorted(snapshot.manifest.files,key=lambda e:(e.file_path,e.side)))
    selected = set((e.side,e.file_path) for e in source.selected_files(snapshot,AgentName.CODE_QUALITY))
    ranges, refs = [], []
    for position,entry in enumerate(entries):
        raw = snapshot.sources[(entry.side,entry.file_path)]
        if sha256(raw.encode('utf-8')).hexdigest() != entry.sha256:
            raise ValueError('Snapshot source identity mismatch')
        text = source.lines(raw)
        reason = ('file_limit' if position >= source.MAX_FILES else
                  'unsupported_file' if (entry.side,entry.file_path) not in selected else None)
        previews = {ref.start_line: ref for _,ref in source.planned_reads(snapshot,(entry,)) if ref is not None} if not reason else {}
        start = 1
        while start <= len(text) or not text and start == 1:
            ref = previews.get(start)
            end = len(text) if reason else ref.end_line if ref is not None else start
            failure = reason or ('oversized_line' if text and ref is None else None)
            ranges.append(AnalysisRange(snapshot_id=snapshot.manifest.snapshot_id,file_path=entry.file_path,
                side=entry.side,sha256=entry.sha256,start_line=start if text else 0,end_line=end if text else 0,
                outcome='skipped' if failure else 'planned',reason=failure))
            refs.append(ref)
            if not text: break
            start=end+1
    units, current = [], []
    for index,r in enumerate(ranges):
        if r.reason: continue
        candidate=[refs[i] for i in (*current,index) if refs[i] is not None]
        if current and source.context_units(candidate,snapshot.manifest.snapshot_id)>source.MAX_CONTEXT_UNITS:
            units.append(tuple(current)); current=[]
        current.append(index)
    if current: units.append(tuple(current))
    messages=tuple(unit_messages(snapshot.manifest.snapshot_id,[refs[i] for i in unit if refs[i] is not None],
        tuple(ranges[i].file_path for i in unit if refs[i] is None),
        SCOPE_NOTES[snapshot.real_source.import_approval.manifest.sample] if snapshot.real_source else
        'Dependencies outside this snapshot are unavailable.', execution_profile=execution_profile) for unit in units)
    return AnalysisPlan(tuple(ranges),tuple(refs),tuple(units),messages,
                        tuple(input_reservation(list(m),[]) for m in messages))


def admission(plan, *, model_calls=4, tool_attempts=6, input_units=80000, output_units=4000,
              output_per_call=1000, protected_input=30000):
    tools=sum(r.reason is None for r in plan.ranges)
    estimates=plan.input_units
    reasons=[]
    if not plan.units or any(r.reason for r in plan.ranges): reasons.append('scope')
    if tools>tool_attempts: reasons.append('tool_attempts')
    if len(plan.units)>model_calls: reasons.append('model_attempts')
    if sum(estimates)>input_units: reasons.append('input_reservation')
    if estimates and (sum(estimates[:-1])>input_units-protected_input or estimates[-1]>protected_input):
        reasons.append('protected_input')
    if len(estimates)*output_per_call>output_units: reasons.append('output_reservation')
    allowed=not reasons
    return AnalysisDiagnostics(admission='admitted' if allowed else 'denied',
        denial_reasons=tuple(reasons),available_input_units=input_units,available_output_units=output_units,
        required_tool_attempts=tools,available_tool_attempts=tool_attempts,
        required_model_attempts=len(plan.units),available_model_attempts=model_calls,
        projected_input_units=estimates,protected_input_units=protected_input,
        ranges=tuple(r if allowed else r.model_copy(update={'outcome':'skipped','reason':r.reason or 'capacity'}) for r in plan.ranges))
