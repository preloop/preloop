"""Unique partial indexes making viewed-event inserts idempotent.

Revision ID: 20260906_ae_viewed_uniq
Revises: 20260905_control_heartbeat
Create Date: 2026-09-06

Console GET and the public token GET both do has_event() then record() for
``viewed`` rows. Concurrent loads of the same request can both pass the
existence check and insert duplicates. PostgreSQL unique indexes treat NULL
as distinct, so authenticated viewers and anonymous token views each need
their own partial unique index. Duplicate rows are collapsed first so the
index can be created on databases that already recorded overlapping views.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "20260906_ae_viewed_uniq"
down_revision: Union[str, None] = "20260905_control_heartbeat"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Dedupe existing viewed rows, then add the unique partial indexes."""
    op.execute(
        text(
            """
            DELETE FROM approval_event
            WHERE id IN (
                SELECT id FROM (
                    SELECT id,
                           row_number() OVER (
                               PARTITION BY approval_request_id, actor_id
                               ORDER BY timestamp ASC, id ASC
                           ) AS rn
                    FROM approval_event
                    WHERE event_type = 'viewed' AND actor_id IS NOT NULL
                ) ranked
                WHERE rn > 1
            )
            """
        )
    )
    op.execute(
        text(
            """
            DELETE FROM approval_event
            WHERE id IN (
                SELECT id FROM (
                    SELECT id,
                           row_number() OVER (
                               PARTITION BY approval_request_id
                               ORDER BY timestamp ASC, id ASC
                           ) AS rn
                    FROM approval_event
                    WHERE event_type = 'viewed' AND actor_id IS NULL
                ) ranked
                WHERE rn > 1
            )
            """
        )
    )
    op.create_index(
        "uq_approval_event_viewed_actor",
        "approval_event",
        ["approval_request_id", "event_type", "actor_id"],
        unique=True,
        postgresql_where=text("event_type = 'viewed' AND actor_id IS NOT NULL"),
    )
    op.create_index(
        "uq_approval_event_viewed_anonymous",
        "approval_event",
        ["approval_request_id", "event_type"],
        unique=True,
        postgresql_where=text("event_type = 'viewed' AND actor_id IS NULL"),
    )


def downgrade() -> None:
    """Drop the viewed-event unique indexes."""
    op.drop_index("uq_approval_event_viewed_anonymous", table_name="approval_event")
    op.drop_index("uq_approval_event_viewed_actor", table_name="approval_event")
