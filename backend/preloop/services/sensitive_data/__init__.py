"""Shared sensitive-data detection for model I/O and tool-call policies.

``detectors`` returns typed match spans so callers can decide (model and
tool rules), redact (replace spans) or fingerprint content. Everything here
is deterministic, in-process and dependency free; an external recogniser
plugs in through :func:`detectors.register_detector`.
"""

from preloop.services.sensitive_data.detectors import (
    BUILTIN_TYPE_IDS,
    BUILTIN_TYPES,
    DetectorConfig,
    DetectorTimeoutError,
    Match,
    SensitiveTypeInfo,
    UnsafePatternError,
    compile_safe_regex,
    detect,
    list_types,
    register_detector,
    registered_type_ids,
    reset_detectors,
)

__all__ = [
    "BUILTIN_TYPE_IDS",
    "BUILTIN_TYPES",
    "DetectorConfig",
    "DetectorTimeoutError",
    "Match",
    "SensitiveTypeInfo",
    "UnsafePatternError",
    "compile_safe_regex",
    "detect",
    "list_types",
    "register_detector",
    "registered_type_ids",
    "reset_detectors",
]
