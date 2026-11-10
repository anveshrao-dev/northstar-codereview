# Architecture and contracts

## Specialist execution

Code Quality accepts correctness/maintainability proposals; Security accepts
security; Test Coverage accepts tests; GitOps accepts gitops; CI/CD & Groovy
accepts cicd. Each specialist is independently callable through `run()` and shares
the existing bounded LangGraph implementation. Trusted packaged Markdown skills
load per specialist; reviewed source cannot select or replace them.

The outer `ReviewOrchestrator` validates one immutable snapshot, selects applicable
specialists deterministically, creates separate conversations, dispatches parallel
branches, collects results at a barrier, validates identities, and finalizes.
Static routing uses language suffixes and conservative platform indicators. YAML
alone does not establish Kubernetes, and Groovy alone does not establish a deployed
Jenkins Shared Library. Unsupported scope prevents an overall completed claim.

Parallel deliveries use an append-only tuple reducer. Exact duplicates fold;
conflicting duplicates invalidate the affected specialist. Job IDs and attribution
come from runtime state, never model output. Failures remain isolated where possible.
Each specialist's messages and source-bearing events stay private to its invocation.
This is in-memory coordination, not durable exactly-once processing.

## Intake and inspection

`ReviewRequest` validates a local target or a future GitHub repository/PR target;
validation neither fetches content nor grants access. The trusted local application
selects a source root inside the editable checkout. Intake uses descriptor-relative
no-follow opens, regular-file checks, hard-link rejection, and identity/stamp
revalidation. It requires POSIX protections and a quiescent application-owned tree;
it is not a transactional filesystem snapshot or a sandbox for hostile Python code.

Limits are 50 files, 1 MiB per file, 10 MiB total, 500 traversed entries, 16 levels,
and 512 UTF-8 bytes per canonical source path. Callers may lower these limits.
UTF-8 text is captured without newline translation. Binary/control-byte inputs,
metadata/private paths and unsafe entries fail closed; they are not silently skipped.

`CollectedSnapshot` retains a copied read-only source mapping and frozen manifest.
Exact bytes, canonical paths and sides determine hashes and snapshot identity.
Tools never reopen source files. Local intake produces HEAD; contracts also support
BASE references for a future ingestion adapter.

The separate [real-source intake boundary](real-source-intake.md) accepts only
three predefined sample IDs and hash-bound import receipts. It preserves the
project-local collector's confinement. `CollectedSnapshot.real_source` carries
typed provenance; optional transmission receipts are checked by existing tools,
specialist constructors/model calls and the orchestrator. No graph, routing,
evidence, report-identity or publication authority is replaced. Original-source
import and inference have not been exercised by this feature's offline tests.

| Tool | Purpose |
|---|---|
| list_snapshot_files | Bounded manifest pagination |
| read_source_lines | Exact captured lines and evidence |
| get_evidence | Exact evidence reference for captured lines |
| get_source_metadata | Hash, byte count and physical line count |

Tool schemas cannot select filesystem roots or snapshots. Reads allow at most 100
lines and serialized results at most 16 KiB, including citation duplication. Errors
contain closed categories, recovery hints and optional trusted line counts, never
raw arguments or exceptions. Missing source is not successful inspection.

Before the first model request, bounded metadata supplies valid paths, hashes,
sides and physical line counts. The 8 KiB preparation limit preserves a stable
prefix and reports omissions. Metadata never counts as observed source evidence.
The fictional model-directed path still selects inspection tools and arguments.
Real snapshots default to the bounded preparation stage described below.

One deterministic correction can lower an otherwise valid `read_source_lines`
end bound to a verified captured EOF when the requested range overlaps the file.
It cannot change the path, side, start line or snapshot, and cannot repair unknown
paths or unsafe arguments. The correction consumes another physical tool attempt.
One matching ToolMessage reports the final observation and recovery provenance.
Any recorded inspection failure retains partial status even after recovery.

## Prepared real-source context

The existing specialist graph adds `prepare_source` between trusted instructions
and inspection/synthesis. For real snapshots it defaults on in all five specialist
classes; the guarded real-review service, CLI and UI continue to select only Code
Quality. Preparation reuses routing and the authorized source tools after the
single-use claim and specialist scope are active. Each attempt counts against the
same local/shared tool ledger. Expiry is checked before every read and inference.

Stable path/side order, at most eight selected files, 100 lines per read, 3,500
serialized units per slice and 12,000 serialized context units bound preparation.
The current real Code Quality path plans every approved range and admits the
complete plan before provider startup. Each unit reads its planned ranges through
guarded tools, verifies exact reference equality, and receives an independent
tool-free JSON analysis request. No extra aggregation call or model-selected
follow-up is added. Oversized or unsupported ranges remain explicit exclusions.
Empty files require successful metadata. No reviewed source is executed.

Prepared coverage records successful observations; analyzed coverage additionally
requires a valid unit result and evidence validation for any proposals. An empty
findings list can represent successful local analysis. Explicit insufficient
evidence remains partial. Cross-unit and missing-dependency behavior remain
unverified. The chunk loop keeps earlier accepted units' findings when a later
unit is rejected; this does not guarantee final report persistence. The legacy
prepared path and other specialist workflows retain their existing bounded inspection behavior.

Prepared synthesis uses a Pydantic-generated compact transport: required `findings`,
optional `outcome`, ordinary proposal fields and evidence pointers containing only
`reference_id`, `start_line`, `end_line`. The runtime resolves these exclusively
against observed immutable ranges, then applies the unchanged canonical proposal,
category and exact-evidence validators. This explicit transport is not JSON repair
or acceptance of missing fields. Citation-format instructions explicitly override
packaged quotation formatting; all evidence and stopping rules remain in force.

Programmatic `prepared_context=False` retains the historical model-directed path
for compatibility tests, with the same authorization/budgets. It is not a UI/CLI
option. No strict provider JSON-schema mode is enabled or assumed compatible.

## Synthesis and budgets

Inspection alternates model requests and permitted tool observations. A final
synthesis node reserves an opportunity to produce structured output before local
request/round limits are exhausted. Synthesis disables tools and selects only
`response_format={"type":"json_object"}`. It preserves the full conversation and
matching tool-call IDs. No JSON-schema mode, fence stripping or automatic repair
is used in guarded live evaluations.

The ordinary specialist API has separate configurable bounded retry/repair limits;
guarded live runners explicitly set both to zero. SDK retries are always zero.
Default specialist ceilings are five model attempts, eight physical tool attempts,
four tool rounds, 30-second requests and a 120-second run. The orchestrator can use
individual budgets alone or the shared ledger; guarded live runners always use it.

Shared admission checks and increments are atomic. Failures, cancellation, invalid
calls and source-read corrections count toward their appropriate budgets. Owner-bound
synthesis request holds protect opportunities from inspection consumption. The
isolated GitOps runner also reserves 30,000 input units inside its 80,000 total.
The real-source runner protects the same 30,000-unit hold within the explicitly
authorized profile's input ceiling: 80,000 units for `standard` and `extended_csv`,
or 90,000 units for `extended_bm25`. The hold is part of that ceiling, not extra capacity.
Other fictional live profiles use request-slot holds without that input hold.

Input reservations count complete serialized message history and tool definitions
as UTF-8 bytes plus 1,024 framing units on every request. They are not a tokenizer,
actual billing or a monetary ceiling. Reported usage and unknown attempts remain
separate. Admitted work is not refunded; unspent holds may be released. Output and
deadline limits still apply. Cooperative cancellation cannot guarantee remote
processing or billing has stopped. See the runner guides for fixed profile limits.

## Findings and reports

A final envelope requires `findings`, even when empty. Optional `outcome` is
`findings` or `insufficient_evidence`; the latter requires an empty list. The prompt
schema is generated from Pydantic. The non-chunked proposal-validation path evaluates
proposals individually; an invalid proposal does not itself discard valid siblings.

The current real-source chunk-analysis path instead accepts findings only after
the entire unit envelope and every proposal pass validation. An invalid envelope,
any rejected proposal, or an `insufficient_evidence` outcome rejects the whole unit:
no findings from that unit are retained, including any validated earlier in the
same unit, and its ranges are not counted as analyzed. A valid empty `findings`
outcome can count as analyzed coverage; it is distinct from insufficient evidence.
Within the chunk loop, findings from earlier accepted units remain in the
accumulated result when a later unit is rejected. The review remains incomplete;
finalization and report persistence are separate gates, not guarantees supplied
by proposal validation.

`FindingProposal` rejects authority fields. Runtime gates check specialist category,
observed ranges, snapshot/file/side identity, captured hashes, line bounds and exact
full-line quotations. LF defines physical lines; whitespace, CRLF and terminal
newlines matter. Coverage may merge adjacent observations, but a single citation
must fit within one observation. Page-spanning citations can therefore be rejected.

Only trusted validation creates `ValidatedFinding`. Unchecked Pydantic construction
must never be used on external input. `evidence_status=validated` establishes source
correspondence; `claim_status=unverified` preserves the separate semantic question.
A complete review requires inspected coverage and no recorded validation or tool
failure. Synthesis success alone cannot upgrade an incomplete review.

`build_report` is deterministic and makes no model call. It orders findings by
proposed severity, source anchor, category and identity. It preserves opposing or
overlapping claims; overlap does not establish duplication or correctness. Report
schema 4 retains source locations/digests, usage, synthesis and inspection outcomes,
limitations and safety notices. Source snippets, messages and raw tool events are
not report fields. Content hashes detect changes, not authorship or approval.

## Durable local review

`ApprovalWorkflow` receives a completed or partial sanitized report after agents
finish. Its graph interrupts for an explicit decision, then validates and records
that decision. Starting, resuming and reading approval state never invokes agents.

Decisions bind review identity, report revision/hash and a trusted local actor
assertion. Partial acknowledgment is mandatory for partial reports. New revisions
supersede pending older reviews; repeated final decisions and mismatched identities
are rejected. These checks are not authentication or publishing authority.

SQLite transactions cover checkpoint advancement and the decision record with
`BEGIN IMMEDIATE`, full synchronization, bounded busy waits and no automatic retry.
Storage is restricted to `.runtime` and `.sqlite3`, with owner-only new files,
no-follow/link checks and checksummed closed JSON serialization. No pickle,
arbitrary constructors, dynamic imports, model histories or credentials are stored.
This saver supports one synchronous graph, full snapshots and a root namespace;
it is not a general-purpose LangGraph checkpoint backend.

There is no encryption-at-rest service, migration, retention/pruning, backup,
multi-user authentication or distributed exactly-once guarantee. Same-user malicious
replacement is outside the trust boundary. An interrupted initial schema creation
can require explicit operator recovery. A checksum is not a signature.

## Real application workflow

The real-source UI and `examples/review_real_source.py` use the same service.
Metadata selection, local retention replacement, external-disclosure authorization,
and one live run are separate explicit actions. They never recollect original
source. `load_import` validates the persisted snapshot before the existing
`CodeQualityAgent.run()` graph receives a separate transmission receipt.
The initial three sample profiles select only Code Quality, without changing
outer-orchestrator routing or other specialist implementations.

Explicit named profiles retain four model attempts, 4,000 output reservation
units, 1,000 output tokens/request, a 20-second request timeout and a protected
30,000-unit synthesis input hold. `standard` permits six tools, 80,000 input units
and 60 seconds; `extended_csv` permits nine tools, 80,000 input units and 70 seconds;
`extended_bm25` permits eleven tools, 90,000 input units and 90 seconds. There is
no automatic escalation, retry or repair. See [engine design](review-engine-design.md).

Private run records bind the exact import, manifest, selected profile and
specialist-scoped transmission approval. The approval is atomically consumed
through a durable single-use claim before provider construction or disclosure.
Reusing the same approval with either the same or a different run ID is rejected,
including after failure or restart. A new execution requires fresh explicit
consent. This is a local single-user boundary, not distributed exactly-once
inference. Provider cleanup is bounded to two seconds after execution.

Retention replacement verifies archived bytes and creates a new atomic bundle,
new import receipt and lineage record. The prior bundle is preserved. The new
manifest digest and import ID invalidate reuse of old transmission approvals.
Normal loading never bypasses retention. Only the explicit replacement operation
can inspect an expired archive to revalidate it; it never grants transmission.
