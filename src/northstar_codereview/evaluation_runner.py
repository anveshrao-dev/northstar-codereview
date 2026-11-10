"""Offline integration runner. Scripted claims are NOT model-accuracy evidence.

No provider import, credentials, source execution, live option, or approval
transition. Expected labels never enter specialist instructions. Assertions are
separate fixture data and bind only exact emitted claims in the resulting report.
"""

import argparse
import asyncio
import json
import os
import socket
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from time import monotonic
from threading import Lock
from typing import Literal, Self
from unittest.mock import patch
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage
from pydantic import Field, model_validator

from .agents.code_quality import RunLimits
from .evaluation import EvaluationError, evaluate, fingerprint, render_evaluation
from .evaluation_artifacts import write_runtime_artifacts
from .evaluation_data import SUITE_PATH, _capture, load_dataset, load_scenario
from .evaluation_models import (EvaluationResult, EvidenceTelemetry, FindingJudgment, Identifier, LabelLocation)
from .local_source import PROJECT_ROOT
from .models import AgentName, Category, Contract, EvidenceReference, FindingProposal, Severity, Text
from .orchestrator import OrchestrationLimits, ReviewOrchestrator
from .reporting import ReviewReport, build_report


class ScriptClaim(Contract):
    id: Identifier
    agent_name: AgentName
    title: Text = Field(max_length=200)
    category: Category
    severity: Severity
    confidence: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)
    explanation: Text
    suggested_remediation: Text
    assumptions: tuple[Text, ...]
    locations: tuple[LabelLocation, ...] = Field(min_length=1)


class ScriptAssertion(Contract):
    claim_id: Identifier
    verdict: Literal['match', 'false_positive', 'out_of_scope']
    expected_id: Identifier | None = None
    control_id: Identifier | None = None


class ScenarioScript(Contract):
    id: Identifier
    claims: tuple[ScriptClaim, ...] = Field(max_length=16)
    assertions: tuple[ScriptAssertion, ...]

    @model_validator(mode='after')
    def consistent(self) -> Self:
        ids = {c.id for c in self.claims}
        if (len(ids) != len(self.claims) or len({a.claim_id for a in self.assertions}) != len(self.assertions)
                or any(a.claim_id not in ids for a in self.assertions)):
            raise ValueError('Invalid scripted identity')
        return self


class ScriptSuite(Contract):
    schema_version: Literal[1] = 1
    scenarios: tuple[ScenarioScript, ...]

    @model_validator(mode='after')
    def unique(self) -> Self:
        if len({s.id for s in self.scenarios}) != len(self.scenarios):
            raise ValueError('Duplicate script identity')
        return self


def load_scripts() -> ScriptSuite:
    captured = _capture(SUITE_PATH, ('scripts.json',))
    try:
        return ScriptSuite.model_validate_json(captured.sources['head', 'scripts.json'])
    except ValueError:
        raise EvaluationError('invalid_scripts') from None


LIMITS = RunLimits(max_provider_retries=0, max_output_repairs=0)
_RUN_LOCK = Lock()


def baseline_configuration(scripts: ScriptSuite, mode: str) -> dict:
    if mode not in {'baseline', 'recovery', 'failure'}:
        raise EvaluationError('invalid_mode')
    return dict(runner='scripted_integration', mode=mode, scripts_id=fingerprint(scripts),
                specialist_limits=LIMITS.model_dump(mode='json'),
                orchestration_limits=OrchestrationLimits().model_dump(mode='json'),
                scoring_version='semantic-adjudication-v1', network='blocked', tracing=False,
                matching='agent/category and all anchors are candidates; exact report-bound semantic judgment required',
                duplicates='exact predictions fold; repeated matches earn one TP per label',
                incomplete='no full accuracy or clean controls for missing, partial, withheld or pending results',
                denominators='undefined ratios are null; out-of-scope findings excluded and counted',
                evidence='unique proposals passing schema/scope/exact-source validation, not defect correctness')


class ScriptedModel:
    """A deterministic test double; observations come from real inspection tools."""
    def __init__(self, snapshot, claims, *, mode='baseline'):
        self.snapshot = snapshot
        self.claims = claims
        self.mode = mode
        self.calls = 0
        self.emitted: dict[str, FindingProposal] | None = None

    def bind_tools(self, tools):
        self.tools = tools
        return self

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        if self.mode == 'failure':
            raise RuntimeError('Scripted model failure')
        if self.calls == 1:
            return AIMessage(content='', tool_calls=[dict(name='list_snapshot_files', args={}, id='inventory')])
        if self.calls == 2:
            calls = []
            for i, entry in enumerate(self.snapshot.manifest.files):
                count = len(self.snapshot.sources[entry.side, entry.file_path].splitlines())
                calls.append(dict(name='read_source_lines', args=dict(file_path=entry.file_path, side=entry.side.value,
                    start_line=1, end_line=count + int(self.mode == 'recovery' and i == 0)), id=f'read-{i}'))
            return AIMessage(content='', tool_calls=calls)
        observed = {}
        for message in messages:
            if isinstance(message, ToolMessage):
                payload = json.loads(message.content)
                for ref in payload.get('evidence', ()):
                    observed[ref['side'], ref['file_path']] = EvidenceReference.model_validate(ref)
        emitted = {}
        for claim in self.claims:
            evidence = []
            for loc in claim.locations:
                ref = observed[loc.side, loc.file_path]
                if not ref.start_line <= loc.start_line <= loc.end_line <= ref.end_line:
                    raise EvaluationError('script_location_unobserved')
                text = ''.join(ref.snippet.splitlines(keepends=True)[loc.start_line-ref.start_line:loc.end_line-ref.start_line+1])
                evidence.append(EvidenceReference(snapshot_id=ref.snapshot_id, **loc.model_dump(), snippet=text))
            emitted[claim.id] = FindingProposal(**claim.model_dump(exclude={'id', 'agent_name', 'locations'}), evidence=evidence)
        self.emitted = emitted
        # No fabricated token counts; agent records unknown usage for this double.
        return AIMessage(content=json.dumps({'findings': [p.model_dump(mode='json') for p in emitted.values()]}))


@dataclass(frozen=True)
class ScriptedRun:
    evaluation: EvaluationResult
    report: ReviewReport


async def run_scripted(dataset, scenario_id: str, *, mode='baseline') -> ScriptedRun:
    """One run at a time: the socket guard temporarily owns process-wide hooks."""
    if not _RUN_LOCK.acquire(blocking=False):
        raise EvaluationError('concurrent_run_not_supported')
    try:
        with _offline_only() as network:
            return await _run_scripted(dataset, scenario_id, mode=mode, network=network)
    finally:
        _RUN_LOCK.release()


async def _run_scripted(dataset, scenario_id: str, *, mode, network) -> ScriptedRun:
    scripts = load_scripts()
    configuration = baseline_configuration(scripts, mode)
    scenario = next((s for s in dataset.scenarios if s.id == scenario_id), None)
    script = next((s for s in scripts.scenarios if s.id == scenario_id), None)
    if scenario is None or script is None:
        raise EvaluationError('unknown_scenario')
    snapshot = load_scenario(scenario)
    if any(c.agent_name not in scenario.specialists for c in script.claims):
        raise EvaluationError('script_applicability_mismatch')
    models = {}
    def factory(name):
        if mode == 'failure':
            # Initialization failure exercises the orchestrator's failed branch;
            # a caught model-request error normally yields a partial specialist.
            raise EvaluationError('scripted_initialization_failure')
        active_mode = mode if mode != 'recovery' or name == scenario.specialists[0] else 'baseline'
        models[name] = ScriptedModel(snapshot, tuple(c for c in script.claims if c.agent_name == name), mode=active_mode)
        return models[name]
    started = monotonic()
    result = await ReviewOrchestrator(snapshot, model_factory=factory, approved_synthetic=True,
                                      specialist_limits=LIMITS).run(review_id=uuid4())
    elapsed = monotonic() - started
    report = build_report(result, snapshot=snapshot.manifest)
    claims = {c.id: c for c in script.claims}
    judgments = []
    # Exact proposal equality includes prose, category, confidence, severity,
    # assumptions and all evidence. A matching line or title is insufficient.
    for assertion in script.assertions:
        name = claims[assertion.claim_id].agent_name
        model = models.get(name)
        emitted = model.emitted.get(assertion.claim_id) if model and model.emitted is not None else None
        matches = [f for f in result.validated_findings if f.agent_name == name and f.proposal == emitted
                   and any(r.finding_id == f.finding_id for r in report.findings)]
        for finding in matches:
            judgments.append(FindingJudgment(dataset_id=fingerprint(dataset), scenario_id=scenario_id,
                report_id=report.report_id, finding_id=finding.finding_id, verdict=assertion.verdict,
                expected_id=assertion.expected_id, control_id=assertion.control_id,
                origin='scripted_fixture', adjudicator='predeclared_script'))
    evidence = None
    if set(models) == set(scenario.specialists) and all(m.emitted is not None for m in models.values()):
        proposed = {(name, fingerprint(p)) for name, model in models.items() for p in model.emitted.values()}
        evidence = EvidenceTelemetry(report_id=report.report_id, proposed=len(proposed),
                                     validated=len({f.finding_id for f in result.validated_findings}))
    evaluation = evaluate(dataset, scenario_id, report, judgments=judgments, evidence=evidence,
        configuration_id=fingerprint(configuration), run_kind='scripted_integration', elapsed_seconds=elapsed,
        network_attempts=network['attempts'])
    return ScriptedRun(evaluation, report)


def render_summary(results) -> str:
    lines = ['# Scripted integration evaluation', '',
             'These results test plumbing and predefined assertions, not independent model accuracy.', '',
             '| Scenario | Review | TP | FP | FN | Precision | Recall | F1 |',
             '|---|---|---:|---:|---:|---:|---:|---:|']
    for result in results:
        result = EvaluationResult.model_validate_json(render_evaluation(result))
        q = result.quality
        values = [result.scenario_id, result.review_status, q.true_positives, q.false_positives, q.false_negatives,
                  q.precision, q.recall, q.f1]
        lines.append('| ' + ' | '.join('unavailable' if v is None else str(round(v, 4)) if isinstance(v, float) else str(v)
                                       for v in values) + ' |')
    lines.extend(['', 'Null metrics remain unavailable. Missing/incomplete reviews cannot pass negative controls.',
                  'Evidence validates source support, not defect correctness. Token usage is unknown for scripted models.',
                  'Fresh run IDs and measured latency vary; scoring is deterministic for identical inputs.', ''])
    return '\n'.join(lines)


def write_artifacts(results, configuration) -> tuple:
    """New files only, descriptor-relative and no-follow within fixed runtime root."""
    safe = [json.loads(render_evaluation(r)) for r in results]
    if not safe or any(r['configuration_id'] != fingerprint(configuration)
                       or r['run_kind'] != 'scripted_integration' or r['workflow']['network_attempts'] != 0 for r in safe):
        raise EvaluationError('configuration_mismatch')
    # Accept only this runner's closed configuration, never arbitrary free text.
    expected = baseline_configuration(load_scripts(), configuration.get('mode'))
    if configuration != expected:
        raise EvaluationError('configuration_mismatch')
    payload = dict(schema_version=1, configuration=configuration,
                   network_attempts=sum(r['workflow']['network_attempts'] for r in safe), results=safe)
    return write_runtime_artifacts(PROJECT_ROOT, payload, render_summary(results))


@contextmanager
def _offline_only():
    network = {'attempts': 0}
    def deny(*args, **kwargs):
        network['attempts'] += 1
        raise EvaluationError('network_attempt_blocked')
    with ExitStack() as stack:
        for obj, name in ((socket, 'create_connection'), (socket, 'getaddrinfo'),
                          (socket.socket, 'connect'), (socket.socket, 'connect_ex')):
            stack.enter_context(patch.object(obj, name, deny))
        stack.enter_context(patch.dict(os.environ, LANGSMITH_TRACING='false', LANGCHAIN_TRACING_V2='false'))
        try:
            yield network
        finally:
            if network['attempts']:
                raise EvaluationError('network_attempt_blocked') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline scripted integration only; no provider or live option.')
    parser.parse_args(argv)
    try:
        with _offline_only():
            dataset = load_dataset()
            async def run_all():
                return [await run_scripted(dataset, scenario.id) for scenario in dataset.scenarios]
            runs = asyncio.run(run_all())
            results = [run.evaluation for run in runs]
        paths = write_artifacts(results, baseline_configuration(load_scripts(), 'baseline'))
        print(render_summary(results))
        for path in paths:
            print(path.relative_to(PROJECT_ROOT))
    except (EvaluationError, ValueError, OSError):
        # No traceback, raw validation input, source, or model content at CLI boundary.
        print('Offline evaluation failed; no complete baseline was established.')
        raise SystemExit(1) from None
