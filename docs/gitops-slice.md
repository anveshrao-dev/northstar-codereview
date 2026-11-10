# Single-specialist GitOps evaluation

`examples/evaluate_gitops_slice.py` invokes `GitOpsAgent.run()` directly. It does
not route or run the outer orchestrator. It reuses the snapshot intake, inspection
tools, owner-bound synthesis reservation, Nebius adapter, report projection, and
semantic evaluation contracts.

The separate `tests/fixtures/gitops_slice` dataset pins one 32-line fictional
Deployment/Service snapshot and predefined labels. The Service selector differs
from the captured pod labels. The Deployment selector matches its own pod template
and is the negative control. External cluster inventory is unknown. Existing
evaluation fixtures and the trusted GitOps skill are unchanged. Labels are used
only by evaluation and never passed to the model. Only snapshot metadata is
prepared; the model must select inspection tools to obtain source content.

## Fixed limits

| Limit | Ceiling |
|---|---:|
| Model attempts, including protected synthesis | 4 |
| Tool attempts | 6 |
| Inspection tool rounds | 3 |
| Input reservation units | 80,000 |
| Protected synthesis input (within the 80,000 total) | 30,000 |
| Output reservations | 4,000 |
| Output tokens per request | 1,000 |
| Request timeout | 20 seconds |
| Evaluation deadline | 60 seconds |
| Provider retries / output repairs | 0 / 0 |

These are separate, lower ceilings; production defaults are unchanged. There are
no CLI budget, model, fixture, or path overrides. Provider cleanup has the existing
separate two-second bound. Local cancellation cannot guarantee remote billing
stops immediately. Token reservations are admission controls, not billing estimates.

The isolated runner reserves a request slot and 30,000 input units for the same
GitOps owner before constructing the provider. Inspections may use only the
remaining 50,000 input units. If the next inspection estimate would cross that
boundary, the existing graph transitions to synthesis with the complete history.
This may leave request slots unused. Shared admission checks and updates occur
under one lock; the specialist-local check protects the same allowance.

Synthesis must fit its own 30,000-unit allowance, the remaining total, output cap
and deadline. It cannot borrow above its hold, even if total capacity remains.
Before admission, the shared synthesis path may remove verified duplicate source
payloads while preserving every message, tool-call ID and full evidence reference.
If the resulting history still exceeds the allowance, it returns partial with
`synthesis_input_allowance`; no source evidence is dropped or repaired. See
[context reduction](review-diagnostics.md#synthesis-input-admission-and-duplicate-source-text).
A successful request converts the hold
into its actual estimated charge and releases only the unused portion. Terminal
cleanup releases an unspent hold. Admitted charges survive failures, cancellation
and lower reported provider usage. Double release is an idempotent no-op; reuse
of a spent or released synthesis permit is rejected.

The shared ledger extension defaults to a zero input hold (the existing request
slot policy). Java, Angular, Jenkins and five-agent defaults are unchanged. Offline
tests may lower the slice ceilings; their hold is capped at the smaller total.
The CLI always uses the fixed profile above and cannot override it.

## Execution, only after separate inference approval

Without `--allow-live`, the runner exits disabled before loading credentials or
fixtures. This command is documented for later approval; preparation does not run it:

```bash
# Run from the checkout root.
env -u NEBIUS_API_KEY -u NEBIUS_MODEL -u NEBIUS_BASE_URL \
  PYTHONDONTWRITEBYTECODE=1 \
  LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false \
  TMPDIR="$PWD/.cache/tmp" \
  uv run --offline --no-sync --no-config --no-python-downloads \
    --cache-dir "$PWD/.cache/uv" --env-file "$PWD/.env" \
    .venv/bin/python -B examples/evaluate_gitops_slice.py --allow-live
```

The runner verifies Python 3.12, the project-local environment, pinned fixture and
label hashes, and the selected Qwen model. It disables tracing explicitly. It
permits only the existing Nebius endpoint through the existing provider. Tools
remain snapshot-bound and read-only. No reviewed source is executed.

Inspection requests retain standard tool calling. Synthesis disables tools and
uses JSON-object mode with the existing Pydantic-generated schema requiring
`findings`, including an empty list. Missing fields are rejected, never inserted.
Pydantic proposal and deterministic evidence checks remain unchanged.

Sanitized JSON and Markdown artifacts use the existing `.runtime/evaluation`
writer. Raw messages, reasoning, tool arguments, and source quotations are not
persisted. Findings remain pending human semantic judgment; citation overlap is
not a true positive. Incomplete inspection cannot pass the negative control.
Even completed source inspection and valid evidence do not prove defect
correctness or establish independent model accuracy.

Offline fake-model tests establish wiring and safety behavior, not live model
reliability. A controlled live attempt reached proposal validation but failed exact-source evidence validation. No retained evidence-validated live finding is established.

Current offline checks cover atomic multi-owner admission, early synthesis,
full replay through the actual adapter with an in-memory transport, exact-source
evidence validation, oversized-history rejection, early completion, failure,
timeout, cancellation and conservative accounting. Synthesis success does not
upgrade incomplete inspection or prove a software defect. Protecting input also
cannot guarantee that an arbitrarily large history fits 30,000 units.
