# Isolated structured-output compatibility probe

## Documented compatibility evidence

The following public sources were reviewed on 2026-11-03; they were not refreshed
as part of offline remediation:

- [Official public model catalog](https://tokenfactory.nebius.com/api/public/models_info)
  lists Qwen/Qwen3-30B-A3B-Instruct-2507 with the `JSON mode` tag, including its
  model flavor. This is model-specific capability evidence, not account access.
- [Nebius Structured output & JSON](https://docs.tokenfactory.nebius.com/ai-models-inference/json)
  directs users to that tag for supported models and describes JSON-object and
  JSON-schema request modes. Examples omit tools, but use a different Qwen model.
- [Chat Completions reference](https://docs.tokenfactory.nebius.com/api-reference/inference/create-chat-completion)
  documents the endpoint and request format. SDK parameter acceptance is not
  evidence of model-specific implementation.

A separately approved one-request probe accepted `json_object` on the selected
model: HTTP 200, valid JSON and local schema, finish_reason stop, 51 input and 13
output tokens. It does not separately certify `json_schema`, strict enforcement,
schema keywords, or tool-free behavior for this particular deployment. Those
remain unverified. No explicit model-specific unsupported schema features were
found; absence of such documentation is not evidence that all features work.

The isolated runner is not used by production agents and does not modify their
provider. It sends exactly one fixed Chat Completions POST when opted in, with
`response_format={"type":"json_object"}`, no tools, `n=1`, `stream=false`,
`max_tokens=128`, no redirects or retries, and a 20-second overall deadline.
Serialized fictional input is capped at 4,096 UTF-8 bytes (not a billing-token
measurement). The response read is capped at 65,536 bytes. Identity encoding is
requested and other content encodings are rejected before reading the live body,
so decompression cannot bypass the size check. There is no
output repair, SDK retry layer, source code, evaluation label, or external tracing.

The fictional task requests exactly `{"status":"ready","count":2}`. Syntax
validation rejects nonstandard constants and duplicate keys. Pydantic rejects
missing/extra fields, coercion, and incorrect values. Successful status requires
valid syntax, the tiny schema, and a normal stop finish reason. A length finish
is reported as truncation even when the returned prefix parses. Finish values
are allowlisted; unknown values become `other`. Token counts are nonnegative
bounded integers or unavailable; missing/invalid usage marks the attempt unknown.
Only sanitized status, categories, validity booleans, numeric usage/status/time,
fixed model/mode, and finish labels leave the runner. No raw response is persisted.
Reasoning is never printed or replayed. This one-turn probe does not establish
compatibility with the production provider's full replay contract.

Credentials are read from the supplied environment only after explicit opt-in;
the runner does not read `.env` itself. Preparation/tests use synthetic credentials.
To execute after separate human inference approval, use the existing environment:

```bash
# Run from the checkout root.
env -u NEBIUS_API_KEY -u NEBIUS_MODEL -u NEBIUS_BASE_URL \
  PYTHONDONTWRITEBYTECODE=1 \
  LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false \
  TMPDIR="$PWD/.cache/tmp" \
  uv run --offline --no-sync --no-config --no-python-downloads \
    --cache-dir "$PWD/.cache/uv" --env-file "$PWD/.env" \
    .venv/bin/python -B examples/check_structured_output.py --allow-live
```

No model, endpoint, prompt, mode, or budget CLI overrides are accepted. Without
`--allow-live`, it returns disabled before reading credentials or making a request.
The runner writes no artifacts. Local timeout cannot guarantee remote cancellation
or exact billing. One conforming sample cannot prove universal JSON enforcement,
correct defect detection, or that a larger production schema will work.

Offline regression tests use an in-memory HTTP transport and the suite-wide
socket denial fixture. Offline verification does not repeat the live probe or establish broad model accuracy.
