# Live review visibility

The current Streamlit path calls `asyncio.run(service.run_review(...))` inside an
aggregate status container. It displays that the explicitly authorized attempt is
running, then the returned terminal result or sanitized failure. It receives no
intermediate stage callback or event feed. This does not establish which internal
stage is running, and a finished attempt may be partial or blocked.

The service performs these internal boundaries: private-execution checks, retained
snapshot selection, specialist/profile-bound transmission validation, provider
configuration, durable run and single-use transmission claims, capacity admission,
provider construction, the Code Quality LangGraph, report/evidence processing,
provider cleanup, and atomic result persistence. Those are code boundaries, not
currently streamed UI events. Claims and terminal records are durable facts; their
existence is not a complete chronological event history. Error and cancellation
paths can skip later work or preserve a partial report.

Saved Pipeline views reconstruct recorded facts from validated retained results
and a separate current checkpoint lookup. A manual walkthrough does not run those
stages again, establish stage timings or convert missing telemetry into success.

## Minimal future plan — not implemented

1. Define a small versioned, closed-vocabulary event contract bound to the exact
   run/import/snapshot, specialist and profile. Store only stage, transition,
   monotonic sequence and sanitized counters/categories; never source, model text,
   prompts, tool arguments, credentials or approval payloads.
2. Emit events at actual service boundaries after successful durable operations.
   Distinguish started, completed, blocked, failed and cancelled. The authorization
   gate and atomic single-use claim must remain before provider startup/source
   disclosure. Do not infer unobserved transitions, retries or cleanup success.
3. Persist events privately using an atomic append/transaction contract. Specify
   crash recovery, ordering, partial terminal results, retention, bounded storage
   and fail-closed behavior for event-write failures. Terminal review records remain
   the authoritative result; never reconstruct findings from progress events.
4. Separately approve a delivery mechanism that lets the UI consume committed
   events while execution proceeds. The present blocking call must not be replaced
   by simulated progress. A UI refresh must never restart work or reuse authority.
5. Before enabling it, test ordering and monotonicity, duplicate deliveries,
   process interruption, concurrent views, single-use authorization, cancellation,
   deadlines, redaction and result/event disagreement with fake providers only.

The current implementation has no stage-event schema, event persistence or
polling service. Any future implementation requires separate review and approval
and must preserve execution budgets, authorization and zero-retry behavior.
