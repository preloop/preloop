"""Index the opening user request used to recognize sessions.

Revision ID: 20270101_session_prompt
Revises: 20261231_subagent_role
"""

from alembic import op
import sqlalchemy as sa

revision: str = "20270101_session_prompt"
down_revision: str = "20261231_subagent_role"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    """Keep first-prompt lookup independent of later transcript volume."""
    op.create_index(
        "ix_rsa_session_opening_prompt",
        "runtime_session_activity",
        ["account_id", "runtime_session_id", "timestamp", "id"],
        postgresql_where=sa.text(
            "activity_type = 'transcript_message' AND metadata ->> 'role' = 'user'"
        ),
    )


def downgrade() -> None:
    """Remove the optional opening-prompt lookup index."""
    op.drop_index(
        "ix_rsa_session_opening_prompt", table_name="runtime_session_activity"
    )
