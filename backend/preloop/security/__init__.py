"""Security helpers for RSA result validation.

Scanner execution (gitleaks, zizmor) happens inside the agent execution
sandbox, installed by the preset the same way as spdx-tools. The server
keeps only deterministic result validation: the gap-register freeze
comparator is result validation, not a scanner.

``git_guard`` has no production callers yet. It is kept deliberately as the
enforcement half of the planned follow-up that wires ``validate_gap_register``
into result ingestion; see its module docstring for details.
"""

from preloop.security.gap_register import (
    GapRegisterValidationError,
    assert_gap_register,
    validate_gap_register,
)
from preloop.security.waivers import (
    FailingItem,
    WaiverValidationError,
    apply_waivers,
    assert_waived_gate,
    validate_waived_gate,
    validate_waiver_entries,
)

__all__ = [
    "GapRegisterValidationError",
    "assert_gap_register",
    "validate_gap_register",
    "FailingItem",
    "WaiverValidationError",
    "apply_waivers",
    "assert_waived_gate",
    "validate_waived_gate",
    "validate_waiver_entries",
]
