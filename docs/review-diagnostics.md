# Interpreting bounded review failures

A successful inspection tool attempt is not necessarily a source read. The four
tools have different effects:

| Tool | Successful observation | Coverage effect |
| --- | --- | --- |
| `list_snapshot_files` | Captured paths and hashes | No inspected source lines |
| `get_source_metadata` | Captured byte and line counts | No inspected source lines; a verified empty file can be accounted for |
| `read_source_lines` | Exact source slice and evidence reference | Observed path and inclusive line interval |
| `get_evidence` | Exact source citation, including its text | Observed path and inclusive line interval |

`reviewed_files` records paths for which source was observed, plus verified empty
files. It does not mean every line in each listed file was inspected. Completion
separately requires continuous coverage of all source lines. Reading one line can
add a path while the review remains partial. Generating no findings does not erase
coverage already acquired. Listing files or obtaining metadata for a nonempty file
never counts as source inspection.

For the current implementation, successful attempts combined with an empty
`reviewed_files` list and no withheld reporting fields imply that no successful
source-read or evidence observation was recorded. They do not identify the exact
sequence of listing and metadata calls in historical reports. New reports also
retain closed tool categories in execution order, not raw inspection events.
Internal events contain source and must not be logged to fill a telemetry gap.

## Final output gates

The final envelope is a JSON object with one required field, `findings`, an array
of at most 16 proposal objects. Optional `outcome` is `findings` (default) or
`insufficient_evidence`; the latter requires an empty array. Unknown envelope
fields are rejected. Supported empty forms are:

```json
{"findings": []}
```

```json
{"findings": [], "outcome": "insufficient_evidence"}
```

The initial prompt includes the Pydantic-generated proposal schema. Final
synthesis repeats these envelope forms and disables tools. JSON-object mode
constrains syntax, not this application schema. The parser validates the envelope
first; proposal validation, specialist-category checks, observed-range checks and
exact-source validation follow separately. A schema-valid proposal is still an
unverified defect claim.

With the current parser, `schema_mismatch` together with
`missing_required_fields` establishes that JSON parsed as an object but lacked
top-level `findings`. It does not identify unexpected keys or nesting, nor mean
that required evidence fields inside a proposal were missing: those are evaluated
later. Several different objects can produce this same diagnostic. No renaming,
unnesting, default insertion, fence stripping or automatic repair is performed.

Explicit provider `finish_reason=length` takes precedence and produces
`truncated_response`. Its absence from a report is not proof that the provider
returned `stop`; historical reports do not retain the actual finish reason. Token
counts alone do not prove truncation or its absence.

## Value-free compliance telemetry

New specialist results and reports include optional runtime `diagnostics`. This
does not add fields to the model's final envelope or change any validation rule.
Historical records without diagnostics remain readable with the same content
digest; the unavailable field is omitted when serialized, not backfilled.

- `tool_categories`: execution-event order, using only `discovery`, `metadata`,
  `source_read`, `evidence`, or `unknown`. Failed physical attempts are included;
  a category alone does not establish success. Calls rejected before an execution
  event remain accounted for by existing attempt counters and limitations.
- `source_read_occurred`: whether source text was actually observed through
  `read_source_lines` or `get_evidence`, not whether a read was merely requested.
- Coverage counts: files with observed source, fully inspected files, total files,
  unique observed lines, and total lines. File counts distinguish snapshot sides;
  overlapping ranges are counted once. Metadata-confirmed empty files can be
  fully inspected without counting as source reads. Coverage means text was made
  available, not that the model understood it or evaluated all behavior.
- `final_response`: the last parsed final response, or the protected synthesis
  response once that phase begins. It is null when that phase has no usable
  response; earlier parse diagnostics cannot stand in for failed synthesis.
  `finish_reason` is allowlisted (`stop`, `length`, `tool_calls`, `content_filter`,
  `function_call`); missing metadata is `unavailable`, other values `unrecognized`.
  `truncated` is true only for `length`, false for other recognized finish reasons,
  and null for unavailable or unrecognized metadata.
- `known_top_level_keys` records only the contract names `findings` and `outcome`.
  `unexpected_keys_present` flags other names without storing them. Arbitrary key
  names can contain secrets and must not be persisted. `findings_present` records
  required-field presence, not type correctness. These shape fields are null when
  a JSON object is unavailable; nested keys and all values are withheld.
- `failure_stage` within `final_response` is `final_envelope` or `none`. It concerns
  final parsing only. Request/budget/inspection/proposal failures and incomplete
  coverage remain separately described by synthesis status, inspection counters
  and existing sanitized limitations. A valid envelope does not clear those gates.

The Code Quality instructions now distinguish discovery from reading source,
explicitly require `findings` even when empty, and explain missing-dependency
uncertainty. These clarify the existing contract; they do not establish that a
prompt defect caused a previous model failure. No automatic field insertion,
renaming, repair calls, relaxed evidence rules, or additional requests are added.

## Synthesis input admission and duplicate source text

Input reservations charge UTF-8 bytes of the serialized message objects and tool
definitions plus 1,024 framing units. They are deliberately conservative estimates,
not provider token counts or billing. Every request is charged for its complete
history; lower reported usage does not refund a reservation. The protected input
hold is also a strict per-synthesis allowance, even if the global total has spare
capacity. Neither rule is relaxed by context reduction.

Source tools originally carry the same source text twice: `read_source_lines`
returns `data.text` and `evidence[].snippet`, while `get_evidence` returns the
reference in both `data` and `evidence`. Repeated citations can duplicate source
again across observations. A synthesis request retains the inspection history,
the original trusted skill and generated schema, and a short final instruction.
The full system instructions are not appended anew for every inspection turn.

Before synthesis, the runtime measures that history. Only when it exceeds the
protected allowance or the specialist's remaining input reservation does it
attempt deterministic reduction. For successful, recognized source observations,
it removes `data.text` or `data.snippet` **only** when the complete data payload
exactly matches a full evidence reference already observed by this specialist.
Non-null data and all location fields remain. Generic tool-result validation
continues to pass; the original typed tool artifacts and events are unchanged.

Every message and complete evidence reference is retained, including repeated
citations, exact source text, line endings, line ranges, snapshot identity, and
source side. All assistant calls, observation IDs, error and recovery information,
metadata, trusted instructions, schema, and missing-dependency guidance remain.
Unknown or nonredundant payloads are not reduced. There are no summaries, source
truncations, removed turns, new requests, or provider changes. The reduced request
still goes through the existing local checks and atomic shared budget ledger.
If it still cannot fit, synthesis remains incomplete and no request is sent.

Optional `diagnostics.synthesis_context` records only numeric sizes, counts,
booleans and closed admission labels:

- `before` and `after`: total projected units, message count, system/human/
  assistant/tool/other message contributions, tool-schema contribution, and
  framing contribution. The contributions sum to the total. No prompt text,
  source, model arguments, key names, or content hashes are recorded.
- `duplicate_source_payloads`: recognized redundant data payloads; and
  `repeated_evidence_references`: repeat occurrences of full, identical references
  across observations. Repeated references are counted but not removed.
- `protected_allowance`, `compaction_attempted`, and `admission`: `admitted` only
  after budget admission; `denied` for budget rejection; `not_attempted` for a
  failure before admission, such as binding, authorization, or timeout. Existing
  synthesis status and limitations explain the failure separately.

In a generated two-file, 138-line example, three observations projected 37,164
units before reduction and 25,350 afterward, within a 30,000-unit hold. All eight
messages and three full evidence references remained; total admitted reservations
were 69,284 input and 3,000 output units across three model calls. These are
offline measurements, not a reconstruction of a historical provider conversation
or reported provider usage. Historical artifacts without these diagnostics cannot
reveal their exact projected size. Their hashes and records are not rewritten.

## Failure interpretation and offline verification

Four model attempts, three successful discovery/metadata calls, no observed files,
and a missing-field synthesis diagnostic represent two independent failures:
source inspection did not occur, and the final envelope did not comply with the
required contract. A successful tool-free synthesis opportunity does not guarantee
either source coverage or usable findings. Partial status with zero findings is
the conservative result, not a clean review or a successful negative control.

`tests/test_real_review_failure.py` exercises this pattern through the real guarded
service, LangGraph, tools, single-use scoped claims and result persistence. All
source is generated inside the test directory. Scripted examples with alternate
keys or nesting demonstrate diagnostic ambiguity; they are not reconstructions of
any retained live response. Positive cases use actual source observations, valid
empty and nonempty envelopes, and unchanged evidence validation. In-memory HTTP
tests verify schema delivery, tool-free JSON synthesis, protocol replay and
finish-reason preservation without external requests.

When these contracts agree and the failure is reproduced without an application
defect, preserve the rejection. Instruction clarification is not proof of a fix
to model compliance. Do not increase budgets, credit discovery as inspection, or
manufacture findings. Offline success
with compliant scripted responses does not establish future live-model compliance
or defect-detection accuracy. Existing real-run claims remain consumed; diagnosis
does not authorize another execution.
