"""Closed value sets for the account hierarchy, sharing, tag and rule tables.

Each set is enforced by a CHECK constraint built with ``in_list_check``. The
Alembic revisions that create those constraints spell the same lists out
literally, because a revision must not change when this module does.
"""

from __future__ import annotations

GRANT_SUBJECT_TYPES = ("user", "team")
GRANT_ACCESS_LEVELS = ("read", "operate", "admin")
GRANT_TARGET_MODES = ("all", "selected")

# Also the closed set for ``resource_tag.resource_type`` and
# ``access_rule.resource_type``: a tag or rule on any other type could never
# match a share, so a typo there fails instead of silently matching nothing.
SHARE_RESOURCE_TYPES = (
    "ai_model",
    "mcp_server",
    "managed_agent",
    "flow",
    "runner_pool",
    "policy_baseline",
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
# string. The hierarchy adds these two values, stored under the parent
# account's ``account_id``.
BUDGET_SUBJECT_SUBACCOUNT = "subaccount"
BUDGET_SUBJECT_SUBACCOUNTS_TOTAL = "subaccounts_total"


def in_list_check(column: str, values: tuple[str, ...]) -> str:
    """SQL for ``column IN (...)`` over a closed value set."""
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"
