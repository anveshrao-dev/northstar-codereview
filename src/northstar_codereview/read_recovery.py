"""One deterministic end-clamp over an existing trusted source-read failure.

Pure eligibility check: no I/O, execution, model call, retry, or budget mutation.
The agent executes any correction through the same bounded tool as a separate
physical attempt in the same model-selected transaction and tool round.
"""

from hashlib import sha256

from pydantic import ValidationError

from .evidence_tools import LinesInput, MAX_LINES
from .local_source import CollectedSnapshot
from .models import ToolResult


def corrected_read_arguments(snapshot: CollectedSnapshot, arguments: dict | None, failure: ToolResult) -> dict | None:
    diagnostic = failure.diagnostic
    if (failure.tool_name != "read_source_lines" or failure.status != "error" or failure.recovery is not None
            or failure.snapshot_id != snapshot.manifest.snapshot_id or diagnostic is None
            or diagnostic.category != "invalid_line_range" or diagnostic.recovery != "use_captured_line_bounds"):
        return None
    try:
        args = LinesInput.model_validate(arguments)
    except ValidationError:
        return None
    entry = next((file for file in snapshot.manifest.files if file.file_path == args.file_path and file.side == args.side), None)
    text = snapshot.sources.get((args.side, args.file_path))
    if entry is None or text is None or sha256(text.encode("utf-8")).hexdigest() != entry.sha256:
        return None
    count = text.count("\n") + int(bool(text) and not text.endswith("\n"))
    if (diagnostic.line_count != count or not args.start_line <= count < args.end_line
            or args.end_line - args.start_line + 1 > MAX_LINES):
        return None
    return {**args.model_dump(mode="json"), "end_line": count}
