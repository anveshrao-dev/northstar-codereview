# Real-source evaluation report

Verified locally on 2026-11-08. This report summarizes retained, explicitly
authorized Nebius evaluations of three real sample groups, including three distinct
CSV executions. No new inference was performed for this report.
Private source bundles, authorization records and raw source are not distributed
with this document.

## Objective and method

Evaluate whether the prepared-source workflow can inspect bounded real source,
produce a valid structured result and preserve honest failure reporting. These
are three distinct owner-local application samples, not public pull requests or
an independently labeled accuracy benchmark. Selection favored small application
modules with supporting tests: portfolio arithmetic, CSV ingestion and lexical
retrieval. No reviewed code or imported tests were executed.

Retained results were loaded through `real_review.load_review`, which checks
stored record digests, validated contracts, current snapshot retention and
snapshot/report identity bindings. Their sanitized coverage, usage, finish
metadata and validation outcomes were compared with the supplied run summaries.
The historical summaries and subsequent CSV outcomes were corroborated. Underlying identifiers and artifacts stay
private; local hash checks establish consistency, not signed provenance.

## Architecture under test

The application has five LangGraph specialists: Code Quality, Security, Test
Coverage, GitOps Platform, and CI/CD & Groovy. **Only Code Quality executed in
these real-source evaluations.** The outer multi-specialist orchestrator
was not exercised by these runs. The real-source runner invokes the existing
specialist graph. The original runs used bounded source preparation and optional
follow-up inspection; the current chunk engine plans complete coverage, performs
guarded reads, and validates independent tool-free analysis units. Protected
budgets, Pydantic validation and immutable-source evidence checks remain active.

The four LangChain tools are `list_snapshot_files`, `read_source_lines`,
`get_source_metadata` and `get_evidence`. Prepared observations use these local
source boundaries; deterministic preparation is not itself LLM-selected tool
calling. Compact citation ranges resolve to exact snapshot evidence before
retaining a finding. Synthesis alone uses API `json_object` mode with tools off.
JSON syntax, schema validity, evidence integrity and defect correctness are
independent gates.

## Approved file scope

Paths below identify scope only; this document contains no imported source.

| Sample | Approved files |
|---|---|
| Portfolio Risk | `src/stock_portfolio_analyzer/risk.py`; `tests/test_risk.py` |
| CSV Import | `src/stock_portfolio_analyzer/csv_import.py`; `src/stock_portfolio_analyzer/models.py`; `src/stock_portfolio_analyzer/validation.py`; `tests/test_imports.py` |
| BM25 Retrieval | `src/northstar_atlas/vector_store/bm25.py`; `src/northstar_atlas/vector_store/catalog.py`; `src/northstar_atlas/vector_store/contracts.py`; `src/northstar_atlas/vector_store/records.py`; `tests/test_bm25.py` |

Portfolio contracts and supporting test helpers are excluded. Actual CSV datasets
are excluded. BM25 ingestion, embeddings, indexes and additional helpers are
excluded. An approved file is not necessarily an inspected file.

## Provider and execution boundaries

Provider: Nebius Token Factory. Model: `Qwen/Qwen3-30B-A3B-Instruct-2507`.
The original prepared-source runs used the Code Quality-only standard limits: at most four model attempts,
six tool attempts, 80,000 input reservation units, 4,000 output reservation units,
1,000 output tokens per request, 20-second request timeout and 60-second overall
deadline. A synthesis request and 30,000 input units are protected within these
ceilings. Provider retries and output repairs are zero.

Import approval, retention, specialist-scoped transmission approval, durable
single-use execution claim, human report review and publication are separate
boundaries. No report is automatically human-approved. External tracing and
publishing are disabled. Private artifacts were persisted and provider cleanup
was recorded as closed for all reported runs.

The two extended CSV runs explicitly selected `extended_csv`: four model attempts,
nine tool attempts, 80,000 input units, 4,000 output units, 1,000 output tokens per
request, 20-second request timeout and 70-second deadline. The protected input
hold remains 30,000. Profile and exact ceilings are bound to the transmission
approval and durable claim; no automatic escalation occurs. `extended_bm25` is
offline-verified but has no live evidence with the new profile/corrected prompts.

## Results

| Sample | Status | Observed lines | Fully inspected files | Model / tool attempts | Finish | Retained findings | Seconds |
|---|---|---:|---:|---:|---|---:|---:|
| Portfolio Risk | Completed | 138 / 138 | 2 / 2 | 1 / 3 | `stop` | 0 | 4.23 |
| CSV Import, original | Partial | 213 / 541 | 2 / 4 | 2 / 5 | `length` | 0 | 22.40 |
| BM25 Retrieval | Partial | 159 / 540 | 2 / 5 | 2 / 4 | `length` | 0 | 19.19 |

| Sample | Reported input / output tokens | Input / output reservations | Projected synthesis input |
|---|---:|---:|---:|
| Portfolio Risk | 4,302 / 7 | 18,272 / 1,000 | 18,272 |
| CSV Import, original | 11,895 / 1,037 | 49,068 / 2,000 | 24,926 |
| BM25 Retrieval | 11,544 / 1,036 | 47,680 / 2,000 | 24,180 |

### Extended CSV comparison

The two later runs used the same approved four-file snapshot and fixed extended
profile. The first received valid insufficient-evidence envelopes in all units;
the corrected-prompt run returned valid empty findings in all units.

| CSV execution | Status | Prepared lines | Validated analyzed lines | Model / tool attempts | Proposed / retained findings | Seconds |
|---|---|---:|---:|---:|---:|---:|
| First extended | Partial | 541/541 (100%) | 0/541 (0%) | 3 / 9 | 0 / 0 | 5.08 |
| Corrected prompts | Completed | 541/541 (100%) | 541/541 (100%) | 3 / 9 | 0 / 0 | 4.01 |

| Corrected unit | Source scope | Classification | Finish | Input / output tokens |
|---|---|---|---|---:|
| 1 | CSV implementation and models | Valid empty findings | `stop` | 4,576 / 7 |
| 2 | Validation module and first test range | Valid empty findings | `stop` | 3,882 / 7 |
| 3 | Remaining test ranges | Valid empty findings | `stop` | 4,653 / 7 |

All three corrected responses passed JSON and envelope validation, with no
abstention or truncation. Nine source reads succeeded; no ranges were excluded,
failed or skipped. Total reported usage was 13,111 input and 21 output tokens;
reservations were 58,340 input and 3,000 output units. The first extended run used
12,679 input and 45 output tokens, with 55,724 input and 3,000 output reservations.
Only its last finish reason was retained; the corrected run retains each unit's
sanitized finish/classification/usage metadata. There were no rejected proposals,
timeouts, retries, repairs or unknown-usage attempts in either extended run.

The original runs predate separate unit-analysis telemetry. Their observed lines
must not be retroactively relabeled as validated analyzed lines. Current analyzed
coverage means ranges received an accepted structured unit result; it does not
measure the model's cognitive depth, defect recall or cross-unit reasoning.

Token usage is aggregated across requests, so total output can exceed the
per-request ceiling. Unknown-usage attempts, retries and repairs were zero.
Reservation estimates are admission controls, not provider billing. No reliable
monetary cost was established. Latencies describe individual runs, not a latency
benchmark or service-level guarantee.

## Coverage and validation

Portfolio Risk's three successful preparation reads covered both files. Synthesis
was admitted and produced a valid envelope containing `findings`, with zero
proposals. No evidence-bearing finding was available to validate or adjudicate.
Completion means the bounded workflow completed, not that the code is defect-free.

The original CSV Import run's four preparation reads covered `csv_import.py` and `models.py`.
`validation.py` and `tests/test_imports.py` were not inspected. One subsequent
discovery call did not add source coverage. BM25's three preparation reads covered
`bm25.py` and `catalog.py`; `contracts.py`, `records.py` and `tests/test_bm25.py`
were not inspected. One subsequent discovery call likewise added no coverage.
All recorded tool attempts succeeded; no recovered or unresolved tool errors were
recorded in these runs. Coverage measures observed lines, not semantic understanding.

In the original prepared-source runs, all synthesis requests were admitted below the protected 30,000-unit allowance;
none required context compaction. CSV Import and BM25 returned provider finish
reason `length` and were rejected as `truncated_response` at the final-envelope
stage (`malformed_output` synthesis outcome). Their retained diagnostics do not
establish JSON syntax validity or proposal count. Zero individual rejected
proposals were recorded because no valid final envelope reached that stage;
this does not mean individual proposals passed. Neither partial review establishes
a clean negative control.

## Reviewed-code findings

**No live source-code defects have been validated.** All reported retained finding
counts are zero. There are no supported severity rankings, true-positive counts,
precision/recall scores or human-approved defect claims from these runs. Source
line overlap alone would not prove a defect even if findings existed.

The corrected CSV completion exercised no finding-level exact citation validation
because it proposed no findings. Actual CSV datasets and excluded dependencies
were not evaluated. A valid empty result does not prove clean code. No model
claim was automatically human-approved.

## Prioritized Northstar engineering limitations

These are application engineering limitations, not defects in the reviewed code.

| Priority | Limitation | Evidence and recommended follow-up |
|---|---|---|
| P1 | Larger snapshots can produce truncated synthesis | Original CSV and BM25 runs ended with `length` under the fixed output ceiling. Investigate shorter validated output contracts and bounded scope offline before any separately approved live comparison; do not silently repair responses. |
| P1 | Live reliability remains bounded and sample-specific | Original CSV/BM25 coverage was incomplete. Chunked corrected CSV now completes, but BM25 has not been retested under the extended profile. One CSV completion does not establish general reliability. |
| P2 | Live defect-detection accuracy is unmeasured | Create independent human-adjudicated expectations and suitable negative controls before future tests; retain workflow completion as a separate metric. |
| P2 | Dependency exclusions constrain conclusions | Record missing context explicitly; additional source needs separate scope, privacy and transmission approval. |

## Threats to validity and conclusion

The sample set is small, selected by the owner, and lacks independent defect
labels. Where inspected, supporting tests were read as text; none were executed.
The original CSV and BM25 runs did not inspect their test files; both later CSV
runs prepared all four files. The CSV iterations changed engine/profile/prompt
conditions and are not a controlled repeated-trial benchmark. No cross-model
comparison was performed. Historical fictional Java evaluations had
instruction/example overlap and are not independent accuracy evidence. Only one
specialist and one provider/model were evaluated here. The stored diagnostics
support truncation and coverage conclusions, not explanations of model reasoning.

Portfolio Risk and corrected-prompt CSV completed bounded workflows with empty
findings. Earlier CSV and BM25 partial outcomes remain important historical
limitations. The observed CSV improvement is compatible with clearer unit-local
instructions, but one execution cannot establish causality or general reliability.
No live source-code defect has been validated, and no independent accuracy or
production-readiness claim follows. At this report's 2026-11-08 verification,
the historical full offline baseline was 2,063 passing tests, covering application
behavior rather than real-model quality. This count records that verification,
not the current application baseline.
