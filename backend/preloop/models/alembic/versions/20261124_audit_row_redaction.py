"""Record retroactive redaction of sealed audit rows.

Revision ID: 20261124_audit_row_redaction
Revises: 20261123_secret_rotation

Adds three nullable columns to ``audit_log``. A sealed row whose content is
rewritten by the stored-row redaction keeps its ``row_hash`` (the next row and
every checkpoint commit to it) and records the hash of its redacted content
and the ``redaction_applied`` audit row that vouches for it. Schema only; the
rewrite is an operator action, never a migration step.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20261124_audit_row_redaction"
down_revision = "20261123_secret_rotation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the redaction columns."""
    op.add_column(
        "audit_log",
        sa.Column(
            "redacted_row_hash",
            sa.String(64),
            nullable=True,
            comment="row hash over the redacted content, same seq and prev_hash",
        ),
    )
    op.add_column(
        "audit_log",
        sa.Column(
            "redaction_event_id",
            UUID(as_uuid=True),
            nullable=True,
            comment="The redaction_applied audit row that lists this rewrite",
        ),
    )
    op.add_column(
        "audit_log",
        sa.Column("redacted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Drop the redaction columns."""
    op.drop_column("audit_log", "redacted_at")
    op.drop_column("audit_log", "redaction_event_id")
    op.drop_column("audit_log", "redacted_row_hash")
