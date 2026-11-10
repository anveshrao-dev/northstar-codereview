"""Lossless synthesis-only reduction of duplicate tool payloads; no I/O."""
import json

from langchain_core.messages import BaseMessage, ToolMessage

from .models import ContextSize, EvidenceReference, ToolResult


def _units(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode('utf-8'))


def input_reservation(messages: list[BaseMessage], tool_schemas: list[dict]) -> int:
    """Unchanged UTF-8 byte estimate plus framing, not provider tokens or cost.

    Complete history and tool definitions are charged on EVERY request. Artifacts
    were already excluded; only model-facing contents contribute here.
    """
    payload = {'messages': [m.model_dump(exclude={'artifact'}) for m in messages], 'tools': tool_schemas}
    return _units(payload) + 1024


def _result(message):
    if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
        return None
    try:
        if len(message.content.encode('utf-8')) > 16384:
            return None
        return ToolResult.model_validate_json(message.content)
    except (ValueError, RecursionError):
        return None


def _redundant(result) -> bool:
    if result is None or result.status != 'ok' or len(result.evidence) != 1:
        return False
    ref = result.evidence[0]
    if result.snapshot_id != ref.snapshot_id:
        return False
    if result.tool_name == 'read_source_lines':
        return result.data == dict(file_path=ref.file_path, side=ref.side.value,
            start_line=ref.start_line, end_line=ref.end_line, text=ref.snippet)
    if result.tool_name == 'get_evidence':
        return result.data == ref.model_dump(mode='json')
    return False


def measure_context(messages: list[BaseMessage], tool_schemas: list[dict]) -> ContextSize:
    contributions = dict(system_units=0, human_units=0, assistant_units=0, tool_units=0, other_units=0)
    categories = {'system':'system_units', 'human':'human_units', 'ai':'assistant_units', 'tool':'tool_units'}
    duplicate = repeated = 0
    seen = set()
    for message in messages:
        contributions[categories.get(message.type, 'other_units')] += _units(message.model_dump(exclude={'artifact'}))
        result = _result(message)
        duplicate += _redundant(result)
        if result is not None:
            for ref in result.evidence:
                # Internal equality key only: never emitted, hashed or persisted.
                identity = ref.model_dump_json()
                repeated += identity in seen
                seen.add(identity)
    total = input_reservation(messages, tool_schemas)
    schemas = _units(tool_schemas)
    return ContextSize(projected_units=total, message_count=len(messages), **contributions,
        tool_schema_units=schemas, framing_units=total - sum(contributions.values()) - schemas,
        duplicate_source_payloads=duplicate, repeated_evidence_references=repeated)


def compact_synthesis_messages(messages: list[BaseMessage], observed: list[EvidenceReference]) -> list[BaseMessage]:
    """Omit only source data text exactly repeated in observed evidence.

    Preserve every message, every full evidence reference (including duplicates),
    assistant call and observation ID, status, error and recovery record. Never
    summarize, truncate, drop context or reinterpret unrecognized payloads.
    The original history and internal tool artifacts remain unchanged.
    """
    reduced = []
    for message in messages:
        result = _result(message)
        if _redundant(result) and result.evidence[0] in observed:
            # Retain all other JSON fields exactly, including optional provenance.
            value = json.loads(message.content)
            # Keep non-null data and all its location fields. Successful
            # ToolResult envelopes still validate; only duplicated text goes.
            del value['data']['text' if result.tool_name == 'read_source_lines' else 'snippet']
            reduced.append(message.model_copy(update={'content':json.dumps(value, ensure_ascii=False, separators=(',', ':'))}))
        else:
            reduced.append(message)
    return reduced
