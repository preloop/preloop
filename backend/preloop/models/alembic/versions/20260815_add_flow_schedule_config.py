"""Add flow.schedule_config for native scheduled (cron) triggers.

Flows with ``trigger_event_source == 'schedule'`` store their cron
expression and IANA timezone in this JSON column, e.g.::

    {"cron": "0 6 * * 1-5", "timezone": "Europe/Athens"}

Revision ID: 20260815_flow_schedule_config
Revises: 20260815_flow_exec_batch_id
Create Date: 2026-08-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260815_flow_schedule_config"
down_revision: Union[str, None] = "20260815_flow_exec_batch_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    op.add_column(
        "flow",
        sa.Column(
            "schedule_config",
            sa.JSON(),
            nullable=True,
            comment="Cron expression + IANA timezone for schedule triggers",
        ),
    )


def downgrade() -> None:
    op.drop_column("flow", "schedule_config")
