"""Closed value sets for the account hierarchy, sharing, tag and rule tables.

Each set is enforced by a CHECK constraint built with ``in_list_check``. The
Alembic revisions that create those constraints spell the same lists out
literally, because a revision must not change when this module does.
"""

from __future__ import annotations

GRANT_SUBJECT_TYPES = ("user", "team")
GRANT_ACCESS_LEVELS = ("read", "operate", "admin")
GRANT_TARGET_MODES = ("all", "selected")

# The resource kinds a ``resource_share`` can carry.
SHARE_RESOURCE_TYPES = (
    "ai_model",
    "mcp_server",
    "managed_agent",
    "flow",
    "runner_pool",
    "policy_baseline",
)
# The closed set for ``resource_tag.resource_type`` and
# ``access_rule.resource_type``. Wider than the shareable kinds: rules gate
# ``tool:call`` and ``runner:accept`` on MCP tools and runners, which are never
# shared on their own, and account tags (``customer:<x>``) select subaccounts.
# Closed so that a typo in a ``forbid`` rule fails instead of matching nothing.
TAGGABLE_RESOURCE_TYPES = (
    *SHARE_RESOURCE_TYPES,
    "account",
    "mcp_tool",
    "runner",
    "policy",
    "tracker",
)
SHARE_TARGET_MODES = ("all", "selected", "rule")

TAG_KEY_PATTERN = "^[a-z0-9._/-]{1,63}$"
TAG_VALUE_MAX_LENGTH = 128
TAG_GOVERNED_BY = ("owner", "parent")

RULE_EFFECTS = ("permit", "forbid")
RULE_ACTIONS = (
    "model:invoke",
    "tool:call",
    "flow:run",
    "runner:accept",
    "resource:view",
    "resource:share",
)
RULE_SCOPES = ("self", "subaccounts", "self_and_subaccounts")

# Budget policies need no table: ``budget_policies.subject_type`` is a free
# string. The hierarchy reserves these two values, stored under the parent
# account's ``account_id``. Nothing writes them yet:
# ``crud.budget_configuration.validate_budget_subject`` still rejects both, and
# accepting them there belongs to the hierarchy budgets change.
BUDGET_SUBJECT_SUBACCOUNT = "subaccount"
BUDGET_SUBJECT_SUBACCOUNTS_TOTAL = "subaccounts_total"


def in_list_check(column: str, values: tuple[str, ...]) -> str:
    """SQL for ``column IN (...)`` over a closed value set."""
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"
