"""Value-free projection of already observed inspection events and coverage."""
from .models import SpecialistDiagnostics


_CATEGORIES = {'list_snapshot_files': 'discovery', 'get_source_metadata': 'metadata',
               'read_source_lines': 'source_read', 'get_evidence': 'evidence'}


def inspection_diagnostics(snapshot, observed, empty_files, events, final_response, synthesis_context=None, preparation=None):
    # Count unique observed lines, including disjoint ranges, without exposing
    # paths or text. Empty metadata-confirmed files count only as fully inspected.
    observed_files = full_files = observed_lines = total_lines = 0
    for file in snapshot.manifest.files:
        identity = (file.side, file.file_path)
        text = snapshot.sources[identity]
        line_count = text.count('\n') + int(bool(text) and not text.endswith('\n'))
        total_lines += line_count
        intervals = sorted((ref.start_line, ref.end_line) for ref in observed
                           if (ref.side, ref.file_path) == identity)
        last = count = 0
        for first, end in intervals:
            count += max(0, end - max(last, first - 1))
            last = max(last, end)
        observed_lines += count
        observed_files += bool(intervals)
        full_files += (bool(intervals) or identity in empty_files) and count == line_count
    return SpecialistDiagnostics(
        tool_categories=tuple(_CATEGORIES.get(event.tool_name, 'unknown') for event in events),
        source_read_occurred=bool(observed_files), files_with_observed_source=observed_files,
        fully_inspected_files=full_files, total_files=len(snapshot.manifest.files),
        observed_lines=observed_lines, total_lines=total_lines, final_response=final_response,
        synthesis_context=synthesis_context, preparation=preparation)
