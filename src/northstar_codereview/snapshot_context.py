"""Bounded snapshot metadata for task context; never source evidence."""

import json
from hashlib import sha256
from uuid import UUID

from .local_source import CollectedSnapshot
from .models import SourceSnapshot
from .output_safety import contains_sensitive


MAX_METADATA_BYTES = 8192


def _encode(value: dict) -> str:
    # Escape HTML delimiters too; filenames remain JSON data, never markup.
    return (json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
            .replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026'))


def prepare_snapshot_context(snapshot: CollectedSnapshot, *, expected_snapshot_id: UUID) -> str:
    """Validate the captured bundle and return at most 8 KiB of JSON metadata.

    No disk access, source text, labels, or observations enter the output. Missing
    entries remain discoverable through existing tools; omission never means
    inspected coverage. Validate even omitted entries before publishing metadata.
    """
    invalid = False
    try:
        manifest = SourceSnapshot.model_validate(snapshot.manifest.model_dump())
        if manifest.snapshot_id != expected_snapshot_id:
            raise ValueError
        if set(snapshot.sources) != {(f.side, f.file_path) for f in manifest.files}:
            raise ValueError
        entries = []
        for entry in sorted(manifest.files, key=lambda f: (f.file_path, f.side.value)):
            text = snapshot.sources[entry.side, entry.file_path]
            if sha256(text.encode('utf-8')).hexdigest() != entry.sha256:
                raise ValueError
            if not contains_sensitive(entry.file_path):
                entries.append(dict(file_path=entry.file_path, side=entry.side.value, sha256=entry.sha256,
                                    line_count=text.count('\n') + int(bool(text) and not text.endswith('\n'))))
    except (ValueError, TypeError, AttributeError, KeyError, UnicodeError):
        invalid = True
    if invalid:
        # Raise outside the handler: no raw validation exception in the chain.
        raise ValueError('Snapshot context rejected: snapshot_mismatch')
    payload = dict(schema_version=1, snapshot_id=str(manifest.snapshot_id), revision=manifest.revision,
                   total_files=len(manifest.files), omitted_files=len(manifest.files), truncated=True, files=[])
    for entry in entries:
        candidate = {**payload, 'files': [*payload['files'], entry],
                     'omitted_files': payload['omitted_files'] - 1}
        candidate['truncated'] = candidate['omitted_files'] != 0
        if len(_encode(candidate).encode('utf-8')) > MAX_METADATA_BYTES:
            break
        payload = candidate
    return _encode(payload)
