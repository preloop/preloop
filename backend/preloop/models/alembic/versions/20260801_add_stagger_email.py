"""Add stagger_email and merge alembic heads from parallel main merges.

Revision ID: 20260801_stagger_email
Revises: 20260801_api_usage_rate_limit, 20260801_agent_identity_v2,
    20260801_tool_config_agent_scope
Create Date: 2026-08-01

Per-user toggle for staggered approval email (push first, email only if
still pending after 60s). Default True so existing rows opt in.

Also an Alembic merge revision: joins the three sibling heads that landed
on main without re-parent rotation (#132/#150/#151/#152) so
``alembic upgrade head`` has a single head again. Do not re-parent those
merged revisions — environments may already have applied them as siblings.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260801_stagger_email"
down_revision = (
    "20260801_api_usage_rate_limit",
    "20260801_agent_identity_v2",
    "20260801_tool_config_agent_scope",
)
branch_labels = None
depends_on = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add stagger_email column with server default true."""
    op.add_column(
        "notification_preferences",
        sa.Column(
            "stagger_email",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment=(
                "When True and push is enabled, delay approval email until "
                "the request is still pending after the stagger window"
            ),
        ),
    )


def downgrade() -> None:
    """Remove stagger_email column."""
    op.drop_column("notification_preferences", "stagger_email")
