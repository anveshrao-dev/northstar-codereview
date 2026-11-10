"""Isolated JSON-object capability probe. Never imported by production agents.

One fixed HTTP request, with no SDK retry layer or configurable prompt/mode.
Transport injection is for trusted offline tests only; there is no CLI override.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path
from time import monotonic
from typing import Literal

import httpx
from langsmith import tracing_context
from pydantic import Field, ValidationError

from .local_source import PROJECT_ROOT
from .models import Contract
from .providers.nebius import OFFICIAL_BASE_URL, load_nebius_settings, _private_provider_logs

MODEL = 'Qwen/Qwen3-30B-A3B-Instruct-2507'
TIMEOUT = 20.0
OUTPUT_TOKENS = 128
INPUT_BYTES = 4096
RESPONSE_BYTES = 65536


class TinyResponse(Contract):
    status: Literal['ready']
    count: int = Field(strict=True, ge=2, le=2)


class ProbeResult(Contract):
    model: Literal['Qwen/Qwen3-30B-A3B-Instruct-2507'] = MODEL
    mode: Literal['json_object'] = 'json_object'
    status: Literal['disabled','failed','succeeded'] = 'disabled'
    category: Literal['configuration','input_limit','invalid_request','authentication','authorization',
        'model_unavailable','rate_limit','server_error','http_error','connection','timeout',
        'protocol_error','response_limit','unexpected_tool_call','refusal','invalid_json',
        'schema_mismatch','truncated','unknown_finish','unknown_error'] | None = None
    requests_attempted: int = Field(default=0, ge=0, le=1)
    http_status: int | None = Field(default=None, ge=100, le=599)
    json_valid: bool | None = None
    schema_valid: bool | None = None
    finish_reason: Literal['stop','length','tool_calls','content_filter','function_call','other'] | None = None
    input_tokens: int | None = Field(default=None, ge=0, le=1_000_000_000)
    output_tokens: int | None = Field(default=None, ge=0, le=1_000_000_000)
    unknown_usage_attempts: int = Field(default=0, ge=0, le=1)
    elapsed_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)


def _reject_constant(_):
    raise ValueError('Invalid JSON constant')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _decode(value):
    return json.loads(value, parse_constant=_reject_constant, object_pairs_hook=_unique_object)


def _interpret(body, result):
    payload = _decode(body)
    choices = payload['choices']
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError('Invalid choices')
    choice = choices[0]
    message = choice['message']
    if message.get('role') != 'assistant':
        raise ValueError('Invalid role')
    finish = choice.get('finish_reason')
    allowed = ('stop','length','tool_calls','content_filter','function_call')
    result['finish_reason'] = finish if type(finish) is str and finish in allowed else ('other' if finish is not None else None)
    usage = payload.get('usage')
    if isinstance(usage, dict):
        counts = [usage.get(k) for k in ('prompt_tokens','completion_tokens')]
        if all(type(n) is int and 0 <= n <= 1_000_000_000 for n in counts):
            result.update(input_tokens=counts[0], output_tokens=counts[1], unknown_usage_attempts=0)
    if message.get('tool_calls') or message.get('function_call'):
        result['category'] = 'unexpected_tool_call'
        return
    if message.get('refusal'):
        result['category'] = 'refusal'
        return
    content = message.get('content')
    if not isinstance(content, str):
        raise ValueError('Invalid content type')
    result.update(json_valid=False, schema_valid=False)
    try:
        decoded = _decode(content)
        result['json_valid'] = True
    except (ValueError, RecursionError):
        result['category'] = 'invalid_json'
    else:
        try:
            TinyResponse.model_validate(decoded)
            result['schema_valid'] = True
        except ValidationError:
            result['category'] = 'schema_mismatch'
    if finish == 'length':
        result['category'] = 'truncated'
    elif finish != 'stop':
        result['category'] = 'unknown_finish'
    elif result['schema_valid']:
        result.update(status='succeeded', category=None)


async def run_probe(*, allow_live=False, environ=None, transport=None) -> ProbeResult:
    if allow_live is not True:
        return ProbeResult()
    began = monotonic()
    result = dict(status='failed', category='configuration', requests_attempted=0,
                  unknown_usage_attempts=0)
    try:
        if (Path.cwd().resolve() != PROJECT_ROOT or sys.version_info[:2] != (3,12)
                or Path(sys.prefix).resolve() != (PROJECT_ROOT / '.venv').resolve()):
            return ProbeResult(**result)
        settings = load_nebius_settings(environ)  # Only after explicit execution opt-in.
        if settings.model != MODEL:
            return ProbeResult(**result)
        payload = dict(model=MODEL, response_format={'type':'json_object'}, max_tokens=OUTPUT_TOKENS,
            n=1, stream=False, messages=[{'role':'system','content':
                'Return only a JSON object with exactly status (string ready) and count (integer 2).'},
                {'role':'user','content':'Fictional Northstar parcel has two blue tokens and is ready. Return its JSON summary.'}])
        encoded = json.dumps(payload).encode('utf-8')
        if len(encoded) > INPUT_BYTES:
            return ProbeResult(**{**result, 'category':'input_limit'})
        # No LangChain/SDK callbacks, retries, redirects, ambient proxies or writes.
        with tracing_context(enabled=False), _private_provider_logs():
            async with asyncio.timeout(TIMEOUT):
                async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT,
                        trust_env=False, follow_redirects=False) as client:
                    result.update(requests_attempted=1, unknown_usage_attempts=1, category='unknown_error')
                    async with client.stream('POST', OFFICIAL_BASE_URL+'chat/completions', content=encoded,
                        headers={'Authorization':'Bearer '+settings.api_key.get_secret_value(),
                                 'Content-Type':'application/json', 'Accept-Encoding':'identity'}) as response:
                        result['http_status'] = response.status_code
                        if response.status_code != 200:
                            result['category'] = {400:'invalid_request',401:'authentication',403:'authorization',
                                404:'model_unavailable',422:'invalid_request',429:'rate_limit'}.get(
                                response.status_code, 'server_error' if 500 <= response.status_code <= 599 else 'http_error')
                        elif response.headers.get('content-encoding','identity').strip().lower() != 'identity':
                            result['category'] = 'protocol_error'  # Reject before any live decompression.
                        else:
                            chunks = bytearray()
                            async for chunk in response.aiter_bytes():
                                if len(chunks)+len(chunk) > RESPONSE_BYTES:
                                    result['category'] = 'response_limit'
                                    break
                                chunks.extend(chunk)
                            else:
                                result['category'] = 'protocol_error'
                                _interpret(chunks, result)
    except (httpx.TimeoutException, TimeoutError):
        result.update(status='failed', category='timeout')
    except httpx.RequestError:
        result.update(status='failed', category='connection')
    except Exception:
        result['status'] = 'failed'  # No exception, body, or chaining leaves this function.
    result['elapsed_seconds'] = monotonic()-began
    return ProbeResult(**result)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'Invalid probe options; no request sent.\n')


def main(argv=None):
    parser = _Parser(description='One isolated JSON-object probe; separate inference approval required.', allow_abbrev=False)
    parser.add_argument('--allow-live', action='store_true')
    args = parser.parse_args(argv)
    try:
        print(asyncio.run(run_probe(allow_live=args.allow_live)).model_dump_json(indent=2))
    except Exception:
        print('Probe failed; details withheld.')
        raise SystemExit(1) from None
