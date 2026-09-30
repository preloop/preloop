"""Shared runtime memory quantity parsing."""

import re
from decimal import Decimal


def docker_memory_bytes(quantity: str) -> int:
    """Accept Docker suffixes and Kubernetes binary quantities in bytes."""
    match = re.fullmatch(
        r"([0-9]+(?:\.[0-9]+)?)([kmgt]i?|b)?", quantity.strip(), re.IGNORECASE
    )
    if not match:
        raise ValueError("Invalid agent memory limit")
    number, suffix = match.groups()
    suffix = (suffix or "").lower()
    exponent = "kmgt".index(suffix[0]) + 1 if suffix and suffix != "b" else 0
    return int(Decimal(number) * 1024**exponent)
