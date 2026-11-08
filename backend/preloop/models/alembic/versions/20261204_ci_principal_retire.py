"""Allow a retired CI identity to release its flow binding.

Revision ID: 20261204_ci_principal_retire
Revises: 20261203_session_keys_idx

``ci_principal.flow_id`` is an ON DELETE RESTRICT key, and CI identities are
never deleted, so a flow that ever had a CI identity could not be deleted.
Retiring an identity now clears ``flow_id`` (the original flow stays in the
grant). The column becomes nullable; the RESTRICT key is unchanged, so an
active identity still blocks its flow's deletion.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261204_ci_principal_retire"
down_revision: Union[str, None] = "20261203_session_keys_idx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("ci_principal", "flow_id", existing_type=sa.UUID(), nullable=True)


def downgrade() -> None:
    retired = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM ci_principal WHERE flow_id IS NULL"))
        .scalar()
    )
    if retired:
        raise RuntimeError(
            f"{retired} retired CI identities have no flow binding; "
            "downgrade would need to delete them"
        )
    op.alter_column("ci_principal", "flow_id", existing_type=sa.UUID(), nullable=False)
