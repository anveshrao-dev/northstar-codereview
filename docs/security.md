# Security boundaries

## Supported scope

Existing demos and live examples use explicitly approved fictional source. Synthetic approval is a
trusted caller assertion, not an automatic content classifier. Source text,
comments, filenames and tool observations remain untrusted data. Prompt-injection
tests verify unavailable actions cannot gain execution authority; they do not
prove that a model cannot be misled within its permitted task.

[Bounded real-source intake](real-source-intake.md) adds fixed allowlists,
private atomic storage and independent hash-bound import/transmission receipts.
Its automated verification uses generated local test data. Marked real snapshots cannot
use synthetic opt-in. Original-source imports and live transmission remain subject
to separate human approval; no UI or example runner enables them automatically.

There are no source execution, shell, GitHub, scanner, cluster or deployment tools.
The Streamlit app runs scripted reviews, displays saved results, and offers a
separate real-source flow requiring disclosure consent and a live-start action.
It binds to localhost and is not an authenticated multi-user service.
Do not expose it through a public listener or tunnel.

## Provider transport

The existing Nebius adapter requires explicit live authorization and the official
HTTPS Chat Completions endpoint. It rejects other endpoints, disables redirects
and environment-proxy inheritance, removes ambient account headers, disables
tracing, and supplies zero SDK retries. Each conversation owns a separate adapter.

Responses are limited to **131,072 bytes during consumption**. An HTTPX stream
wrapper counts actual raw chunks before the SDK can accumulate the body. An
oversized Content-Length can reject early, but missing or inaccurate lengths do
not bypass chunk counting. Overflow stops before consuming the remaining body.

The adapter sends `Accept-Encoding: identity`. Non-identity Content-Encoding is
rejected before body consumption, with `unsupported_content_encoding`. Compressed
responses are intentionally unsupported; no compressed payload reaches HTTPX's
decoder. This avoids decompression amplification without adding another provider
or changing model-generation parameters. HTTP errors are classified before body
consumption. Timeouts and cancellation preserve connection cleanup.

The bound covers accumulated response bytes; the trusted transport owns an
individual chunk allocation. HTTPS headers, SDK objects and bounded JSON parsing
also consume memory. The installed HTTPX/OpenAI behavior is regression-tested;
upgrades require repeating transport and protocol tests.

Before normalization, the guard rejects unknown assistant/tool fields and any
meaningful unsupported reasoning state. `reasoning` may be absent or exactly null;
all non-null values and reasoning aliases remain rejected. Optional annotations
may be null or an empty list; audio and legacy function_call may only be null.
No meaningful state is silently discarded. Standard tool-call IDs and observations
survive replay. Synthesis alone requests JSON-object mode with tools disabled.

Known credential copies, including encoded JSON forms, are rejected. Dependency
logging is suppressed in provider call contexts. Public errors contain fixed
categories and safe status/field/type labels, not response bodies, headers,
arguments, reasoning, request IDs or exception chains.

## Report and storage safety

Reports omit source snippets and private model/tool history. A deterministic output
policy withholds entire findings containing recognizable credentials in prose or
paths. It does not rewrite a claim while retaining its validation label. Withholding
prevents a completed public projection; safe siblings remain visible. This is
pattern-based defense in depth, with false positives and false negatives, not a
universal secret or employer-information detector.

Markdown renders untrusted prose as literal escaped spans. Streamlit uses text
rendering, not unsafe HTML. Misleading natural language remains possible even when
structural injection is prevented.

Runtime artifacts use no-follow, no-overwrite local writes. JSON/Markdown companions
are individually flushed, not a transactional pair. Saved-result readers bound
artifact sizes and reject incompatible schemas, links and inconsistent identities.
Hashes detect corruption; they do not authenticate provider provenance or prevent
malicious same-user rewriting. Historical evidence status is not a fresh source
observation or a human semantic judgment.

Local approval databases use closed JSON and owner-only files. Decisions are bound
to the exact report and local actor assertion. No decision grants GitHub posting,
merging, deployment or ownership rights. See [architecture](architecture.md).

## Local configuration and publication hygiene

Keep `.env` owner-only (`0600`). Never store actual credentials in `.env.example`,
arguments, logs, documentation or test snapshots. Streamlit secrets, local databases
and sidecars, logs, private records, environments and runtime artifacts are excluded
by `.gitignore`. Forced inclusion can bypass ignore rules; review an explicit file
manifest before any publication.

Dependencies have separate licenses and security lifecycles. Locked versions and
hashes improve reproducibility but do not establish absence of vulnerabilities.
No current external vulnerability-advisory clearance is claimed. Keep all downloads,
upgrades, live inference and publishing explicitly authorized.

## Real-source operator actions

Selection reads bounded metadata only. Consent binds the exact import, manifest,
snapshot and Nebius model for at most 30 minutes, capped by retention. Live start
is separate; it rechecks retention, privacy, hashes, approval and actual provider
identity before requests. There is no automatic dotenv read, transmission grant,
renewal, reimport, repair or retry. The environment loader runs only on explicit
live start. Missing configuration fails closed with values withheld.

Renewal defaults to 30 days (explicitly configurable 1–365) and creates a new
private bundle, preserving the old one and source bytes. Old transmission receipts
do not authorize it. Revoked or tampered archives cannot be renewed. Expiry is
enforced at normal load/tool/model boundaries; it is not deletion or secure erasure.
Retained reports, exported downloads and SQLite checkpoints have no automatic purge.

Real review results are checksummed, size-bounded, private atomic JSON records.
Each transmission approval is consumed once, bound to its exact approval ID,
import/manifest/snapshot identity, provider, model, approved specialist allowlist,
requested specialist subset and run ID. Admission creates an
owner-only file under `.runtime/real_source/transmission_claims/<approval-id>.json`
using exclusive, no-follow creation; the file and parent directories are flushed
before provider startup. Concurrent processes cannot both create the same claim.
Records contain identities and digests, never source or credentials. This directory
is private and ignored along with other runtime state.

Claims survive restarts and remain consumed after startup failure, cancellation,
timeout, crash, or partial claim writes. Neither the same nor a different run ID
can reuse one. Never delete/reset claims to retry; a fresh execution requires a
new explicit transmission approval. Claims do not extend receipt expiry or snapshot
retention and grant no import, human-review or publication authority.

CLI and Streamlit use the same runner gate. Programmatic orchestrator `run()` claims
before invoking model factories. Transmission receipts require an explicit
`specialists` array using `code_quality`, `security`, `test_coverage`, `gitops`, or
`cicd_groovy`. Empty, duplicate, unknown, wildcard and malformed scopes are rejected.
There is no implicit all-specialist grant. Legacy receipts without scope fail
closed; they are not migrated or rewritten. A fresh explicit scoped approval is
required. CLI/UI authorization creates Code Quality-only receipts and exposes no
specialist override. The current real-review profile and budgets are unchanged.

The orchestrator validates its entire deterministic routing decision against the
approved and claimed scope before any model factory starts. It rejects a route
containing an unapproved specialist rather than silently narrowing or expanding
the route. Direct specialists derive their identity from the closed implementation
registry; caller labels cannot turn a Security agent into Code Quality. Model and
tool checks enforce that identity on each invocation. Tool handles are bound to
their specialist and cannot be borrowed by another parallel branch, including
through argument-validation error paths.

Direct specialist callers must enter
`transmission_claims.execution_scope(snapshot, approval, run_id=review_id,
identity=("nebius", model_id), specialists=("code_quality",))` before constructing the provider and specialist,
then call `run(review_id=review_id)` once inside that scope. Model-facing tools
require the active, process-bound scope and recheck persistent claim integrity,
receipt expiry and snapshot retention. A persisted claim is not a resume token;
raw graph invocation cannot substitute for the admitted run. Parallel specialists
share one outer execution, each with a distinct one-use job slot.

This protection assumes the existing trusted local application boundary and local
filesystem exclusive-create/fsync semantics. It is not protection against a user
who rewrites claim storage or bypasses Northstar to call a provider directly.
Backup/restore must preserve consumed claims; rolling them back can restore replay
risk. No automatic claim garbage collection or recovery retry is provided.
Reports are safe projections, not a guarantee that
all proprietary information is absent from model-authored prose. Exact citations
are shown only locally on request and omitted from exports. Human approval is a
report-level local decision, never automatic confirmation of each defect claim.

Offline contract tests use generated snapshots and fake providers. They establish
authorization and execution ordering, not current live configuration or successful
inference. Before a separately authorized real run, verify the retained snapshot's
hashes and retention, explicitly renew only if needed, check credential presence
and exact model/endpoint without printing values, and create a fresh scoped receipt
valid for at most 30 minutes. A renewal, import receipt, clean privacy scan or passed
test suite grants no transmission permission. Missing configuration and human
authorization are operational blockers; provider availability, source coverage,
structured-output compliance and defect correctness remain runtime uncertainties.

## Maintainer review

Before accepting contributions, maintainers should define a private security-reporting
channel and supported-platform policy. Neither a response SLA nor production support
is promised. Do not submit secrets, employer material or private repository contents
in public issues. Contributor provenance and redistribution rights require review.
Northstar is licensed under the [MIT License](../LICENSE); dependencies retain
their separate licenses, and private source snapshots are outside the release scope.
