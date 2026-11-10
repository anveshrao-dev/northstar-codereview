# Bounded real-source intake

This is a guarded local import and real-code review workflow, not evidence of
successful live defect detection. Automated verification uses generated synthetic
files inside the checkout. A guarded import CLI, review CLI and Streamlit workflow
separate local capture, retention, transmission consent and live execution.
There is no arbitrary upload flow or automatic live execution.

## Four independent authorities

1. Synthetic fixture approval retains the existing `approved_synthetic=True`
   trusted-caller assertion. It cannot authorize a snapshot marked real.
2. `ImportApproval` permits a fixed sample to be captured locally. It includes an
   exact hash manifest, snapshot identity, privacy-review assertion, purpose,
   approval UUID, approval state, approval time, import expiry and retention date.
3. `TransmissionApproval` separately binds that import receipt and manifest
   digest, exact snapshot including every file hash, provider, model, purpose and
   expiry. Import approval and a clean scan do not grant transmission.
4. `PublicationApproval` is a separate contract with a rights assertion. No
   importer, tool, provider, or publisher consumes it. Existing SQLite finding
   review decisions also grant neither transmission nor publication authority.

Approved, pending, denied and revoked are explicit states. Only approved,
current, matching records pass the relevant boundary. Unknown fields/states,
naive times, mismatched identities and paths fail closed. Receipts are local
operator assertions, not signatures or authenticated multi-user permissions.
No API generates approval merely from a scan or filesystem location.

## Fixed samples

The public importer accepts a `SampleId` and an import receipt; it accepts no
source-root or file-path argument. Roots are fixed application configuration:
the sibling `Week1-project/stock-portfolio-analyzer` and `northstar-atlas`
projects. This layout must be reviewed before an actual import is authorized.

| Sample | Exact allowlist relative to its source project |
| --- | --- |
| `portfolio_risk` | `src/stock_portfolio_analyzer/risk.py`, `tests/test_risk.py` |
| `csv_import` | `src/stock_portfolio_analyzer/csv_import.py`, `src/stock_portfolio_analyzer/models.py`, `src/stock_portfolio_analyzer/validation.py`, `tests/test_imports.py` |
| `bm25_retrieval` | `src/northstar_atlas/vector_store/bm25.py`, `src/northstar_atlas/vector_store/catalog.py`, `src/northstar_atlas/vector_store/contracts.py`, `src/northstar_atlas/vector_store/records.py`, `tests/test_bm25.py` |

No supporting module, dataset, corpus, index or additional test is discovered or
included. Portfolio supporting contracts are outside this allowlist. Extending
scope requires a reviewed code change and new exact-hash approvals.

## Capture and privacy

`snapshot_from_hashes` builds a proposed identity without reading source. A future
human-reviewed manifest must supply the hashes; the CLI provides no automatic
discovery or approval process. `import_sample` validates approval
before opening a configured root, then walks path components through directory
descriptors with no-follow checks. Only single-link regular `.py` files are
accepted. Directory/file identity and mutation stamps are rechecked, followed by
exact approved SHA-256 comparison. The original project-local collector remains
confined to the checkout.

Existing limits remain 50 files, 1 MiB per file and 10 MiB total, with existing
depth/path bounds. The fixed samples currently contain only two, four or five
files. UTF-8 decoding is strict; binary signatures and control bytes are rejected.
Source is never imported, executed, compiled, tested or normalized. UTF-8 bytes
and CRLF/LF endings survive the JSON storage round trip unchanged.

Before source persistence, deterministic checks reject credential-like patterns,
private-key markers, email/SSN-like personal-data indicators, common absolute
workstation paths and external URL indicators. Diagnostics contain only a closed
category, never matching values, excerpts or host paths. These checks can both
miss sensitive information and reject harmless code examples. They do not clear
employer information, intellectual-property rights, confidentiality or safe
external disclosure. No redaction or automatic scan exception is implemented.

## Private local storage

All bundles are beneath ignored `.runtime/real_source/`:

```text
claims/<import-approval-uuid>
approvals/<import-approval-uuid>.json          # human-prepared import metadata
imports/<import-approval-uuid>/
  manifests/intake.json
  snapshots/source.json
  receipts/import.json
  receipts/transmission-<approval-uuid>.json  # only if separately recorded
  review_artifacts/                         # written only by an authorized review
```

Directories are owner-only `0700`; files are `0600`. Existing insecure permissions,
symlinks, special files and hard-linked data files are rejected rather than fixed
silently. Import uses an exclusive claim, a private staging directory, fsync and
an atomic directory rename. A UUID-based loader cannot open pending bundles.
Ordinary failure removes partial staging content and releases its claim;
simultaneous duplicate imports cannot replace an existing committed bundle.
Loading revalidates receipts, manifest identity, all source hashes, size limits and
privacy checks. Duplicate JSON keys in source, manifests and authority receipts
are rejected.

Transmission receipts are separately recorded without invoking any provider.
`record_transmission` validates the loaded snapshot and writes with an atomic
no-replace commit. Callers must explicitly provide the current receipt to tools
and agents; a stored receipt is not automatically selected. Publication inventory
must exclude the entire runtime tree; release manifests require a separate audit.

Retention is explicit: loading and model-facing authorization stop at
`retain_until`. Import expiry limits capture, not subsequent use of an already
captured snapshot within its retention period. Transmission has its own expiry,
bounded by retention. Expiry does not erase files, backups or in-memory objects.
Deletion is a separate manual local operation; there is no secure-erasure promise
or purge daemon. Abrupt process termination can leave ignored pending directories
or exclusive claims; these are never treated as committed snapshots and require
operator inspection before cleanup. There is no durable revocation registry:
revoked replacement records must be supplied by the trusted caller; an existing
approved in-memory receipt remains usable until expiry.

## Guarded import CLI

Run from the checkout root with the existing project-local Python environment.
No credentials or `.env` loading are needed. The only operations are `preflight`
and `import`; neither starts a model, agent graph, test runner, human-review
checkpoint, external service or publisher. There are no source-root, file-path,
model, transmission or budget override arguments. Unknown arguments and duplicate
sample/approval selectors are rejected without echoing their values.

The operator must first obtain a separately reviewed `ImportApproval` record.
The record contains `approval_id`, the exact `IntakeManifest` (sample, project,
provenance, purpose, retention and complete snapshot/hash allowlist), an approved
state, aware approval/expiry times and `privacy_reviewed: true`. The snapshot
identity must match `snapshot_from_hashes` for those exact approved file hashes.
Defaults and validation are defined by `real_source_contracts.py`; use
`ImportApproval.model_dump_json()` to serialize a reviewed typed record.

Place that record at the **fixed** inbox path
`.runtime/real_source/approvals/<approval-uuid>.json`. Both private directories
must be owner-only `0700`; the single-link regular file must be owner-only `0600`
and owned by the current user. Prepare it through a trusted local workflow and
publish the complete record atomically, without replacing an existing approval
UUID. Do not put source content or credentials in it. This CLI does not mint,
approve, refresh, rewrite or repair approval records, or collect original-source
hashes. Hash preparation and privacy review of original projects require separate
authorization; they have not been performed by this CLI's verification.

Metadata-only scope inspection, which returns exit code 2 when no approval is
supplied:

```sh
env PYTHONDONTWRITEBYTECODE=1 \
  LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false TMPDIR="$PWD/.cache/tmp" \
  .venv/bin/python -B examples/import_real_source.py preflight \
    --sample portfolio_risk
```

To validate an existing reviewed record, replace the placeholder below with its
UUID (not a pathname):

```sh
env PYTHONDONTWRITEBYTECODE=1 \
  LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false TMPDIR="$PWD/.cache/tmp" \
  .venv/bin/python -B examples/import_real_source.py preflight \
    --sample portfolio_risk --approval-id '<approval-uuid>'
```

Preflight reports the allowlisted relative filenames, expected count and whether
current matching authorization metadata is available. It checks the bounded
32 KiB approval document, private filesystem protections, duplicate JSON keys,
UUID binding, approval interval, retention, exact manifest and existing
claim/import conflicts. **It does not open original source, verify current source
hashes, run a privacy scan, copy files or create directories.** `ready` means only
that these metadata checks passed at that moment. It is not a lock or a guarantee
that import will succeed; expiry, changed source and concurrent import can still
cause rejection.

Future import command, only after separate authorization to read and copy the
specified original files:

```sh
env PYTHONDONTWRITEBYTECODE=1 \
  LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false TMPDIR="$PWD/.cache/tmp" \
  .venv/bin/python -B examples/import_real_source.py import \
    --sample portfolio_risk --approval-id '<approval-uuid>' --allow-import
```

Other allowed sample IDs are `csv_import` and `bm25_retrieval`. The opt-in flag is
necessary but insufficient: the approved record must also be present, current
and exact. The existing importer independently rechecks authorization, opens only
allowlisted files, verifies approved hashes, scans privacy indicators and atomically
commits the private bundle. Other project files are ignored, never discovered or
included. An approval attempting to add files is rejected.

Successful import emits only sanitized JSON with `status: committed`, approval
UUID, snapshot UUID, manifest digest and fixed scope metadata. The existing
`receipts/import.json` remains the durable import receipt. No transmission receipt
or human-review/publication permission is created. The output explicitly reports
all three as unauthorized. Source text, raw exceptions, absolute workstation
paths and untrusted argument values are never report fields.

Exit code 0 means metadata-ready or committed, depending on operation. Exit code
2 means rejected. Help exits 0 and performs no intake. Failure categories are:

| Categories | Meaning |
| --- | --- |
| `invalid_arguments` | Unknown/missing/duplicate CLI selectors, malformed UUID or unsupported option |
| `authorization` | Missing opt-in or missing, invalid, stale or mismatched authority metadata |
| `sample_scope` | Approval belongs to another fixed sample |
| `conflicting_import` | Existing claim or committed import; no overwrite or automatic retry |
| `source_integrity`, `unsafe_file`, `binary_content`, `size_limit` | Existing capture/integrity/size checks rejected the source |
| `credential_indicator`, `private_key_indicator`, `personal_data_indicator`, `workstation_path_indicator`, `external_url_indicator` | Privacy signal; no automatic redaction or transmission |
| `storage_failure` | Filesystem rejection/failure, including a missing required source file |
| `internal_error` | Unexpected failure with details withheld; never retried automatically |

Some invalid metadata also receives the existing storage/integrity/size categories.
On any failure, inspect the private local state before retrying manually: a failure
after atomic promotion can leave a complete committed bundle. Stale crash claims
are never automatically deleted by this CLI. Existing privacy, retention,
trusted-operator and redistribution limitations remain unchanged. None of these
commands authorizes transmission to Nebius or clears publication rights.

## Model and graph boundary

`CollectedSnapshot.real_source` carries immutable provenance. The five existing
specialist constructors and outer orchestrator accept an optional
`transmission_approval`. For marked real snapshots, synthetic opt-in is rejected;
exact content identity and authorization are checked at construction, before each
model call, and before each tool observation. The model must expose its actual
credential-free `transmission_identity`; the Nebius adapter exposes its existing
configured identity without changing request construction. Currently only
`nebius` / `Qwen/Qwen3-30B-A3B-Instruct-2507` is permitted by the receipt contract.

Real-source specialists require zero provider retries and output repairs. Default
real-source limits only reduce those two allowances to zero; no ceilings increase.
The four tools, LangGraph nodes, routing, parallel isolation, budgets, JSON mode,
conversation IDs, Pydantic contracts and exact-source validators remain in place.
Only provenance wording is adjusted in trusted instructions for the real path.
Source and repository Markdown never become trusted policy.

Real-source tools suppress tracing around the complete LangChain call, including
callback setup. External callbacks, tool verbosity and global LangChain
debug/verbose modes are rejected before source-bearing execution. No global
configuration is changed. Constructed real snapshots also undergo the bounded
content/privacy checks, not just the loader path.

Expiry detected during execution stops the run as partial without another model
request. Expiry detected at run entry raises a sanitized authorization rejection
before the graph starts. Tool failures and any requests already made retain their existing accounting. Evidence
validation remains separate from semantic correctness and publication rights.

## Threat model and limits

The boundary protects against model-supplied paths, accidental scope expansion,
stale receipts, ordinary filesystem mutation, link escapes, partial imports and
mistaken reuse of synthetic approval. It does not protect against malicious code
running as the same OS user, an administrator, or a caller deliberately constructing
an unmarked snapshot from private text. Provenance is carried by the trusted
intake path, not inferred from bytes. Hashes prove content identity, not authorship.
Descriptor checks reduce races but cannot create a transactional source filesystem.
Changing a model object or monkeypatching authorization code is outside this
trusted-process boundary. Use only reviewed model factories with zero internal
provider retries; the existing Nebius adapter meets that condition.

Pattern checks are defense in depth. Raw internal tool events contain source and
must not be logged or published. Existing sanitized report/export and human-review
boundaries remain necessary. Intake alone does not save review reports, enable
tracing, read credentials, grant external access or approve a live run. Authorized
review execution separately persists its result through the review service.
POSIX descriptor semantics and checkout-based deployment remain requirements.

## Offline verification

`tests/test_real_source.py` uses only generated project-local files, including
synthetic privacy indicators. Tests cover exact allowlists; path, link, binary,
size and mutation rejection; failed/duplicate/concurrent imports; private atomic
storage; retention; separate approvals; model identity; expiry between requests;
tool-call IDs; exact CRLF evidence; all five real graphs with fake models; and
unchanged deterministic routing. The global pytest fixture rejects socket and DNS
attempts. The full existing suite covers the unchanged synthetic workflows.

`tests/test_real_source_cli.py` additionally exercises the entry point, sanitized
argument handling, metadata-only preflight, explicit import opt-in, private approval
inbox, expired/mismatched/mutated/duplicate records, source rejection, atomic failure
cleanup and the absence of implicit transmission. All imports in these tests use
temporary generated trees inside the checkout, never the original projects.

Before any original-source import, a human must separately approve the exact
files/hashes, privacy review and retention. A later provider transmission needs
its own scoped approval. Offline tests do not import original projects or perform
live inference, and do not prove live defect-detection accuracy.

## Local real-review workflow

`examples/review_real_source.py` and the Streamlit real-code workspace share the
same service. `standard` is always the default; a sample name, source size or
failed review never selects an extended profile. Profiles describe evaluated
workload sizes, not new source permissions. All select Code Quality only in this
workflow; approved sample/file scope remains independently enforced.

| Execution profile | Model attempts | Tool attempts | Input units | Output reservations | Overall deadline |
|---|---:|---:|---:|---:|---:|
| `standard` | 4 | 6 | 80,000 | 4,000 | 60s |
| `extended_csv` | 4 | 9 | 80,000 | 4,000 | 70s |
| `extended_bm25` | 4 | 11 | 90,000 | 4,000 | 90s |

Each preserves 1,000 output tokens/request, 20-second request timeout, zero
retries/repairs, a 30,000-unit protected final hold and 12,000-unit source contexts.
No arbitrary budget/source/model override exists. Complete-plan admission runs
before provider construction. Infeasible plans stay blocked; failed executions
never escalate or retry. Missing dependencies remain explicit limitations.

For extended use, explicitly pass the **same** `--execution-profile extended_csv`
or `--execution-profile extended_bm25` to both `authorize` and `run`. Omitting it
selects `standard`, even when the approval names an extended profile, and that
mismatch is rejected. Changing selection requires fresh explicit approval.

New approvals and durable claims bind the profile name and every registry limit,
in addition to snapshot/import/manifest/provider/model/purpose/specialist/expiry
bindings. Before claim creation, a named approval must also match its stored
receipt; substituting another valid profile in a caller's copy is rejected.
Legacy approvals without profile bindings cannot authorize extended execution.
Their historical serialization remains unchanged; no approval or claim is migrated.

Streamlit shows the profile and resource ceilings before disclosure consent.
Changing the selection clears consent and approval selection; execution still
requires a separate confirmation. Run artifacts and CLI output include sanitized
profile details, including blocked/partial outcomes. These profiles are offline
verified settings, not guarantees of live completion, accuracy or monetary cost.

From the project root, metadata listing needs no credentials:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B examples/review_real_source.py \
  catalog --sample portfolio_risk
```

Select the exact import UUID and manifest digest from the metadata. Placeholders
below are not real approvals. Never put credentials in command arguments.

```sh
# Optional explicit local retention replacement; no inference or reimport.
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B examples/review_real_source.py \
  renew --sample portfolio_risk --import-id '<import-uuid>' \
  --manifest-digest '<digest>' --days 30 --allow-renewal

# Separate disclosure decision for the currently selected import/digest.
PYTHONDONTWRITEBYTECODE=1 LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false \
  .venv/bin/python -B examples/review_real_source.py authorize \
  --sample portfolio_risk --import-id '<import-uuid>' \
  --manifest-digest '<digest>' --allow-transmission
```

Renewal accepts 1–365 days, default 30. It verifies the archived source again,
including expired archives, without opening original projects. It atomically
creates a new bundle and import receipt plus `receipts/renewal.json` containing
lineage digests/IDs and explicit local authorization time. Old bundles and
timestamps are unchanged; exact source bytes, line endings and snapshot identity
remain the same. Use the **new import UUID and manifest digest** afterward. A
revoked, corrupt or unsafe archive is rejected. No transmission permission is
copied. Expired old snapshots continue to fail normal loading.

Transmission authorization lasts at most 30 minutes and never exceeds retention.
It is an unauthenticated local operator assertion, not cryptographic identity
proof. It authorizes neither publication nor execution by itself. The run action
requires its receipt ID and a separate explicit live opt-in:

```sh
env -u NEBIUS_API_KEY -u NEBIUS_MODEL -u NEBIUS_BASE_URL \
  PYTHONDONTWRITEBYTECODE=1 LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false \
  TMPDIR="$PWD/.cache/tmp" \
  uv run --offline --no-sync --no-config --no-python-downloads \
    --cache-dir "$PWD/.cache/uv" --env-file "$PWD/.env" \
    .venv/bin/python -B examples/review_real_source.py run \
    --sample portfolio_risk --import-id '<import-uuid>' \
    --transmission-id '<transmission-uuid>' --allow-live
```

This is a potentially billable external operation and needs human permission.
`--offline` prevents uv package access, not inference. The provider must be Nebius
with `Qwen/Qwen3-30B-A3B-Instruct-2507`; credentials are checked only at Start.
For the UI, use the same explicitly approved environment-loading prefix followed
by `.venv/bin/python -B examples/local_app.py start`. This existing guarded launcher
performs local preflight and starts Streamlit with fixed loopback and privacy
settings; see [local delivery](local-delivery.md). Neither the launcher nor the app
loads `.env` itself. Startup, initial pages, sample selection and widget reruns
never call a model; an authorized review still requires its separate start action.

Private results are under `imports/<import-uuid>/review_artifacts/<run-uuid>.json`.
The run UUID can be supplied with `--run-id` for deterministic duplicate rejection.
A durable `.claim` remains even on failure/cancellation/crash; do not remove it to
retry. The same ID cannot start a second model session. A crash after the claim
can leave no final result; usage is then unknown and operator inspection is needed.
Source, raw model messages, reasoning and tool arguments are not result fields.
Status, usage, unknown usage, exact transmission ID, manifest digest, fixed profile,
sanitized report and cleanup state are retained. Cost remains unknown; reservation
estimates are not billing measurements.

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B examples/review_real_source.py \
  show --sample portfolio_risk --import-id '<import-uuid>' --run-id '<run-uuid>'
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B examples/review_real_source.py \
  export --sample portfolio_risk --import-id '<import-uuid>' --run-id '<run-uuid>'
```

Export emits sanitized JSON to stdout; do not redirect it into public storage.
The Streamlit download includes an available SQLite report-level human decision;
CLI export does not fetch a decision automatically. Report claims remain unverified
unless independently adjudicated; neither report approval nor evidence validity
proves correctness. UI citations are loaded locally on explicit request and omitted
from exports. Unknown or unsupported proposals remain rejected, and zero supported
findings is allowed without claiming a clean or accurate review.

Normal result loading/export also requires current snapshot retention. Previously
downloaded files and SQLite checkpoints are not automatically deleted at expiry.
There is no purge, encryption, durable revocation registry, multi-user identity,
provider billing guarantee, or protection against malicious same-user rewriting.
Failure categories distinguish blocked authorization/configuration, duplicate run,
deadline/cancellation, execution, storage and cleanup failures; source/provider
exception text is never returned. No failure triggers an automatic retry.

### Prepared review execution

The guarded real-review service/CLI/UI now defaults to deterministic source
preparation through existing authorized tools. No approval, retention, specialist
scope or single-use claim is expanded. The default standard profile remains
unchanged. Extended profiles require explicit matching authorization; all retain
the 30,000-unit protected synthesis allowance.
Preparation caps, compact citation resolution, partial-coverage behavior and the
explicit programmatic legacy option are documented in [architecture](architecture.md).

Model claims remain unverified even with exact source evidence. Full source
coverage means captured lines were supplied, not that the model understood them
or that unavailable portfolio dependencies were checked. Numeric preparation,
coverage, synthesis-admission and rejected-proposal diagnostics are retained in
sanitized reports and exports. They never include raw source or model messages.
