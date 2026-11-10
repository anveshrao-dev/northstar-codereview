# Product interface

## Design

The existing Streamlit application has five destinations: Overview, New Review,
Review History, Test Lab and Settings. Full-width native sidebar buttons show the
active destination in teal, with a text label identifying the current page. They
retain native keyboard activation and focus semantics.

The wide page uses a 1,280-pixel workspace that shrinks to its parent on narrower
screens. Content uses at most two columns, with a three-metric native summary;
long paths wrap. Disclosure text is bounded to 920 pixels. The sidebar **Appearance**
radio selects Light or Dark. Fresh sessions start Light; normal widget reruns keep
the choice. Switching applies on the same rerun without a reload or server restart,
changes only `appearance:theme`, and starts no review or authorization. It does not
change the operating-system or native Streamlit Settings preference.

`ui_theme.py` holds both closed palettes, shared by native widgets and the pipeline:

| Token | Light | Dark |
| --- | --- | --- |
| Canvas | #F5F7FB | #0B1220 |
| Sidebar | #FFFFFF | #111C2E |
| Surface | #FFFFFF | #1E293B |
| Primary text | #0F172A | #F8FAFC |
| Secondary text | #334155 | #CBD5E1 |
| Metadata | #475569 | #94A3B8 |
| Decorative border | #CBD5E1 | #475569 |
| Control boundary | #64748B | #94A3B8 |
| Accent | #0F766E | #14B8A6 |
| Text/checkmark on accent | #FFFFFF | #0B1220 |

Every status has an explicit text label; pipeline stages also have symbols.
Supported startup theme options provide 16-pixel body text, heading sizes, compact
metrics and restrained corner radii. No remote fonts, assets or background grid.

Installed Streamlit 1.66 has no public session-local theme setter: `st.set_option`
does not permit theme options and `st.context.theme` is read-only. The native
startup theme remains Light. One controlled stylesheet overrides native color pairs
using stable test IDs and React Aria state attributes, including popup portals.
There is no private configuration mutation, JavaScript or browser-storage access.
The in-app selector is authoritative for these application components; native
Streamlit Settings is not a second application theme control.

The stylesheet explicitly sets text-child foregrounds and caption-container opacity
to keep native and application colors consistent. It also covers headings, metrics,
controls, selected states, alerts and plain code. Semantic badges use
application-owned wrappers.
No model/source values enter the stylesheet; untrusted titles remain literal text.
`ui_style.py` retains layout, navigation and finding-title helpers. Recheck native
selectors on Streamlit upgrades. Reduced-motion preferences disable transitions.

Color-pair calculations and AppTest cover both modes and session-state isolation.
They do not establish browser contrast, focus order or full WCAG conformance.
Manual acceptance must check Light → Dark → Light, open dropdowns and tooltips,
selected/disabled controls, keyboard focus, narrow windows, source text, findings,
and pending/finalized human review. Source excerpts remain ephemeral across reruns;
switching themes preserves the selected file/range without silently rereading it.

Overview explains approved imported snapshots and the unsupported GitHub boundary.
A primary View existing reviews action, New review action and expandable seven-stage
process guide lead to supported navigation. The guide describes the workflow; it
does not claim recorded stage events. Explore Northstar offers two compact paths:
Real AI Review opens the newest eligible completed CSV Import and Validation result
from the validated retained-history catalog, with actual coverage and finding counts.
It requires a completed Nebius Code Quality execution with recorded model attempts;
no other sample or partial result is substituted. The genuine-Nebius label describes
the recorded execution; local hashes do not independently authenticate provider
provenance (see the control help and security limitations). Catalog ordering is artifact
modification time, not an inferred completion timestamp. Missing, expired or invalid
records show an unavailable state. Zero findings does not establish defect-free code.
Multi-Agent Test Demonstration selects Java backend and scripted mode in Test Lab;
it does not run the harness automatically. Predetermined responses, multiple
specialists and the intentional false positive remain clearly labeled as offline.
Recent retained reviews remain visible.
Total reviews, Completed and Need attention are counts of the bounded validated
history, not lifetime totals. Attention uses overall execution status even when
the graph report completed but provider cleanup failed. Each saved-review card
has an Open review action tied to that exact import/run identity. The detail view
offers Back to reviews without starting a new review or requiring UUID input.
Numbered card labels distinguish otherwise identical saved reviews. These are
positions in the current bounded list, not persistent IDs or completion order.
New Review exposes only the supported Code Quality profile and keeps exact source
selection, provider disclosure consent and execution confirmation separate.
History is a bounded, read-only projection of validated private records; selection
revalidates retention and report identity. File-modification timestamps are labeled
as filesystem metadata, never as inference/completion times. Missing timestamps
remain unavailable. Test Lab isolates scripted and historical fictional evaluations.
Settings owns retention actions and technical/privacy information.

Results prioritize sample, specialist, overall execution status, analyzed coverage
and findings. View Pipeline, View Source Files and Inspect Findings are immediately
available above the longer explanation and stay accessible from every tab. Coverage
context and run metadata remain collapsible; incomplete-review warnings stay visible.
Seven native tabs are Summary, Pipeline, Findings, Coverage, Source Files, Human Review
and Technical Details. Summary has a compact stage strip and next human action; Pipeline has the
only full stage view: seven native buttons styled as connected cards, with an advanced keyboard selector and bounded recorded details. Streamlit renders
all tab content on rerun; switching tabs does not alter report identity or consent. The compact
Who reviewed this code? cards distinguish selection, recorded execution, incomplete work,
not-selected capabilities and unavailable metadata. Coverage retains the detailed counters.
Provider/model identity comes only from the saved result; legacy omissions stay unavailable.
Scripted results identify their predetermined offline responses explicitly.
Findings retain all original claims, evidence status and remediation. Exact source
is fetched only by explicit action. Report decisions use the existing SQLite
workflow; no individual-finding adjudication is invented. Advanced details remain
available in collapsed sections. Incomplete coverage and meaningful errors remain
visible. No backend authorization, model, graph, budget or evidence contract changes.

Verification uses generated imports, scripted providers and AppTest. A bounded
localhost health/page-shell smoke test is separate from browser visual acceptance;
no live model calls are needed. Browser visual acceptance remains an operator task.
Check wide and narrow windows, tab/Enter/Space navigation, visible focus, card
wrapping, consent controls and export placement before accepting the visual design.


## Review pipeline

The seven-stage graphic uses horizontal connected cards on wide screens and a
vertical connected layout below 1,000 pixels. Compact strips in Overview, History
and Summary wrap on narrower screens. The same `pipeline_stages` projection and
report-bound human checkpoint lookup serve every real-review view. No separate
Overview heuristic exists. Native expanders provide keyboard-accessible drill-down.

Pipeline indicators use the same palette: Light/Dark completed #15803D/#22C55E,
failed #B91C1C/#F87171, partial or blocked #B45309/#F59E0B, running #1D4ED8/#60A5FA,
and pending/not run/unavailable #64748B/#94A3B8. Every node also has a symbol and
visible status text. Text uses the primary foreground on its theme's panel surface.
Application-owned HTML contains only closed stage names/statuses; source, model
prose and evidence details never enter it. Ordered-list semantics and decorative
icons support assistive technology, but actual browser/screen-reader behavior still
requires manual acceptance. No scripts, external assets or generated CSS selectors.


The real saved-result view shows seven ordered stages: source identity, authorization,
chunk preparation, specialist analysis, evidence validation, report persistence and
human review. `ui_pipeline.py` is a pure projection of validated saved contracts;
it has no I/O, model calls, authority or persistence. The UI reuses the normal
retention/integrity loader and the existing report-bound human-review lookup.

Each stage is independently labeled. Recorded model attempts plus transmission
lineage establish historical authorization, not permission to rerun. Prepared ranges
are not analyzed ranges. Accepted analysis is not semantic correctness. Retained
citations establish source correspondence only. Empty findings leave finding-level
citation validation unavailable. A partial review can still have a successfully
persisted report, while rejected proposals never appear as validated findings.
Missing legacy telemetry remains unavailable. Explicitly skipped specialist work
is Not run; a missing checkpoint for the selected/default local thread is Not started.
A pending checkpoint is Awaiting human review; a lookup or integrity failure stays
Unavailable. A generic proposal rejection does not prove an evidence-check failure.
Only explicit `invalid_evidence` gate telemetry can make that stage red. That existing
bucket can also include a wrong specialist category, so the detail describes a
proposal/evidence validation-gate rejection, not proof of an incorrect quotation. A recorded manual decision is
report-level, regardless of whether its disposition is approve, reject or request
changes; no decision becomes a defect verdict or publication approval.

The execution call has no intermediate UI event feed. While it runs, the existing
status container reports only an aggregate running attempt and explicitly states
that the current stage is unavailable. It updates from the returned durable result.
No progress percentages, invented timings, polling threads or event storage exist.
Test Lab remains clearly labeled scripted integration or historical fictional live
results; it does not create real-source history or authorization.

## Guided configuration

Four visible steps are Choose code, Select reviewer, Approve code sharing and
Run review. Profile selection and token/deadline ceilings are in a collapsed advanced
section; the chosen profile name remains visible. Standard remains the default. Profile changes
invalidate prior UI consent as before. Only Code Quality is supported for live real
sources; the other four cards describe capabilities without enabling execution.
Source file scope and snapshot identities remain in explicit technical details.
Settings shows fixed profiles read-only; it does not edit budgets or model settings.

## Demonstration modes

Test Lab exposes five implemented, offline-tested specialist capabilities separately
from actual run selection. Only Code Quality is supported for live imported-source
execution. History labels all three samples as imported demonstration source code.
Completed and incomplete zero-finding records have different explanations; neither
asserts defect-free code. Record timestamps and execution profiles remain available
in collapsed details. Result summaries precede the saved pipeline; finding limitations
remain accessible alongside claims, with human review and sanitized export preserved.
The [saved review walkthrough](#saved-review-walkthrough) explains how to inspect
recorded results without starting another review.

## Test Lab job presentation

Test Lab retains fictional scripted checks and separately labeled historical live
results. Harness PASSED means a validated report and evaluation were returned. It
does not assert that all specialist workflows or semantic judgments passed. Specialist
PASSED/FAILED/PARTIAL/NOT RUN reflect their recorded completion status. Selection is
not execution. Running status is shown only around an active scripted harness call.
On harness failure, stale prior results are cleared from that scenario/mode view.

Predeclared fixture matches, false-positive cases and negative controls remain
separate from workflow status. Finding severity is shown separately, so a critical
finding does not turn a successful test execution red. No historical artifacts are
modified, and no new live execution action is added.

## Recorded diagnostics and local source inspection

Seven native Streamlit buttons are the connected stage cards; there is no duplicate
full graphic or second row of stage buttons. An advanced keyboard selector also opens
stage details. Selection persists on
rerun and resets when a different review opens. The details panel
shows evidence provenance, numeric counters and closed diagnostic categories from
the saved terminal result. It is labeled Recorded stage details: there is no persisted
chronological event stream. Missing legacy telemetry stays unavailable. Unit details
are capped at twelve entries and show schema/finish/truncation metadata where recorded.
Free-form model prose, prompts, source payloads, authorization identifiers and claim
contents are not displayed in this panel. Transmission lineage is historical evidence;
the panel does not independently inspect claim consumption or grant new authority.
Human status comes from the separate current report-bound checkpoint lookup.

Overview and History show compact stage-count summaries, with the mini graphic in a
collapsed Pipeline summary expander. The detailed Summary tab retains the compact strip; Pipeline uses interactive native
cards. Counts are terminal states, not chronological progress.

Source Files contains an explicit local source-files viewer independent of finding
citations. It revalidates the retained import and saved report and compares the second
loaded snapshot's import identity, sample, manifest digest and report snapshot identity.
Only manifest-owned HEAD paths are offered. Existing descriptor protections, privacy
checks, source hashes, strict range schema, 100-line limit and serialized 16-KiB output
limit remain in force. There are no arbitrary roots, archival reads or source downloads.
Expired, missing, altered or invalid bundles are unavailable.

The user selects a file and clicks Read selected source lines for an initial bounded
range. Previous/Next range controls remain within the captured file; custom inclusive
ranges are under Advanced. Inspect code on a finding selects its validated citation
and opens Source Files with up to five surrounding lines per side, limited to 100
total. Multiple citations have an explicit selector. An unavailable citation fails
closed rather than opening unrelated code. Citations longer than 100 lines cannot
be opened as one context window.
Source is shown as plain text only for that render, with absolute physical LF line
numbers prefixed; a literal `>` marks cited lines without relying on color. Source
is never cached in session state, executed or transmitted.
Local viewing does not change analyzed coverage, findings, approval or execution claims.
This relies on the existing local unauthenticated single-operator boundary, not a new
enterprise identity system. Import permission remains separate from model disclosure.

Human Review shows status, specialists, analyzed coverage, findings count and limitations
before decisions. Buttons open Source Files and Findings, including cited evidence.
Its three sections are Report summary, Inspect evidence and Make a local decision.
Accept report, Request follow-up and Reject report map to the unchanged backend
values `approve`, `request_changes` and `reject`. No decision is preselected; submission
remains disabled until the reviewer explicitly chooses one.
Incomplete reviews still require explicit acknowledgement. Checkpoint IDs, report
revision binding and resume controls are in Advanced; a persisted decision replaces
the pending UI on rerun. No decision is created merely by opening this view.
The header offers three primary navigation actions. Native tab state survives widget reruns. Its existing checkpoint bindings and explicit decisions are unchanged.
Test Lab's Java example retains its predefined expected-match and false-positive
claims. Explicit source buttons resolve only the pinned fictional dataset and exact
report-bound locations. These are scripted fixture demonstrations, not live discoveries.

## Saved review walkthrough

After a retained real report passes the existing loader's validation, Walk through
this review opens Pipeline at Source verified. Start walkthrough is also available
within Pipeline. Previous stage and Next stage are bounded to the seven stages;
native stage buttons and the selector remain available. The selected stage has
an outline and text marker. Selection is session-only,
persists across ordinary reruns and resets on a different review. End walkthrough
returns to normal exploration without changing the report.

The label is “Saved review walkthrough — reconstructed from recorded results.”
The numbering is an inspection order, not a chronological log. Missing telemetry
is unavailable, partial outcomes stay partial, and Human review is a current
report-bound checkpoint lookup. No timing, events or model work are invented.
Unsaved or rejected retained reports do not offer this action. See
[live visibility boundaries and future plan](live-review-visibility.md).

Source snapshots are frozen approved file sets. Opening a report never reruns it.
A new authorized review of a usable snapshot creates a new execution; a consumed
transmission approval cannot be reused. Expiry requires supported retention renewal
and fresh authorization. Viewing and walkthrough controls do not grant either.
