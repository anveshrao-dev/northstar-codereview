# Local application delivery

## Delivery design

Keep the existing Streamlit application, guarded real-review service and five
specialist graphs. No replacement backend or deployment framework is needed.
The delivery additions are a fixed localhost launcher, an offline preflight
command, and end-to-end acceptance coverage through human decision and export.
A container or service manager would introduce dependencies and operational scope
without improving this single-user checkout workflow; neither is added.

The launcher uses this checkout's Python, fixes loopback binding and browser
telemetry/tracing settings, confines temporary storage to the project, and runs in
the foreground. Check mode performs no inference, approval creation or source
execution. Optional provider checks read only the supplied environment; optional
Portfolio Risk checks use the existing retained-snapshot loader and report safe
identities/counts/expiry only. Readiness is not authorization or evidence of remote
provider availability. Tests use generated imports, fake providers and actual
Streamlit/LangGraph/SQLite/report paths without launching a listening server.

## Local package and commands

Use a complete checkout, the existing `.venv`, Python **3.12.15**, and the
installed dependencies pinned by `uv.lock`. This is a checkout-based package, not
a standalone wheel, container, cloud deployment or multi-user service. No install
or dependency resolution occurs during startup or checks.

From the checkout root, run offline local checks:

```sh
PYTHONDONTWRITEBYTECODE=1 LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false \
  TMPDIR="$PWD/.cache/tmp" .venv/bin/python -B examples/local_app.py check
```

To check provider configuration and the newest usable Portfolio Risk import,
explicitly load the existing ignored `.env` with the existing uv installation:

```sh
env -u NEBIUS_API_KEY -u NEBIUS_MODEL -u NEBIUS_BASE_URL \
  PYTHONDONTWRITEBYTECODE=1 LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false \
  LANGCHAIN_TRACING=false TMPDIR="$PWD/.cache/tmp" \
  uv run --offline --no-sync --no-config --no-python-downloads \
    --cache-dir "$PWD/.cache/uv" --env-file "$PWD/.env" \
    .venv/bin/python -B examples/local_app.py check --check-provider --portfolio-risk
```

Check mode emits only sanitized JSON. It validates the local interpreter,
checkout files, required installed distributions, local storage, disabled debug
and tracing, and—when selected—provider settings and retained-snapshot integrity,
privacy, retention and private report directory. It neither creates nor renews
approval, opens a socket, tests remote credentials, nor runs a review. It reports
`inference_authorized: false` even when local checks pass. A missing provider
configuration does not prevent scripted use; the optional provider check fails
with exit 2. Missing/expired/corrupt snapshot or unsafe report storage also fails
the optional snapshot check. Basic checks do not prove future disk space or write
success. Dependency presence is checked; the lock and full regression suite
remain the reproducibility and compatibility gates.

After operator review, start the local UI (no inference starts automatically):

```sh
.venv/bin/python -B examples/local_app.py start
```

For later explicitly authorized real reviews, use the same offline uv environment
loading command above, replacing `check --check-provider --portfolio-risk` with
`start`. The launcher does not load `.env` itself. Never put keys in command-line
arguments, URLs, logs, shared screenshots or report artifacts.

The launcher pins **127.0.0.1:8501**, disables telemetry, browser auto-launch,
static serving, raw error details, external tracing and Streamlit secrets-file
loading, and retains CORS/XSRF protections. It creates only missing project-local
`.runtime` and `.cache/tmp` directories; unsafe existing permissions or symlinks
are rejected instead of changed. The child inherits credentials only in its
process environment. TMPDIR and XDG cache storage are project-local; no HOME,
PATH or system settings are changed.

It deliberately uses the installed Streamlit bootstrap rather than its normal
CLI activation path, which can auto-resolve a malformed global credentials file.
Fixed config loads before runtime startup. The pinned bootstrap API is covered
by offline characterization and must be revalidated on Streamlit upgrades.
Streamlit may read/watch existing global config; critical network/privacy flags
are overridden. No global configuration or credentials are written by this
launcher. Arbitrary host, port and source-path switches are not exposed.

## Health, shutdown and failure states

Once an operator has separately started the app, Streamlit's local
`http://127.0.0.1:8501/_stcore/health` endpoint reports runtime liveness. It does not
establish provider access, valid transmission approval, complete review coverage
or correct findings. Offline acceptance tests do not contact this endpoint.
The offline `check` command remains the safe configuration/readiness check.

The process stays in the foreground. Use **Ctrl-C** or SIGTERM for normal
shutdown; there is no background daemon, restart policy or automatic review
retry. Avoid stopping an active review. Provider cleanup runs in the review
service's `finally` block with a bounded close timeout. Abrupt process termination
can prevent cleanup or final artifact creation; the durable claim remains
consumed. Remote processing/billing may continue after cancellation. Inspect
retained results and require fresh explicit consent before any new attempt.

| State | Meaning and operator action |
|---|---|
| Local preflight blocked | Check interpreter, checkout and directory ownership/permissions; no automatic chmod/install. |
| Port occupied / server failed | Stop the other local instance or resolve the local conflict; no alternate public bind. |
| Configuration blocked | Supply the exact approved model, official endpoint and API key through the environment; no remote probe occurs. |
| Snapshot blocked | Verify retained identity, privacy and expiry through the existing intake workflow; checks never renew it. |
| Approval consumed/expired | This run cannot reuse it; only a new explicit authorization can permit another review. |
| Partial / malformed / truncated | Retain limitations and rejected proposals; do not repair, retry or call the result clean. |
| Cleanup/storage failure | Treat execution as incomplete; check private retained state before any new action. |

This is an unauthenticated **single-user workstation** app. Do not expose it with
a public listener, reverse proxy or tunnel. Keep `.runtime` private and out of
publication. Real reports remain in their import's `review_artifacts` directory;
manual decisions use `.runtime/approvals/reviews.sqlite3`. Export is a local
sanitized JSON download, never publishing. Retention gates viewing/export;
cleanup, disk capacity and backups remain operator responsibilities.

## Acceptance and readiness boundaries

The offline acceptance tests use generated imports and scripted providers with
the actual LangGraph source preparation, compact proposal resolution, exact
citation checks, durable claim, private report persistence, Streamlit controls,
SQLite human decision and export. Valid and malformed results are tested.
Approval is never automatic; tests explicitly reject the report and retain
unverified claim status. Rerendering does not repeat inference.

The local real workflow runs **Code Quality only** with an explicitly selected
profile bound to approval and a single-use claim. `standard` remains the default;
extended CSV/BM25 limits require explicit consent. All retain four model attempts,
4,000 output reservations, **1,000 output tokens/request**, a protected 30,000-unit
input hold, a 20-second request timeout, and no retries or repairs. See the
[profile guide](real-source-intake.md#local-real-review-workflow) for exact ceilings.
JSON-object mode is not a schema/factuality guarantee. Exact citations establish
source correspondence, not defect correctness.

Portfolio Risk completed with empty findings. The original CSV and BM25 runs
were partial with truncation; the first extended CSV run abstained in all units.
Corrected-prompt extended CSV completed with 541/541 prepared and validated analyzed
lines and three empty findings outcomes. BM25 has not been tested live with the
extended profile and corrected prompts. See the [evaluation report](evaluation-report.md).
No live source defect has been validated; finding-level citation validation was
not exercised by the completed zero-finding runs.
Offline acceptance, local configuration checks and these limited live results do
not establish production readiness, remote availability, independent accuracy or
a successful deployed listening-server session.
