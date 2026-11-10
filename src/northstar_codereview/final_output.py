"""Final-envelope contract and value-free diagnostics; no repair or inference."""
import json
from typing import Any, Literal

from langchain_core.messages import AIMessage
from pydantic import Field, ValidationError, model_validator

from .models import Contract, FindingProposal, FinalResponseDiagnostics


FinalOutputDiagnostic = Literal['empty_response', 'invalid_json', 'top_level_type_mismatch',
    'missing_required_fields', 'invalid_field_types', 'unexpected_tool_call',
    'truncated_response', 'unknown_validation_failure']

FinalFormatDiagnostic = Literal['empty_response', 'markdown_fenced_json',
    'non_json_leading_text', 'invalid_json_syntax', 'schema_mismatch',
    'truncated_response', 'unknown_format_failure']


class FinalEnvelope(Contract):
    # Proposal validation remains separate so one invalid proposal cannot erase
    # valid siblings. This is the unchanged parser contract.
    findings: list[dict[str, Any]] = Field(max_length=16)
    outcome: Literal['findings', 'insufficient_evidence'] = 'findings'

    @model_validator(mode='after')
    def consistent_outcome(self):
        if self.outcome == 'insufficient_evidence' and self.findings:
            raise ValueError('Insufficient evidence requires an empty findings array')
        return self


class _PromptEnvelope(FinalEnvelope):
    # Pydantic supplies root-level $defs for the same proposal contract applied
    # by each specialist after envelope parsing.
    findings: list[FindingProposal] = Field(max_length=16)


def final_response_schema() -> dict:
    return _PromptEnvelope.model_json_schema()


def final_response_diagnostics(message: AIMessage) -> FinalResponseDiagnostics:
    """Project bounded response shape without persisting untrusted key names.

    No values, unknown names, nested structure, lengths or excerpts escape this
    function. Parsing here never repairs or changes the authoritative validator.
    """
    raw_finish = message.response_metadata.get('finish_reason')
    known_finish = ('stop', 'length', 'tool_calls', 'content_filter', 'function_call')
    finish = (raw_finish if isinstance(raw_finish, str) and raw_finish in known_finish
              else 'unavailable' if raw_finish is None else 'unrecognized')
    keys = present = unexpected = None
    if isinstance(message.content, str) and len(message.content.encode('utf-8')) <= 131072:
        try:
            value = json.loads(message.content)
        except (ValueError, RecursionError):
            pass
        else:
            if isinstance(value, dict):
                # Never echo arbitrary model-supplied keys (they may be secrets).
                keys = tuple(key for key in ('findings', 'outcome') if key in value)
                present = 'findings' in value
                unexpected = any(key not in ('findings', 'outcome') for key in value)
    _, error = parse_final_message(message)
    return FinalResponseDiagnostics(finish_reason=finish,
        truncated=(finish == 'length') if finish in known_finish else None,
        known_top_level_keys=keys, findings_present=present, unexpected_keys_present=unexpected,
        failure_stage='final_envelope' if error else 'none')


def final_format_diagnostic(message: AIMessage) -> FinalFormatDiagnostic | None:
    """Describe rejected format only; never transform or accept its contents.

    Fence/leading-text labels describe lexical shape, not the validity of any
    embedded JSON. Existing detailed envelope categories remain authoritative.
    """
    _, diagnostic = parse_final_message(message)
    if diagnostic is None or diagnostic == 'unexpected_tool_call':
        return None
    if diagnostic in ('empty_response', 'truncated_response'):
        return diagnostic
    if not isinstance(message.content, str):
        return 'unknown_format_failure'
    if diagnostic == 'invalid_json':
        text = message.content.strip()
        first_line = text.split('\n', 1)[0].rstrip('\r').lower()
        if first_line in ('```', '```json') and text.endswith('```'):
            return 'markdown_fenced_json'
        if text and text[0] not in '{["-0123456789' and not text.startswith(('true', 'false', 'null')):
            return 'non_json_leading_text'
        return 'invalid_json_syntax'
    # Only successfully decoded JSON may receive the schema-mismatch label.
    try:
        json.loads(message.content)
    except (ValueError, RecursionError):
        return 'unknown_format_failure'
    return 'schema_mismatch'


def parse_final_message(message: AIMessage) -> tuple[FinalEnvelope | None, FinalOutputDiagnostic | None]:
    if message.tool_calls or message.invalid_tool_calls:
        return None, 'unexpected_tool_call'
    # Chat Completions' explicit length finish reason survives installed
    # ChatOpenAI normalization. Do not infer truncation from text or usage.
    if message.response_metadata.get('finish_reason') == 'length':
        return None, 'truncated_response'
    content = message.content
    if not isinstance(content, str):
        return None, 'invalid_field_types'
    if not content.strip():
        return None, 'empty_response'
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        return None, 'invalid_json'
    except (ValueError, RecursionError):
        return None, 'unknown_validation_failure'
    if not isinstance(value, dict):
        return None, 'top_level_type_mismatch'
    try:
        return FinalEnvelope.model_validate_json(content), None
    except ValidationError as error:
        # Never format/log the exception, inputs, locations or context values.
        codes = {item['type'] for item in error.errors(include_url=False, include_input=False, include_context=False)}
        if 'json_invalid' in codes:
            return None, 'invalid_json'
        if 'missing' in codes:
            return None, 'missing_required_fields'
        if codes & {'list_type', 'dict_type', 'string_type', 'literal_error'}:
            return None, 'invalid_field_types'
        return None, 'unknown_validation_failure'
    except (ValueError, RecursionError):
        return None, 'unknown_validation_failure'
