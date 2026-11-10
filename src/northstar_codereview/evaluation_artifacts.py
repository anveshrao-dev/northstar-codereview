"""New, project-local evaluation artifacts; no overwrite or symlink following."""

import json
import os
from contextlib import ExitStack

from .evaluation import EvaluationError, fingerprint
from .output_safety import contains_sensitive


def write_runtime_artifacts(root, payload, summary):
    """Callers must first project/validate their typed output. Not a secret scanner.

    Each file is flushed independently; a failed pair write may leave only JSON.
    Root is application-owned, never a CLI source/output path.
    """
    contents = (json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + '\n', summary)
    if contains_sensitive(payload) or contains_sensitive(summary) or any(len(s.encode()) > 4 * 1024 * 1024 for s in contents):
        raise EvaluationError('unsafe_output')
    stem = fingerprint(payload)
    names = (f'{stem}.json', f'{stem}.md')
    try:
        with ExitStack() as stack:
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, fd)
            for directory in ('.runtime', 'evaluation'):
                try:
                    os.mkdir(directory, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                stack.callback(os.close, fd)
            for name, content in zip(names, contents):
                target = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                with os.fdopen(target, 'w', encoding='utf-8') as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
    except OSError:
        raise EvaluationError('artifact_write_failed') from None
    return tuple(root / '.runtime' / 'evaluation' / name for name in names)
