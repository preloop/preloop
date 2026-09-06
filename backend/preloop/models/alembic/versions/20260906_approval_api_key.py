"""Record which API key raised an approval request.

Attribution surfaces must always answer "who asked". The agent, runtime
session and flow run are only sometimes known, but every approval arrives on
an authenticated call, so the key is the one fact that is always available.

Revision ID: 20260906_approval_api_key
Revises: 20260906_halt_launch_intent
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20260906_approval_api_key"
down_revision = "20260906_halt_launch_intent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable api_key_id link plus its lookup index."""
    op.add_column(
        "approval_request",
        sa.Column(
            "api_key_id",
            UUID(as_uuid=True),
            nullable=True,
            comment="API key the requesting caller authenticated with (if known)",
        ),
    )
    op.create_index(
        "ix_approval_request_api_key_id",
        "approval_request",
        ["api_key_id"],
    )
    # SET NULL, not CASCADE: revoking a key must not delete the approval
    # history that key produced.
    op.create_foreign_key(
        "fk_approval_request_api_key_id",
        "approval_request",
        "api_key",
        ["api_key_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Drop the API key attribution link."""
    op.drop_constraint(
        "fk_approval_request_api_key_id", "approval_request", type_="foreignkey"
    )
    op.drop_index("ix_approval_request_api_key_id", table_name="approval_request")
    op.drop_column("approval_request", "api_key_id")
