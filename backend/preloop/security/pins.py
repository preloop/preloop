"""Pinned versions for OSS scanners the Preloop MCP tools wrap.

Absence of a binary is an error, never a clean/MET result. A gitleaks
finding count of 0 is not a secrets-hygiene MET; that decision lives in
gap-register / result.json validation, not in the wrapper.
"""

from __future__ import annotations

RECOMMENDED_GITLEAKS_VERSION = "8.24.3"
RECOMMENDED_ZIZMOR_VERSION = "1.16.0"
