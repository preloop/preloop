"""Merge agent identity, rate limit, and tool config heads.

No-op merge revision: PRs #150 (api_usage_rate_limit), #152
(agent_identity_v2), and the tool-config agent-scope migration each parented
on the previous main head, leaving three alembic heads and breaking
``alembic upgrade head``. This revision only reunifies the graph.

Revision ID: 20260802_merge_heads
Revises: 20260801_api_usage_rate_limit, 20260801_agent_identity_v2,
    20260801_tool_config_agent_scope
Create Date: 2026-08-02 12:17:41.123358

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "20260802_merge_heads"
down_revision: Union[str, Sequence[str], None] = (
    "20260801_api_usage_rate_limit",
    "20260801_agent_identity_v2",
    "20260801_tool_config_agent_scope",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema (no-op merge)."""


def downgrade() -> None:
    """Downgrade schema (no-op merge)."""
