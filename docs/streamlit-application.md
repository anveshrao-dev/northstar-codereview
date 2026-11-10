# Local Streamlit application

This guide describes implemented local functionality and deferred integration boundaries.

## Component status

| Component | Status | Northstar boundary |
|---|---|---|
| Deep Agents / LangGraph orchestration | Alternative implementation | Explicit specialist and outer LangGraph graphs preserve deterministic routing, isolation and budgets. Deep Agents is not installed. |
| Specialist delegation | Implemented | Five independently callable specialists and bounded parallel dispatch. |
| LangChain tools | Implemented | Four snapshot-bound read-only tools; no arbitrary shell or source execution. |
| Markdown skills / on-demand loading | Partially implemented | Fixed trusted packaged skills load per specialist. Dynamic discovery and repository-provided skills are deferred. |
| Virtual filesystem | Alternative implementation | Immutable hashed snapshots replace a general writable agent filesystem. |
| GitHub PR metadata / ingestion | Partially implemented | Typed pinned-PR boundary exists; retrieval is absent and UI fields are disabled. |
| Regex search | Missing / deferred | Existing tools inspect explicit ranges; no regex execution added. |
| Sandboxed Bandit / Semgrep | Missing / deferred | No scanner packages, subprocesses or execution controls. |
| Persistent review memory | Partially implemented | SQLite approval checkpoints and decisions exist; cross-review agent memory is absent. |
| PR metadata middleware | Partially implemented | Typed repository/revision contracts exist; remote enrichment is absent. |
| Cost middleware | Partially implemented | Usage, unknown attempts and reservations exist; verified pricing is absent. |
| Confidence-gated human review | Partially implemented | Confidence displayed; threshold explicitly unconfigured. Every decision is manual and report-bound. |
| Diff-anchored GitHub comments | Missing / deferred | Validated local file/line references exist; remote diff mapping and posting are absent. |
| Streamlit application | Implemented | Scripted scenarios, saved live results, specialist status, findings, evaluation context and manual local review. |

## Navigation and presentation

The existing `streamlit_app.py` opens Overview. The sidebar has five destinations:

| Destination | Supported actions |
|---|---|
| Overview | View existing reviews, explore a seven-stage process guide, start New Review and see three grounded saved metrics. |
| New Review | Select an approved import and supported Code Quality profile; separately consent to Nebius disclosure and start one execution. |
| Review History | Open a validated retained real report from an actionable card; inspect findings and coverage, review it locally and export. |
| Test Lab | See five offline-tested specialist capabilities; run fictional scripted checks or view saved fictional live evaluations. |
| Settings | Read local status and provider requirements; explicitly manage retention; inspect advanced/privacy information. |

Native Streamlit controls use a wide page with a responsive 1,280-pixel maximum
workspace, restrained teal accent and bordered cards. The sidebar Appearance
selector offers Light and Dark. A new session defaults to Light, with dark text
on pale backgrounds; Dark uses light text on dark navy backgrounds. The selected
theme persists across reruns in that session. Sidebar buttons identify the active
page. Shared theme tokens and the static, application-keyed styles in `ui_style.py`
provide readable text and focus indicators. No external assets, custom scripts,
model-derived HTML or generated-class CSS selectors are used. Long model prose
and paths render as wrapping text. Results show overall execution status, findings,
source coverage and explicit selected/not-selected specialist cards. Technical identities,
reservations, token usage and diagnostics are collapsed. Exact source remains
behind an explicit evidence button; merely loading a result does not display it.

`streamlit_ui.py` handles navigation and shared results. `real_review_ui.py` keeps
existing consent/start/retention/report actions; `ui_models.py` revalidates reports
before presentation; `review_history.py` adds a read-only index using the existing
`real_review.load_review` authority. No agent or validation framework is duplicated.

History scans at most 12 import candidates per sample, stops directory enumeration
at 4,097 entries (withholds oversized directories), considers at most 32 recent
UUID-named report candidates and at most 16 MiB of recorded report sizes. Existing
private no-follow readers, snapshot integrity/retention and report validators are
reused. Invalid, expired or inaccessible records are withheld. Counts describe this
bounded available set, not lifetime totals. Rows label filesystem modification
time explicitly; it is not a trusted inference or completion timestamp. Selection
uses stable import/run identities and revalidates before view/export. No UUID entry
is required and no inference occurs on navigation or rerender. Cards include a compact seven-stage strip derived from the same projection used
in details. Cards open their exact saved report; Back to reviews restores the list. A disappeared or expired
record cannot leave a stale report or export visible.

Test Lab retains `java_backend`, `angular_frontend` and `jenkins_aks`. Scripted
reviews use fake responses with actual graphs and tools and are clearly labeled.
Saved fictional results still use the existing bounded `.runtime/evaluation`
reader and pinned dataset/report checks. Fictional results never become real-code
history or independent model-accuracy claims. No source is executed.

Findings keep severity order, evidence status, unverified claims and remediation.
A deliberate scripted false-positive label requires trusted report-bound fixture
judgment; arbitrary model text cannot assign it. The existing Java false-positive
and negative-control violation remain visible in Test Lab. No finding is hidden,
reworded or silently promoted to a correct defect. Partial reviews remain warnings;
empty findings never establish a clean negative control.

## Local human review

`ApprovalWorkflow` retains its SQLite transactions, LangGraph interrupt and report
revision/hash checks. Creating a checkpoint and recording a decision require
separate explicit clicks. A partial review requires acknowledgment. A checkpoint
UUID can be loaded only when it matches the displayed report. Finalized or
superseded records cannot be silently overwritten.

“Start human review” creates the same existing SQLite checkpoint and does not
approve anything. Approve accepts the report for local review; Reject declines
it; Request changes records a need for revision without rerunning agents. Resume
and technical checkpoint details are collapsible. Keyboard-operated native
Streamlit controls and visible text labels remain in use; confidence never
automatically selects or submits an approval.

The fixed `local-ui-operator` identity represents an unauthenticated local user,
not enterprise authentication. Decisions authorize local report review only;
they grant no posting, merging or deployment authority. Neither approval nor model
confidence changes evidence status, promotes rejected proposals, or turns an
unverified claim into a verified defect. Production deployment needs authenticated
identities and an explicit authorization policy.

## Launch and verification

Streamlit is installed only in the project environment and pinned in `uv.lock`.
Run from the project root without loading `.env`:

```bash
env PYTHONDONTWRITEBYTECODE=1 \
  LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false \
  TMPDIR="$PWD/.cache/tmp" \
  .venv/bin/python -B examples/local_app.py start
```

The fixed launcher binds to `127.0.0.1`, disables usage statistics and file
watching, preserves CORS/XSRF protections, and suppresses raw error details. Do not
expose this unauthenticated local app through a public listener or tunnel.
Graph and review operations also explicitly disable tracing.

Real Streamlit `AppTest` smoke tests run without opening a server or making
external requests. Tests cover scenario selection, provenance, partial/empty
results, manual decisions, isolated checkpoints and error redaction. The existing
repository-wide socket-denial fixture remains active.

## Deferred integration contracts

These are design boundaries, not empty modules or functional UI controls:

| Extension | Proposed integration contract |
|---|---|
| PR retrieval | Existing `github_boundary` records bind repository and base/head revisions; retrieval produces a verified immutable snapshot before tools run. |
| Regex search | Snapshot ID, bounded path/pattern and limits → `ToolResult` with exact evidence. Require bounded execution and safe-pattern rejection; no shell. |
| Optional scanners | Approved snapshot and versioned sandbox policy → untrusted `FindingProposal` data, still subject to ordinary evidence checks. |
| Virtual workspace | Preserve `CollectedSnapshot` and bounded read tools. Any future write requires a separate explicit capability. |
| Review memory | Scoped sanitized report identities and decisions with consent, retention and tenant boundaries. Recalled text is never current source evidence. |
| Cost tracking | Usage, unknown-attempt counts and a verified versioned pricing table → labeled estimate. Missing data stays unavailable, not zero-cost. |
| Skill discovery | Application-owned allowlist of skill IDs, versions and digests; never automatically trust repository instructions. |
| Diff comments | Pinned PR revisions and validated locations → local draft anchors. Recheck current head and explicit publishing authority in a separate transport step. |

Future pinned GitHub ingestion requires design and offline contract tests before
separately authorized retrieval. Scanners, posting and cross-review memory are
unimplemented. A separate real-source workflow offers explicitly authorized live
inference through the existing provider and Code Quality graph.

## Real application workspace

Select **New Review → Choose an approved source snapshot**, then one of the imported
source samples: Portfolio Risk, CSV Import and Validation, or BM25 Retrieval. These
are source-code examples, not CSV datasets or fetched GitHub pull requests. Only previously imported registered snapshots are
listed; selection never imports original source or authorizes disclosure. Review
the manifest identity, filenames, retention, missing dependencies and fixed profile.
The four steps read Choose code, Select reviewer, Approve code sharing and Run review.
Advanced execution profile and limits are collapsed; Standard remains the default.
The selected profile stays visible, and changing it still invalidates prior UI consent.

Expired snapshots are blocked. In **Settings → Source retention**, the optional local retention action requires its
own checkbox and button, defaults to 30 days, and creates a replacement bundle.
It preserves the prior bundle and source bytes and does not extend transmission.

Transmission consent and live start are separate checkboxes/buttons. Approval
lasts at most 30 minutes within retention and is bound to the selected import,
digest and exact Nebius model. Switching samples/imports cannot reuse consent.
The app never reads `.env`; explicitly load the environment at process launch for
live use. Credentials are read only on Start, never shown. Missing settings block
the review. Page loads and reruns do not launch inference; every run has a durable
claim. Select its retained result in **Review History**, or use **Open in Review History** after execution.

Results use seven native tabs: Summary, Pipeline, Findings, Coverage, Source Files,
Human Review and Technical Details. A compact summary strip and one full graphical pipeline
share recorded state; stage buttons and a selector open a bounded details panel. Standard
Streamlit tab rendering preserves existing session keys and local review persistence.
An explicit local evidence action shows only cited source ranges as literal text.
Exports contain prioritized sanitized report data, usage and the available local
report decision, not source snippets, messages or approval payloads. Defect claims
remain unverified regardless of a report-level decision. No predefined real-source
accuracy labels or negative-control outcomes are invented. Cost is unknown until
reliable pricing and usage support it. Partial coverage is never a clean result.

## Engineering interpretation

Deep Agents supplies higher-level agent conventions; Northstar retains its explicit
LangGraph topology and deterministic boundaries. Scripted tool sequences test
plumbing. A live model choosing tools from schemas is a distinct behavior requiring
separate evaluation. A virtual filesystem may be mutable; snapshots bind citations
to exact captured bytes and revisions. Confidence prioritizes attention but cannot
establish identity or authorization. Separating findings from posting mirrors
enterprise CI/CD separation of analysis, artifacts, human gates and deployment
identities.

### Prepared-source results

Real-code review uses the guarded service's bounded preparation path. Specialist
cards show observed/total lines, fully inspected/total files, preparation tool
attempts and reservation units, proposal/rejection counts, synthesis admission and
safe final-response status when recorded. Historical reports without this data
show it as unavailable. These counters do not establish understanding or defect
correctness. Partial warnings, local human adjudication, unverified claim labels
and unknown cost remain visible. No additional model calls occur on rendering.

See [local delivery](local-delivery.md) for offline checks, explicit environment loading, startup/shutdown and health semantics. The launcher bypasses Streamlit CLI credential activation.

## Visual acceptance boundary

AppTest verifies navigation, consent, selected history identity, partial and empty
results, safe text rendering, evidence actions, human review, export and rerender
behavior. It does not establish actual contrast perception, browser focus order
or narrow-window spacing. After local launch, verify keyboard-only navigation,
visible focus, readable wrapping at desktop/narrow widths, and collapsed technical
sections. Native widget semantics remain in use with a project-local theme. Browser
visual acceptance is pending. A test-owned localhost health/page-shell smoke can
verify startup, but does not establish viewport layout or keyboard focus. No external
requests or model calls are part of UI acceptance.


### Saved execution pipeline

Real saved reports include an ordered, selectable seven-stage pipeline derived
from persisted evidence. Stages can be completed, partial, failed, blocked or
unavailable independently; overall completion never fills missing telemetry.
No retained citations means citation validation is unavailable, not a claim that
all defects were ruled out. Report generation can succeed for a partial review.
Human review is shown only from a matching local report checkpoint/decision.
In-flight attempts show aggregate running state only, since the guarded service
has no UI event stream. See [product interface](product-interface.md) for the
projection boundaries, Light/Dark palettes and accessibility checks.

The [saved review walkthrough](product-interface.md#saved-review-walkthrough)
uses retained results without authorizing transmission, running inference or
recording human decisions. Overview introduces
approved imported snapshots; history distinguishes completed zero-finding results
from incomplete zero-finding results. Neither is described as defect-free code.

### Graphical job outcomes

Overview and History reuse the same compact stage renderer as the saved report
summary; detailed stages use connected cards with a narrow-screen vertical fallback.
Every status includes text and a symbol, not just color. Missing legacy telemetry is
unavailable; explicitly skipped work is not failed. A confirmed missing local
checkpoint is not started, while failed checkpoint reads remain unavailable.

Test Lab job cards distinguish harness execution, specialist review completion,
fixture judgments and severity. PASSED is a workflow label, not measured accuracy
or proof that every assertion succeeded. A finding is not automatically a failed test.
Existing saved results are not rewritten. See [product interface](product-interface.md)
for the rendering boundaries and status semantics.

### Source files and recorded stage details

On a saved real review, choose Pipeline → a native stage card (or the advanced
Select pipeline stage control) for sanitized recorded
counters and interpretation limits. These are terminal report facts, not live logs.
Claim consumption is not freshly inspected by the panel. No timestamps or provider
events are invented. Missing telemetry is explicitly unavailable.

The Source Files tab offers local viewing of retained approved snapshot ranges
even when no findings were retained. Select an approved file and click Read selected
source lines for the initial bounded range. Previous/Next controls browse adjacent
ranges; Go to line and custom bounds remain under Advanced. The service repeats integrity, retention and report-binding
checks for each read, reusing existing bounded source readers. The source is not kept
in session state, exported, executed or sent to a model. At most 100 lines are requested;
the existing output limit can require a smaller range. Displayed range bounds refer to
physical captured lines, prefixed on each displayed line. Source browsing does not update historical agent coverage.

Test Lab → Java backend → Run scripted review → Findings demonstrates the existing
predeclared expected match and deliberately incorrect control claim. Inspect code
opens Source Files at the selected validated citation, with bounded surrounding lines
and literal `>` markers on cited lines. Absolute file line numbers remain visible,
including ranges starting after line 1. Multiple citations have an explicit selector.
Scripted source comes from the pinned fictional dataset only. A citation that cannot
be validated is unavailable, never replaced by unrelated source. No real imported
source or live provider is involved. Human Review remains a report decision, not a
GitHub approval, finding verdict, source edit, model rerun or publication action.

Review headers and compact activity cards distinguish selection from recorded execution.
Provider/model names appear only when saved, with unavailable values preserved for
legacy reports. Human Review starts with no selected decision; explicit local choices
retain the existing backend mappings and durable checkpoint semantics.
