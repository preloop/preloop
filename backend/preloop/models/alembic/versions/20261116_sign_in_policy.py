"""Add inert account sign-in policy storage.

Revision ID: 20261116_sign_in_policy
Revises: 20261211_external_grant_idx
"""

import sqlalchemy as sa
from alembic import op

revision = "20261116_sign_in_policy"
down_revision = "20261211_external_grant_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Leave existing accounts unrestricted until a policy provider enables them."""
    op.add_column(
        "account",
        sa.Column(
            "strong_sign_in_policy", sa.String(8), nullable=False, server_default="off"
        ),
    )
    op.create_check_constraint(
        "ck_account_strong_sign_in_policy",
        "account",
        "strong_sign_in_policy IN ('off', 'admins', 'all')",
    )


def downgrade() -> None:
    """Remove policy storage without touching any identity or factor."""
    op.drop_constraint("ck_account_strong_sign_in_policy", "account", type_="check")
    op.drop_column("account", "strong_sign_in_policy")
