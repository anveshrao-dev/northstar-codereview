"""Narrow, fail-closed Nebius Chat Completions integration.

ChatOpenAI preserves standard tool messages but drops provider-specific fields.
The response guard rejects extensions before they can be silently discarded.
A model requiring those fields needs a separately tested integration change.
"""

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal, get_args

import httpx
from langchain_core.messages import AIMessage, BaseMessage
from langchain_openai import ChatOpenAI
from langsmith import tracing_context
from openai import AsyncOpenAI, APIConnectionError, APIStatusError, APITimeoutError
from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator

from ..agents.code_quality import TransientModelError
from ..models import Contract


OFFICIAL_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"


@contextmanager
def _private_provider_logs():
    """Suppress dependency logs only in this call's async/thread context.

    httpcore can log headers before response hooks; the SDK can log request IDs.
    Response sanitization alone is too late. Filters leave levels/handlers and
    unrelated contexts untouched, and are removed even after cancellation.
    Explicit names cover the installed transport's lazily imported loggers.
    """
    active = ContextVar("private_nebius_logs", default=False)

    class ContextFilter(logging.Filter):
        def filter(self, record):
            return not active.get()

    prefixes = ("openai", "httpx", "httpcore", "langchain_openai", "langchain_core", "langsmith")
    names = set(logging.Logger.manager.loggerDict) | {
        "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpcore.socks",
        "httpx", "openai._base_client",
    }
    loggers = [logging.getLogger(name) for name in names
               if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)]
    guard = ContextFilter()
    token = active.set(True)
    try:
        for logger in loggers:
            logger.addFilter(guard)
        yield
    finally:
        active.reset(token)
        for logger in loggers:
            logger.removeFilter(guard)


class ProviderConfigurationError(ValueError):
    """Safe public configuration error; never includes the offending value."""


SafeProtocolField = Literal[
    "assistant.reasoning_content", "assistant.reasoning", "assistant.reasoning_details",
    "assistant.annotations", "assistant.audio", "assistant.function_call", "assistant.name", "assistant.id",
    "assistant.unknown_field", "tool_call.index", "tool_call.unknown_field", "function.unknown_field",
]

ReasoningFieldType = Literal[
    "absent", "null", "string_empty", "string_nonempty", "array", "object", "boolean", "number",
]


def _reasoning_type(message: Any) -> ReasoningFieldType | None:
    """Classify only the exact reasoning key in a decoded JSON message object.

    No content, size, nested keys, or fingerprints leave this function. Invalid
    message containers remain unclassified; this helper does not validate them.
    """
    if not isinstance(message, dict):
        return None
    if "reasoning" not in message:
        return "absent"
    value = message["reasoning"]
    if value is None:
        return "null"
    if isinstance(value, str):
        return "string_empty" if value == "" else "string_nonempty"
    # Exact JSON types keep booleans distinct from Python's integer subclassing.
    return {list: "array", dict: "object", bool: "boolean", int: "number", float: "number"}.get(type(value))


def _safe_protocol_fields(location: Literal["assistant", "tool_call", "function"],
                          unexpected: set[str]) -> tuple[SafeProtocolField, ...]:
    """Only emit fixed labels: even a provider-controlled field *name* may be secret.

    This allowlist is for diagnostics, not permission to accept or drop a field.
    Values, presence of nulls, array indices, and unknown names are never reported.
    """
    labels = {label.split(".", 1)[1]: label for label in get_args(SafeProtocolField)
              if label.startswith(location + ".")}
    return tuple(sorted({labels.get(name, labels["unknown_field"]) for name in unexpected}))


class SafeDiagnostic(Contract):
    """Closed vocabulary only: no provider message, error code, headers, or body."""
    category: Literal[
        "authentication_failure", "authorization_failure", "invalid_request", "model_unavailable",
        "rate_limit", "server_error", "connection_failure", "timeout", "local_budget_rejection",
        "malformed_tool_call", "unsupported_protocol_fields", "malformed_response",
        "protocol_replay_mismatch", "credential_guard_rejection", "response_size_limit", "model_refusal",
        "invalid_final_response", "missing_usage_metadata", "unknown_provider_error",
        "configuration_error", "tool_execution_failure", "unsupported_content_encoding",
    ]
    http_status: int | None = Field(default=None, strict=True, ge=100, le=599)
    unexpected_fields: tuple[SafeProtocolField, ...] = ()
    reasoning_type: ReasoningFieldType | None = None


class ProviderRequestError(RuntimeError):
    """Safe public error; the originating provider exception is not retained."""
    def __init__(self, diagnostic: SafeDiagnostic):
        self.diagnostic = diagnostic
        super().__init__(f"Nebius operation failed: {diagnostic.category}; details withheld.")


class ProviderTransientError(ProviderRequestError, TransientModelError):
    """Preserve the specialist's existing bounded transient-retry policy."""


class ProviderTimeoutError(ProviderRequestError, TimeoutError):
    """Preserve timeout handling while supplying a sanitized diagnostic."""


class _ProtocolViolation(ProviderRequestError):
    def __init__(self, category="malformed_response", http_status=None, *, unexpected_fields=(), reasoning_type=None):
        super().__init__(SafeDiagnostic(category=category, http_status=http_status,
                                       unexpected_fields=unexpected_fields, reasoning_type=reasoning_type))


MAX_RESPONSE_BYTES = 131072


class _BoundedResponseStream(httpx.AsyncByteStream):
    """Check raw chunks before HTTPX accumulates the identity-encoded body.

    The transport owns its individual chunk allocation. This wrapper never
    yields an overflowing chunk or reads the remainder after a limit failure.
    HTTPX closes the stream on success, hook failure, timeout and cancellation.
    """
    def __init__(self, stream: httpx.AsyncByteStream):
        self._stream = stream

    async def __aiter__(self):
        consumed = 0
        async for chunk in self._stream:
            consumed += len(chunk)
            if consumed > MAX_RESPONSE_BYTES:
                raise _ProtocolViolation("response_size_limit", 200)
            yield chunk

    async def aclose(self):
        await self._stream.aclose()


def _http_diagnostic(status: int) -> SafeDiagnostic:
    category = {400: "invalid_request", 401: "authentication_failure", 403: "authorization_failure",
                404: "model_unavailable", 422: "invalid_request", 429: "rate_limit"}.get(status)
    return SafeDiagnostic(category=category or ("server_error" if 500 <= status <= 599 else "unknown_provider_error"),
                          http_status=status)


def _diagnose(error: Exception) -> SafeDiagnostic:
    # HTTP clients may wrap a hook failure in APIConnectionError. Inspect types
    # in that private chain, never stringify/copy its sensitive contents.
    current = error
    seen = set()
    for _ in range(8):
        if id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, _ProtocolViolation):
            return current.diagnostic
        current = current.__cause__ or current.__context__
        if current is None:
            break
    if isinstance(error, (TimeoutError, APITimeoutError, httpx.TimeoutException)):
        return SafeDiagnostic(category="timeout")
    if isinstance(error, APIStatusError):
        return _http_diagnostic(error.status_code)
    if isinstance(error, (APIConnectionError, httpx.TransportError)):
        return SafeDiagnostic(category="connection_failure")
    return SafeDiagnostic(category="unknown_provider_error")


def _contains_credential(value: Any, credential: str, depth: int = 0) -> bool:
    """Inspect decoded keys/values, including JSON nested inside message strings.

    Tool arguments and final structured outputs are JSON strings within the
    outer response. Checking only one decoding layer misses escaped secrets.
    Stop on excessive nesting rather than bypassing this guard.
    """
    if depth > 32:
        raise _ProtocolViolation("credential_guard_rejection")
    if isinstance(value, str):
        if credential in value:
            return True
        if value.lstrip().startswith(("{", "[", '"')):
            try:
                decoded = json.loads(value)
            except ValueError:
                return False
            return _contains_credential(decoded, credential, depth + 1)
        return False
    if isinstance(value, dict):
        return any(_contains_credential(key, credential, depth + 1) or
                   _contains_credential(item, credential, depth + 1) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_credential(item, credential, depth + 1) for item in value)
    return False


class NebiusSettings(Contract):
    api_key: SecretStr = Field(repr=False, exclude=True)
    model: str = Field(min_length=1, max_length=256)
    base_url: str = OFFICIAL_BASE_URL

    @field_validator("api_key")
    @classmethod
    def check_key(cls, value):
        raw = value.get_secret_value()
        if not raw.strip() or raw != raw.strip() or not raw.isascii() or any(ord(c) < 33 for c in raw):
            raise ValueError("Invalid provider credential format")
        return value

    @field_validator("model")
    @classmethod
    def check_model(cls, value):
        if not value.strip() or value != value.strip() or any(ord(c) < 33 for c in value):
            raise ValueError("An explicit provider model identifier is required")
        return value

    @field_validator("base_url")
    @classmethod
    def check_endpoint(cls, value):
        if value not in {OFFICIAL_BASE_URL, OFFICIAL_BASE_URL.rstrip("/")}:
            raise ValueError("Only the verified official Nebius endpoint is supported")
        return OFFICIAL_BASE_URL

    @model_validator(mode="after")
    def check_separation(self):
        if self.api_key.get_secret_value() in self.model:
            raise ValueError("Model identifier must not contain credentials")
        return self


def load_nebius_settings(environ: Mapping[str, str] | None = None) -> NebiusSettings:
    """Called explicitly, never on import; no dotenv/global credential files."""
    source = os.environ if environ is None else environ
    try:
        return NebiusSettings(api_key=source.get("NEBIUS_API_KEY", ""), model=source.get("NEBIUS_MODEL", ""),
                              base_url=source.get("NEBIUS_BASE_URL", OFFICIAL_BASE_URL))
    except (ValueError, ValidationError):
        pass
    # Raise outside the handler so even __context__ cannot expose raw inputs.
    raise ProviderConfigurationError("Configure NEBIUS_API_KEY, NEBIUS_MODEL, and the official NEBIUS_BASE_URL; values withheld.")


def _canonical_assistant(message: dict) -> dict:
    calls = []
    for call in message.get("tool_calls") or []:
        function = call["function"]
        calls.append({"id": call["id"], "type": call["type"], "name": function["name"],
                      "args": json.loads(function["arguments"])})
    return {"content": message.get("content") or "", "calls": calls}


class NebiusModel:
    """One async conversation, explicitly authorized; no implicit paid fallback.

    transport is a trusted dependency-injection seam for offline MockTransport
    tests. Application callers use the default HTTPS transport. Never share one
    adapter across conversations; create/close an instance per specialist run.
    """

    sdk_max_retries = 0

    def __init__(self, settings: NebiusSettings, *, allow_live: bool = False,
                 timeout_seconds: float = 30, transport: httpx.AsyncBaseTransport | None = None):
        if allow_live is not True:
            raise ProviderConfigurationError("Live provider use requires explicit authorization.")
        if not 0 < timeout_seconds <= 30:
            raise ProviderConfigurationError("Provider timeout must be within 30 seconds.")
        self._settings = settings
        self._timeout = timeout_seconds
        self._expected_replays: list[dict] = []
        self.protocol_checks = 0
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(transport=transport, timeout=timeout_seconds, trust_env=False,
                                      follow_redirects=False, event_hooks={"request": [self._guard_request],
                                                                         "response": [self._guard_response]})
        self._sdk = AsyncOpenAI(api_key=settings.api_key.get_secret_value(), base_url=settings.base_url,
                                organization="", project="", max_retries=0, timeout=timeout_seconds,
                                http_client=self._http)
        # Supply the async SDK ourselves to exclude ambient OpenAI account settings.
        # A nonfunctional sync client prevents construction of a second HTTP client.
        self._chat = ChatOpenAI(model=settings.model, api_key=settings.api_key, base_url=settings.base_url,
                                client=object(), async_client=self._sdk.chat.completions, root_async_client=self._sdk,
                                http_async_client=self._http, openai_proxy="", organization="unused",
                                timeout=timeout_seconds, max_retries=0, temperature=None, n=1, streaming=False,
                                stream_usage=False, use_responses_api=False, callbacks=[], cache=False)
        self._bound = None
        self._active_synthesis_json: bool | None = None

    @property
    def transmission_identity(self) -> tuple[str, str]:
        """Credential-free identity for exact-model source authorization."""
        return ("nebius", self._settings.model)

    def bind_tools(self, tools):
        allowed = {"list_snapshot_files", "read_source_lines", "get_evidence", "get_source_metadata"}
        if any(tool.name not in allowed for tool in tools):
            raise ProviderConfigurationError("Only approved snapshot inspection tools may be bound.")
        self._bound = self._chat.bind_tools(tools) if tools else self._chat
        return self

    async def _guard_request(self, request: httpx.Request):
        if request.method != "POST" or str(request.url) != OFFICIAL_BASE_URL + "chat/completions":
            raise _ProtocolViolation("invalid_request")
        # Do not forward ambient organization/project identifiers from other SDKs.
        for name in ("OpenAI-Organization", "OpenAI-Project"):
            request.headers.pop(name, None)
        # HTTPX's default decoder can allocate an unbounded expanded chunk.
        # Negotiate identity and reject other encodings before body consumption.
        request.headers["Accept-Encoding"] = "identity"
        payload = json.loads(request.content)
        # Verify the actual SDK serialization before transport. JSON mode is
        # invocation-local and never a default on the inspection model.
        if self._active_synthesis_json is True:
            if (payload.get('response_format') != {'type': 'json_object'}
                    or 'tools' in payload or 'tool_choice' in payload):
                raise _ProtocolViolation('invalid_request')
        elif self._active_synthesis_json is not False or 'response_format' in payload:
            raise _ProtocolViolation('invalid_request')
        key = self._settings.api_key.get_secret_value()
        if key in request.content.decode("utf-8") or _contains_credential(payload, key):
            raise _ProtocolViolation("credential_guard_rejection")
        replayed = [_canonical_assistant(message) for message in payload["messages"]
                    if message["role"] == "assistant" and message.get("tool_calls")]
        if replayed != self._expected_replays:
            raise _ProtocolViolation("protocol_replay_mismatch")

    async def _guard_response(self, response: httpx.Response):
        if response.status_code != 200:
            # Classify before SDK error-body parsing/logging. Do not retain or
            # inspect provider bodies, error codes, or request identifiers.
            diagnostic = _http_diagnostic(response.status_code)
            raise _ProtocolViolation(diagnostic.category, diagnostic.http_status)
        if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
            raise _ProtocolViolation("unsupported_content_encoding", 200)
        length = response.headers.get("content-length", "")
        # Length is only an early rejection hint; actual chunks enforce the cap.
        if length.isascii() and length.isdecimal():
            significant = length.lstrip("0") or "0"
            if len(significant) > 6 or int(significant) > MAX_RESPONSE_BYTES:
                raise _ProtocolViolation("response_size_limit", 200)
        response.stream = _BoundedResponseStream(response.stream)
        # aread now consumes only guarded identity bytes and caches the bounded
        # content for the existing SDK. Pre-buffered trusted test transports are
        # checked below too; live HTTPX responses arrive unconsumed at this hook.
        raw = await response.aread()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise _ProtocolViolation("response_size_limit", 200)
        if self._settings.api_key.get_secret_value().encode() in raw:
            raise _ProtocolViolation("credential_guard_rejection", 200)
        try:
            payload = json.loads(raw)
            if _contains_credential(payload, self._settings.api_key.get_secret_value()):
                raise _ProtocolViolation("credential_guard_rejection", 200)
            choices = payload["choices"]
            if len(choices) != 1:
                raise _ProtocolViolation("malformed_response", 200)
            message = choices[0]["message"]
            unexpected = set(message) - {"role", "content", "tool_calls", "refusal"}
            # The installed SDK declares these three fields Optional with None
            # defaults. An empty annotations list contains no citation state.
            # Accept only those exact stateless shapes, not arbitrary falsy values.
            for field in unexpected & {"annotations", "audio", "function_call"}:
                if message[field] is None or (field == "annotations" and message[field] == []):
                    unexpected.remove(field)
            # The observed Nebius reasoning extension may be explicit JSON null.
            # Accept only that exact value; other types and reasoning aliases
            # remain rejected before normalization can discard meaningful state.
            if isinstance(message, dict) and "reasoning" in unexpected and message["reasoning"] is None:
                unexpected.remove("reasoning")
            if unexpected:
                raise _ProtocolViolation("unsupported_protocol_fields", 200,
                                         unexpected_fields=_safe_protocol_fields("assistant", unexpected),
                                         reasoning_type=_reasoning_type(message))
            if message.get("refusal"):
                raise _ProtocolViolation("model_refusal", 200)
            if message.get("role") != "assistant" or not isinstance(message.get("content") or "", str):
                raise _ProtocolViolation("malformed_response", 200)
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict) or not {"id", "type", "function"} <= set(call) or call["type"] != "function":
                    raise _ProtocolViolation("malformed_tool_call", 200)
                if unexpected := set(call) - {"id", "type", "function"}:
                    raise _ProtocolViolation("unsupported_protocol_fields", 200,
                                             unexpected_fields=_safe_protocol_fields("tool_call", unexpected))
                function = call["function"]
                if not isinstance(function, dict) or not {"name", "arguments"} <= set(function):
                    raise _ProtocolViolation("malformed_tool_call", 200)
                if unexpected := set(function) - {"name", "arguments"}:
                    raise _ProtocolViolation("unsupported_protocol_fields", 200,
                                             unexpected_fields=_safe_protocol_fields("function", unexpected))
                try:
                    args = json.loads(function["arguments"])
                    if not isinstance(args, dict):
                        raise ValueError
                except (TypeError, ValueError):
                    raise _ProtocolViolation("malformed_tool_call", 200) from None
            canonical = _canonical_assistant(message)
            if canonical["calls"]:
                self._expected_replays.append(canonical)
            self.protocol_checks += 1
        except (KeyError, TypeError, ValueError):
            raise _ProtocolViolation("malformed_response", 200) from None

    async def ainvoke(self, messages: list[BaseMessage], *, max_tokens: int,
                      synthesis_json: bool = False) -> AIMessage:
        if self._bound is None:
            raise ProviderConfigurationError("Bind approved tools before invoking the model.")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 8192:
            raise ProviderConfigurationError("Output token cap is invalid.")
        if type(synthesis_json) is not bool or (synthesis_json and self._bound is not self._chat):
            raise ProviderConfigurationError("JSON synthesis requires an explicit flag and disabled tools.")
        diagnostic = SafeDiagnostic(category="unknown_provider_error")
        async with self._lock:
            with tracing_context(enabled=False), _private_provider_logs():
                try:
                    self._active_synthesis_json = synthesis_json
                    # Direct response_format selects SDK .parse in this installed
                    # ChatOpenAI, which raises on length before finish metadata
                    # reaches our validator. Supported extra_body merges this
                    # same top-level wire field through ordinary .create instead.
                    options = {'extra_body': {'response_format': {'type': 'json_object'}}} if synthesis_json else {}
                    return await asyncio.wait_for(self._bound.ainvoke(messages, max_tokens=max_tokens,
                                                                      config={"callbacks": []}, **options), timeout=self._timeout)
                except Exception as error:
                    diagnostic = _diagnose(error)
                finally:
                    self._active_synthesis_json = None
        # Raise after leaving the handler: no provider exception in __context__
        # or __cause__, and no body/request attached to the public exception.
        error_type = ProviderRequestError
        if diagnostic.category == "timeout":
            error_type = ProviderTimeoutError
        elif diagnostic.category in {"rate_limit", "server_error"}:
            error_type = ProviderTransientError
        raise error_type(diagnostic)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        with _private_provider_logs():
            await self._sdk.close()
