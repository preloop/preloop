"""Security helpers used by Preloop MCP tools and RSA result validation.

Scanners are thin wrappers around maintained OSS CLIs (gitleaks, zizmor)
registered on the existing ``preloop-mcp`` server with
``default_enabled: False``. Gap-register freeze is result validation, not
a scanner.
"""

from preloop.security.gap_register import (
    GapRegisterValidationError,
    assert_gap_register,
    validate_gap_register,
)
from preloop.security.pins import (
    RECOMMENDED_GITLEAKS_VERSION,
    RECOMMENDED_ZIZMOR_VERSION,
)

__all__ = [
    "GapRegisterValidationError",
    "RECOMMENDED_GITLEAKS_VERSION",
    "RECOMMENDED_ZIZMOR_VERSION",
    "assert_gap_register",
    "validate_gap_register",
]
