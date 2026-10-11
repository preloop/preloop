"""Record the harness event that delivered a note, and add ``undeliverable``.

Additive: ``delivery_event`` is NULL on every existing row and the widened
status check only permits a new value, so no backfill is needed and no reader
has to change. A note whose only session ended before a turn boundary could
carry it moves to ``undeliverable`` (issue #1551), which is terminal and
distinct from ``expired``.

Revision ID: 20261012_note_delivery_event
Revises: 20261011_audit_outbox
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261012_note_delivery_event"
down_revision: Union[str, None] = "20261011_audit_outbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

TABLE = "agent_control_command"
STATUS_CHECK = "ck_agent_control_command_status"


def upgrade() -> None:
    """Add ``delivery_event`` and allow the ``undeliverable`` note state."""
    op.add_column(
        TABLE,
        sa.Column("delivery_event", sa.String(length=24), nullable=True),
    )
    op.drop_constraint(STATUS_CHECK, TABLE, type_="check")
    op.create_check_constraint(
        STATUS_CHECK,
        TABLE,
        "status IN ('pending', 'delivered', 'acked', 'failed', 'expired', "
        "'cancelled', 'undeliverable')",
    )


def downgrade() -> None:
    """Drop ``delivery_event`` and restore the prior status check.

    Rows already in ``undeliverable`` would violate the narrowed check, so
    they are returned to ``pending``: the prior reader does not understand the
    state and would otherwise fail to load the table.
    """
    op.execute(
        sa.text(
            "UPDATE agent_control_command SET status = 'pending' "
            "WHERE status = 'undeliverable'"
        )
    )
    op.drop_constraint(STATUS_CHECK, TABLE, type_="check")
    op.create_check_constraint(
        STATUS_CHECK,
        TABLE,
        "status IN ('pending', 'delivered', 'acked', 'failed', 'expired', "
        "'cancelled')",
    )
    op.drop_column(TABLE, "delivery_event")