"""Add user.auth_generation so JWT sessions can be revoked everywhere.

Each user carries an integer generation. Access and refresh tokens put
that value in the ``gen`` claim. Incrementing the column rejects every
outstanding JWT for that user without rotating the instance signing
secret or deactivating the account. Existing rows receive 0, which is
also the value treated as present when a pre-change token has no claim.

Revision ID: 20260921_auth_generation
Revises: 20260917_plan_choice
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260921_auth_generation"
down_revision: Union[str, None] = "20260917_plan_choice"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add user.auth_generation, defaulting existing rows to 0."""
    op.add_column(
        "user",
        sa.Column(
            "auth_generation",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Incremented to revoke all outstanding JWT sessions",
        ),
    )


def downgrade() -> None:
    """Drop user.auth_generation."""
    op.drop_column("user", "auth_generation")
