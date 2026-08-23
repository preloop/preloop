"""Generic repository security scanners for opt-in RSA / audit flows.

These tools are not loaded for ordinary agents. A flow opts in by listing
``repo-audit`` in ``allowed_mcp_servers`` and the individual tool names in
``allowed_mcp_tools``.
"""

from preloop.security.opt_in import (
    REPO_AUDIT_SERVER,
    REPO_AUDIT_TOOLS,
    wants_preloop_mcp,
    wants_repo_audit,
)

__all__ = [
    "REPO_AUDIT_SERVER",
    "REPO_AUDIT_TOOLS",
    "wants_preloop_mcp",
    "wants_repo_audit",
]
