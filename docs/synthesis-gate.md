# Reserved synthesis and final-output contract

## Model-directed phase boundary

The inspection/synthesis loop below remains in use for fictional model-directed
reviews and the explicit programmatic legacy path. Current prepared real-source
Code Quality reviews instead use the chunk-analysis path described below; they
do not append a separate synthesis request after all chunks.

All five specialists share an inspection loop and a deterministic `synthesize`
node. The graph switches to synthesis before consuming the last individual model
slot or when inspection reaches its tool-round/attempt boundary. A one-call budget
permits synthesis only, with no fabricated coverage. An early natural final answer
uses the ordinary parsing path without another call.

Synthesis rebinds the same provider conversation to an empty tool set, retaining
all assistant calls and matching ToolMessages. Unexecuted calls blocked by local
limits receive explicit blocked replies before synthesis; these are not successful
observations or physical tool attempts. Any unexpected tool calls during synthesis
are rejected without execution. Synthesis has no retry or repair.

The request passes `synthesis_json=True` only after tools are disabled. The existing
adapter serializes `response_format={"type":"json_object"}` through the supported
SDK extra_body interface. Its request hook checks the actual wire format and
rejects JSON mode during inspection or tool definitions during synthesis. No global
model mutation or JSON-schema mode is used. Finish reason and usage survive SDK
normalization, including provider-confirmed length truncation.

## Shared reservation contract

Before parallel dispatch, the ledger reserves one request opportunity per selected
job. Opaque permits are owner-bound and never enter prompts, reports or checkpoints.
Inspection admission accounts for outstanding holds under the same lock as charging.
Three specialists under the Java nine-request profile leave six inspection admissions
and three protected synthesis opportunities. Inspection remains first-come; there
is no fairness scheduler or guaranteed source coverage.

Wrong owners and spent/released permits fail. Unspent holds can be released once;
admitted work remains charged after failure or cancellation. Token and deadline
checks still apply. The fictional full-evaluation profiles use request-slot-only
holds. The isolated GitOps profile additionally protects 30,000 input units within
its 80,000 ceiling. Real-source profiles also protect 30,000 input units within
their total: 80,000 for `standard` and `extended_csv`, 90,000 for `extended_bm25`.
The final analysis unit consumes that hold; earlier units cannot consume it.

The non-chunked synthesis path may remove verified duplicate source payloads from
serialized observations when needed, preserving messages, tool-call IDs and full
evidence references. This does not increase its allowance. If the resulting request
still cannot fit, admission is denied and the review remains incomplete; no retry
or budget escalation follows. See [context reduction](review-diagnostics.md#synthesis-input-admission-and-duplicate-source-text)
and the [GitOps limits](gitops-slice.md).

## Validated envelope

The trusted prompt uses the Pydantic-generated schema with root-level definitions.
The non-chunked envelope always requires `findings`, with at most 16 proposals.
Optional `outcome` is `findings` or `insufficient_evidence`; the latter requires
an empty findings array.
Missing fields are never inserted. Markdown fences and surrounding prose are not
stripped. JSON-object generation does not guarantee this schema or factual truth.

Proposals pass independent structure, specialist-domain, observed-range and exact
source checks. In the non-chunked validation path, one invalid sibling does not
erase valid findings. A valid envelope does not imply valid proposals, and valid
evidence does not prove a defect.

Synthesis outcomes are `not_started`, `succeeded`, `insufficient_evidence`,
`budget_denied`, `malformed_output`, `tool_call_rejected`, or `request_failed`.
`succeeded` means envelope parsing succeeded; incomplete coverage or other failures
still retain partial status. `not_started` also covers natural early final answers.

## Current real-source chunk analysis

The guarded real-review runner invokes Code Quality only. After authorization and
the single-use execution claim, it plans all approved ranges and checks complete
capacity before provider construction. Each unit reads its ranges through the
guarded tools and makes one tool-free JSON-object analysis request. Only that unit's
source, trusted instructions, compact schema and dependency limitations are sent;
prior responses are not replayed. There is no extra aggregation request, retry or
output repair. The same shared ledger enforces the selected profile's ceilings.

`UnitEnvelope` narrows the required `findings` list to at most two proposals, each
with at most two observed-range evidence pointers. Deterministic resolution obtains
exact snapshot quotations before canonical proposal, category and evidence checks.
A malformed envelope, any invalid proposal, or `insufficient_evidence` rejects the
whole unit: none of its findings are retained and its ranges receive no analyzed
coverage. A valid empty findings result can count as analyzed coverage without
establishing defect absence. Prepared coverage remains a separate metric.

Earlier accepted units' findings remain in the accumulated state when a later
unit is rejected. Finalization and report persistence are separate gates, not
guaranteed by unit acceptance. Independent later units may proceed within the plan;
transport or budget interruption stops further requests. The final unit must fit
the protected input allowance. See [engine design](review-engine-design.md) and
[architecture](architecture.md#findings-and-reports) for coverage and retention rules.

## Sanitized diagnostics

Envelope labels are `empty_response`, `invalid_json`, `top_level_type_mismatch`,
`missing_required_fields`, `invalid_field_types`, `unexpected_tool_call`,
`truncated_response`, and `unknown_validation_failure`.

Format labels are `empty_response`, `markdown_fenced_json`, `non_json_leading_text`,
`invalid_json_syntax`, `schema_mismatch`, `truncated_response`, and
`unknown_format_failure`. They describe rejected shape without exposing or repairing
content. Only explicit provider `finish_reason=length` establishes truncation.

Proposal rejection labels are `invalid_proposal_structure`,
`wrong_specialist_category`, `source_identity_mismatch`, `unsupported_evidence`,
`unobserved_evidence_range`, `exact_source_mismatch`, and
`unknown_validation_failure`. Only the first failed gate and trusted proposal
ordinal are reported. Raw values, arbitrary field names, source excerpts, validation
inputs, reasoning and credentials are excluded.

Report schema 4 and full live-envelope schema 3 retain synthesis outcomes; semantic
evaluation-result schema 2 remains separate. Incompatible older schemas are not
silently migrated. The recorded live status is summarized in [evaluation](evaluation.md).

## Verified scope

Offline tests exercise real graphs and SDK serialization over in-memory transports,
including call-ID replay, failure/cancellation accounting and strict evidence gates.
Controlled live runs have demonstrated tool calling and JSON-object synthesis.
The historical full fictional Java, Angular and Jenkins-on-AKS evaluations remained
partial with no retained evidence-validated live findings. Subsequent real-source
Portfolio Risk and corrected-prompt CSV reviews completed with empty findings;
these do not establish defect-detection accuracy. The [evaluation report](evaluation-report.md)
preserves the individual historical results and their limitations.
Model-specific JSON-schema support and schema-subset enforcement remain unverified.
