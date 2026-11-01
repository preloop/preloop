"""Record flow owners and the owner-flow subscription opt-in.

``flow.created_by_user_id`` names the user who created a flow. The model
gateway compares it with the owner of a subscription-OAuth model before a
hosted flow execution may use that subscription.

``ai_model.owner_flow_use_user_id`` is the opt-in: the user who allowed their
own flows to use this subscription model. Storing the user rather than a
boolean keeps the grant self-checking: when the subscription's owner changes,
the stored user no longer matches and the grant stops applying.

Existing flows in accounts with exactly one user are backfilled with that
user; flows in multi-user accounts keep NULL (no recorded owner, never
eligible).

Revision ID: 20261114_owner_flow_subscription
Revises: 20261113_pull_request_details
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261114_owner_flow_subscription"
down_revision = "20261113_pull_request_details"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add both nullable owner columns and backfill single-user accounts."""
    op.add_column(
        "flow",
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_flow_created_by_user_id", "flow", ["created_by_user_id"], unique=False
    )
    op.add_column(
        "ai_model",
        sa.Column(
            "owner_flow_use_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE flow AS f
           SET created_by_user_id = sole.user_id
          FROM (
                SELECT account_id, MIN(id::text)::uuid AS user_id
                  FROM "user"
                 GROUP BY account_id
                HAVING COUNT(*) = 1
               ) AS sole
         WHERE f.account_id = sole.account_id
           AND f.is_preset IS FALSE
           AND f.created_by_user_id IS NULL
        """
    )


def downgrade() -> None:
    """Drop the opt-in and flow owner columns."""
    op.drop_column("ai_model", "owner_flow_use_user_id")
    op.drop_index("ix_flow_created_by_user_id", table_name="flow")
    op.drop_column("flow", "created_by_user_id")
