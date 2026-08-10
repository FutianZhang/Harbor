"""Resilient Candidate-output checks that preserve public comparison boundaries."""
from __future__ import annotations

import re
from typing import Iterable


_WINDOWS_ABSOLUTE = re.compile(r"(?i)\b[a-z]:[\\/]")
_WINDOWS_UNC = re.compile(r"\\\\[^\\\s]+\\[^\\\s]+")
_POSIX_ENVIRONMENT = re.compile(r"(?<![A-Za-z0-9])/(?:home|users|root|tmp|var|private|opt|mnt|srv|data|input|output|app|workspace|workspaces)/")


def _warning_identity(record: dict[str, object]) -> tuple[object, ...]:
    return (record.get("code"), record.get("file"), record.get("row_key"), record.get("field"))


def compare_validation_records(expected: Iterable[dict[str, object]],
                               actual: Iterable[dict[str, object]]) -> list[str]:
    """Strictly compare warning code/cardinality/location, never message text."""
    expected_ids = [_warning_identity(record) for record in expected]
    actual_ids = [_warning_identity(record) for record in actual]
    if expected_ids == actual_ids:
        return []
    return [f"validation identities/order differ: expected={expected_ids!r}, actual={actual_ids!r}"]


def validate_warning_messages(records: Iterable[dict[str, object]]) -> list[str]:
    findings: list[str] = []
    for index, record in enumerate(records):
        message = record.get("message")
        if not isinstance(message, str) or not message.strip():
            findings.append(f"warning[{index}] message must be non-empty")
            continue
        if not any(character.isalpha() for character in message):
            findings.append(f"warning[{index}] message must be human-readable")
        if _WINDOWS_ABSOLUTE.search(message) or _WINDOWS_UNC.search(message) or _POSIX_ENVIRONMENT.search(message):
            findings.append(f"warning[{index}] message contains an environment-specific absolute path")
    return findings
