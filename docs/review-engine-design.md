# Bounded prepared-source review engine

## Architecture and scope

Five specialists share a LangGraph inspection/synthesis/validation workflow.
The outer graph delegates deterministically. Prepared real Code Quality reviews
use bounded analysis units inside that same specialist graph. Other specialists
and existing synthetic model-directed workflows retain their existing behavior.
The explicit programmatic legacy path remains for historical characterization;
it retains authorization and budget checks and is not a UI/CLI switch.

Source access, planning, budget admission, citation resolution, aggregation and
human decisions are deterministic. The model supplies behavioral analysis and
proposed remediation. Validating citations does not prove defect correctness.
Scripted completion is not evidence of live-model accuracy or production readiness.

## Planning and admission

After transmission authorization and the durable single-use execution claim,
a pure planner inventories every approved path, side and source hash in stable
order. It preserves exact LF-based line numbers and CRLF content. Ranges are
nonoverlapping; empty files require metadata observations. Unsupported files,
files beyond the eight-file preparation bound and oversized individual lines
remain explicit exclusions, never invisible omissions.

Ranges respect the existing 100-line and 3,500 serialized-unit read bounds.
Adjacent ranges are packed into contexts of at most 12,000 serialized units.
Before provider construction, the real runner checks the complete plan against
the explicitly selected, authorization-bound execution profile. The default
standard profile retains four model attempts, six tool attempts, 80,000 input
reservation units, 4,000 output reservation units and 1,000 output tokens/request. Earlier units
must fit while the 30,000-unit final-input hold remains protected; the final
unit must fit that hold. Projections include exact instructions, schema and
message framing, using the existing byte-based estimator, not provider billing.

An infeasible plan produces a private capacity result without provider startup
or inference. It reports required/available capacity and closed denial labels.
Its execution claim remains consumed; admission failure never authorizes retry.
The actual CSV and BM25 file distributions require at least seven and eight
reads respectively, even without the stricter slice bound. Chunking cannot make
those snapshots fit a six-tool profile. The named extended_csv (nine tools) and
extended_bm25 (eleven tools, 90,000 input units) profiles require explicit selection
and matching authorization; they are never inferred from the sample or failure.
See [profile limits and consent](real-source-intake.md#local-real-review-workflow).

## Execution and output validation

Each admitted unit reads source through the existing snapshot-bound tools.
Tool observations must reproduce planned references exactly before disclosure.
Each model request receives only its unit, the trusted specialist instructions,
compact output schema and dependency limitations. Previous responses are not
repeated, summarized or used as fabricated evidence. Tools are disabled and
JSON-object mode is enabled for these analysis requests. There is no additional
model aggregation call, retry or output repair.

The required envelope contains `findings`, including when empty, and optional
`outcome`. A unit permits at most two proposals, two evidence pointers per
proposal, concise text fields and at most two assumptions. These transport
bounds narrow output demand but do not guarantee a 1,000-token response fits.
Provider-confirmed truncation, malformed JSON, invalid schemas and unsupported
tool calls remain hard rejections. JSON-schema API mode is not enabled.

Pointers resolve only within that unit's observed ranges, preserving snapshot,
path, side, inclusive line bounds and exact quotation bytes. The unchanged
canonical Pydantic/category/evidence validators then apply. A unit is accepted
transactionally: one invalid proposal prevents all its proposals from being
retained or its ranges from receiving analyzed coverage. Explicit insufficient
evidence remains partial. Independent later units may still succeed within the
original plan; transport/budget interruption stops further requests.

Only fully validated unit findings are aggregated. Exact canonical finding IDs
are deduplicated; existing severity/priority handling remains unchanged. Earlier
successful findings survive a later unit failure or cancellation. No claim is
automatically human-approved. Cross-unit behavior and excluded dependencies
remain unverified and are disclosed in the report.

## Coverage, privacy and failure semantics

Optional versioned diagnostics distinguish planned ranges, successfully prepared
ranges, validated model-analyzed ranges, failures and skipped work. Preparation
is independent of terminal analysis status. Each line is counted once per
metric. Empty files retain explicit range records even though they have zero
lines. Completed empty findings require successful analysis of every approved
range; incomplete reviews never establish a clean negative control.

Historical observed-source metrics remain observed-source metrics, with absent
new fields omitted to preserve old serialization. Streamlit retains its existing
navigation and displays analyzed versus prepared coverage for new results.
Diagnostic records contain identities, bounds, counts and closed labels, not
source text, provider responses or reasoning. Exact evidence remains available
only through existing authorized local viewing.

Every tool/model boundary retains authorization and retention checks. Each
operation still obtains atomic admission from the existing shared ledger; a
preflight estimate is not a reservation and concurrent usage can still stop a
run. The final unit consumes its owner's protected synthesis reservation.
20-second request and profile-bound 60/70/90-second overall deadlines, zero retries/repairs,
provider cleanup and private result persistence remain in force.

Offline tests use generated snapshots and scripted providers through actual
LangGraph nodes and local tools. They cover full admitted reviews, capacity
rejection, transactional failures, evidence integrity, cancellation, replay
prevention and historical compatibility. No live reliability claim follows
from those tests; a separately authorized live evaluation is still necessary.

Chunk instructions describe the explicitly selected immutable profile, while
the ledger remains the authority for admission. Each unit evaluates its supplied
ranges after guarded source preparation; it does not need to prove whole-repository
completeness. An adequately inspected unit with no supported issue may return an
empty findings list. Missing information that materially prevents responsible
local analysis still requires `insufficient_evidence` and leaves that coverage
partial. Neither outcome establishes defect-detection accuracy.

New analysis reports optionally retain `units`: one-based unit identifiers,
zero-based indices into the report's ranges, closed-vocabulary response and
schema classifications, sanitized finish metadata, and reported input/output
tokens (null when unavailable). Received responses rejected before parsing can
retain known usage without retaining their content. The current response schema
has no safe abstention-reason code, so reasons are recorded as `unavailable`;
model prose is never copied into diagnostics. Older reports omit `units` and
retain their serialized shape. No prompt, source, tool argument, or raw provider
response is stored in these diagnostics.
