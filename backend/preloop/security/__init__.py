"""Security helpers for RSA result validation.

Scanner execution (gitleaks, zizmor) happens inside the agent execution
sandbox, installed by the preset the same way as spdx-tools. The server
keeps only deterministic result validation: the gap-register freeze
comparator is result validation, not a scanner.
"""

from preloop.security.gap_register import (
    GapRegisterValidationError,
    assert_gap_register,
    validate_gap_register,
)

__all__ = [
    "GapRegisterValidationError",
    "assert_gap_register",
    "validate_gap_register",
]
