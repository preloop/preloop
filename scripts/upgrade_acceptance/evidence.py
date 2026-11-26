"""Credential redaction shared by host and container acceptance probes."""

from __future__ import annotations

import re


def scrub(value: str, sensitive: list[str]) -> str:
    """Remove known test secrets and common credential forms from artifacts."""
    for item in sorted(sensitive, key=len, reverse=True):
        if item:
            value = value.replace(item, "[redacted]")
    value = re.sub(r"postgres(?:ql)?(?:\+\w+)?://[^\s'\"]+", "[database-url]", value)
    value = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[jwt]", value)
    return value
