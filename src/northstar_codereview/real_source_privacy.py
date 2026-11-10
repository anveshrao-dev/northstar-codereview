"""Bounded deterministic content checks, never disclosure authorization."""

import re

from .local_source import IntakeLimits, _text
from .output_safety import sensitive_text, normalize_for_scan
from .real_source_contracts import FailureCategory as F, IntakeRejected


def privacy_check(text: str) -> None:
    """Conservative signals, not proof of confidentiality or redistribution rights."""
    # Scan normalization is never written back to captured source. Reject URL
    # indicators before the credential-userinfo expression can rescan suffixes
    # of long repeated URLs. Apply the same decoding policy as that scanner.
    normalized = normalize_for_scan(text)
    if re.search(r"(?i)(?<![a-z0-9+.-])[a-z][a-z0-9+.-]*://|\bwww\.", normalized):
        raise IntakeRejected(F.URL)
    if re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", text):
        raise IntakeRejected(F.PRIVATE_KEY)
    if sensitive_text(text):
        raise IntakeRejected(F.CREDENTIAL)
    if re.search(r"(?<![\w.+-])[\w.+-]{1,64}@[\w.-]{1,253}\.[A-Za-z]{2,63}|\b\d{3}-\d{2}-\d{4}\b", text):
        raise IntakeRejected(F.PERSONAL)
    if re.search(r"/(?:Users|home)/[^\s/]+|[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]", text):
        raise IntakeRejected(F.WORKSTATION)


def validate_contents(sources) -> None:
    limits = IntakeLimits()
    if len(sources) > limits.max_files:
        raise IntakeRejected(F.SIZE)
    total = 0
    for text in sources.values():
        if type(text) is not str:
            raise IntakeRejected(F.INTEGRITY)
        # Check characters before allocation, then exact encoded byte limits.
        if len(text) > limits.max_file_bytes:
            raise IntakeRejected(F.SIZE)
        raw = text.encode("utf-8")
        total += len(raw)
        if len(raw) > limits.max_file_bytes or total > limits.max_total_bytes:
            raise IntakeRejected(F.SIZE)
        _text(raw)
        privacy_check(text)
