"""route tracker credentials through the secret service

Adds credentials_secret_id / webhook_secret_id FKs to the tracker table and
makes the legacy plaintext api_key column nullable, so tracker API keys and
Jira webhook secrets can be stored encrypted at rest via SecretReference.

Revision ID: 20260712_tracker_secret
Revises: 20260712_override_fx
Create Date: 2026-07-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "20260712_tracker_secret"
down_revision: Union[str, None] = "20260712_override_fx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static analysis
# treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    op.add_column(
        "tracker",
        sa.Column(
            "credentials_secret_id",
            UUID(as_uuid=True),
            sa.ForeignKey("secret_reference.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "tracker",
        sa.Column(
            "webhook_secret_id",
            UUID(as_uuid=True),
            sa.ForeignKey("secret_reference.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_tracker_credentials_secret_id",
        "tracker",
        ["credentials_secret_id"],
    )
    op.create_index(
        "ix_tracker_webhook_secret_id",
        "tracker",
        ["webhook_secret_id"],
    )
    # The credential now lives in a SecretReference; the plaintext column
    # becomes nullable and is left NULL for new/migrated rows.
    op.alter_column(
        "tracker",
        "api_key",
        existing_type=sa.String(length=1000),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "tracker",
        "api_key",
        existing_type=sa.String(length=1000),
        nullable=False,
        server_default="",
    )
    op.drop_index("ix_tracker_webhook_secret_id", table_name="tracker")
    op.drop_index("ix_tracker_credentials_secret_id", table_name="tracker")
    op.drop_column("tracker", "webhook_secret_id")
    op.drop_column("tracker", "credentials_secret_id")
