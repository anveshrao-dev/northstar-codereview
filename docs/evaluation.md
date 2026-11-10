# Evaluation methodology and observed scope

## Separate quality gates

Unit/contract tests check deterministic behavior. Offline scripted integration uses
fake model decisions with real LangGraph orchestration, inspection tools and evidence
checks. Streamlit AppTest exercises the local UI without a browser server. These
checks do not measure independent model accuracy.

Live provider evaluations test model-selected actions and generation against pinned
fictional source. Semantic judgments are separate human assertions bound to the
exact dataset, scenario, report and finding. Source-line overlap creates a candidate,
not a true positive. A correct quotation can support an incorrect defect claim.

The three full scenarios are Java/Maven/JUnit, Angular/TypeScript/Jasmine, and
Jenkins-on-AKS. They are incomplete static fictional contexts, not deployed systems
or three real pull requests. Expected labels and safe controls were predefined;
labels and scripted responses never enter model prompts. Earlier Java evaluations
used a trusted Code Quality skill containing a similar refund example. That example
has since been removed, but those historical results remain workflow/protocol
checks rather than an independent accuracy benchmark.

## Recorded live status

Controlled runs with `Qwen/Qwen3-30B-A3B-Instruct-2507` have established a synthetic
tool-call round trip and JSON-object compatibility. Full Java, Angular and
Jenkins-on-AKS reviews remained partial. Valid JSON did not ensure complete source
coverage, envelope compliance or accepted proposals. Provider-confirmed truncation
was observed in Angular synthesis.

The isolated GitOps run inspected its fictional configuration, admitted protected
synthesis, and reached structured proposal validation. Its proposal failed exact-source
evidence validation. No retained evidence-validated live finding has yet been
established. These are bounded observations, not reliability or accuracy guarantees.
Saved live artifacts remain ignored local data and are not included in a checkout.

## Scoring and controls

The version-2 semantic evaluation envelope consumes version-4 review reports.
Missing judgments remain pending. Precision/recall/F1 require complete appropriately
adjudicated results; unavailable denominators remain null. Partial/failed/skipped
reviews cannot establish a clean negative control. A demonstrated control violation
can still be recorded. Explicit scripted judgments are labeled test assertions,
never live-model performance. Duplicate matches do not inflate recall.

The Java scripted baseline deliberately preserves an incorrect finding. UI labeling
requires matching report/finding identity, category, severity, scripted provenance
and `judgment_origin=scripted_fixture`; arbitrary model wording cannot assign a
false-positive label. Evidence validity and claim correctness remain independent.

## Runner interfaces and limits

`examples/evaluate_offline.py` has no live mode or credentials. It blocks sockets
and writes sanitized results to `.runtime/evaluation`. `examples/evaluate_live.py`
requires `--allow-live`, one predefined scenario, correct environment/model, and
unchanged dataset/source pins. There are no arbitrary source paths or publishing
actions. Numeric overrides can lower limits, never increase the selected profile.

| Profile | Model / tool attempts | Input / output reservations | Output per request | Request / evaluation time |
|---|---|---|---|---|
| default (all three scenarios) | 6 / 12 | 120,000 / 6,000 | 1,000 | 20 / 60 seconds |
| java (java_backend only) | 9 / 15 | 180,000 / 9,000 | 1,000 | 20 / 60 seconds |
| isolated GitOps | 4 / 6 | 80,000 / 4,000 | 1,000 | 20 / 60 seconds |

All guarded live modes use zero retries and repairs. Protected synthesis requests
still need token and deadline admission. Input units are conservative serialization
estimates, not billable tokens. Unknown usage remains unknown. There is no verified
pricing table or exact monetary cap; local cancellation cannot revoke remote billing.

Example for a separately approved single full evaluation, from the checkout root:

```sh
env -u NEBIUS_API_KEY -u NEBIUS_MODEL -u NEBIUS_BASE_URL \
  PYTHONDONTWRITEBYTECODE=1 \
  LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false \
  TMPDIR="$PWD/.cache/tmp" \
  uv run --offline --no-sync --no-config --no-python-downloads \
    --cache-dir "$PWD/.cache/uv" --env-file "$PWD/.env" \
    .venv/bin/python -B examples/evaluate_live.py \
    --scenario java_backend --budget-profile java --allow-live
```

For Angular or Jenkins use `angular_frontend` or `jenkins_aks` with the default
profile. This documentation authorizes no execution or automatic scenario sequence.
The full live envelope is version 3; isolated GitOps is version 1; incompatible old
schemas require their original reader rather than relaxed validation.

`examples/check_nebius.py` is an opt-in two-request protocol probe; its explicit
`--diagnose-first-response` mode permits one request and no tool execution.
[Structured-output probing](structured-output-probe.md) uses one tool-free request.
The separate Code Quality live runner reviews only its pinned inventory fixture,
with four model attempts, six tool attempts, zero retries/repairs, 30-second request
and 90-second run limits. None is part of ordinary tests or UI execution.

Future accuracy work needs independently authored held-out inputs, reviewed rights,
predeclared labels, adequate bounded coverage, and human semantic adjudication.
Passing tests or observing source does not substitute for these requirements.
