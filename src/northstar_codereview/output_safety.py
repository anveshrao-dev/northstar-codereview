"""Local deterministic detection of obvious credential-shaped output.

Defense in depth, not a secret scanner or publication authorization. Does not
read credentials, source files or the environment, and never returns matches.
Arbitrary/encoded secrets, proprietary information and semantic prompt injection
cannot be ruled out by patterns. False positives are deliberately visible.
"""

import html
import re
import unicodedata
from urllib.parse import unquote


_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\bsk-(?:proj-|svcacct-)?[a-z0-9_-]{16,}",
    r"\b(?:gh[pousr]_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,})",
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    r"\bxox[baprs]-[a-z0-9-]{10,}",
    r"\beyJ[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}",
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
    r"\b(?:Bearer|Basic)\s+[a-z0-9+/_.=~-]{8,}",
    r"\bauthorization\s*:\s*(?:Bearer|Basic)\s+[^\s\"'<>]+",
    r"\b(?:[a-z0-9_.-]{1,128}[_-])?(?:api[_-]?key|access[_-]?key|private[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|client[_-]?secret|password|passwd|secret|token|accountkey|connection[_-]?string|sig|x-amz-signature)\b[\"']?\s*[:=]\s*[\"']?[^\s\"'<>]+",
    # Start only at a scheme-token boundary: retrying an unbounded scheme at
    # every character is quadratic on long source tokens without a URL.
    r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s]+@",
))


def normalize_for_scan(value: str) -> str:
    # A fixed number of decoding passes catches obvious escaped spellings without
    # an unbounded decoder or revealing what matched. Rendering uses originals.
    text = value
    for _ in range(2):
        text = unquote(html.unescape(text))
    text = unicodedata.normalize("NFKC", text)
    return "".join(char for char in text if unicodedata.category(char) != "Cf")


def sensitive_text(value: str) -> bool:
    text = normalize_for_scan(value)
    return any(pattern.search(text) for pattern in _PATTERNS)


def contains_sensitive(value) -> bool:
    """Scan only the report's bounded data projection, including mapping keys."""
    if isinstance(value, str):
        return sensitive_text(value)
    if isinstance(value, dict):
        return any(contains_sensitive(key) or contains_sensitive(item) for key, item in value.items())
    if isinstance(value, (tuple, list)):
        return any(contains_sensitive(item) for item in value)
    return False
