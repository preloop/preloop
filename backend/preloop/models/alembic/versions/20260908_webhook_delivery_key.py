"""Delivery-level idempotency key for webhook-sourced flow executions.

Revision ID: 20260908_webhook_delivery_key
Revises: 20260908_structured_answer
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260908_webhook_delivery_key"
down_revision: Union[str, None] = "20260908_structured_answer"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

UNIQUE_INDEX = "uq_flow_execution_webhook_delivery"
CONTENT_INDEX = "ix_flow_execution_webhook_content_key"
LEGACY_INDEX = "ix_flow_execution_trigger_delivery_id"


def upgrade() -> None:
    """Add flow_execution.webhook_delivery_key plus its lookup indexes.

    Additive and backfill-free on purpose. A backfill would have to invent a
    key for rows written before this column existed, and the incident proves
    duplicates already exist in production, so the unique index below could
    not be created over backfilled values. Legacy rows stay reachable through
    LEGACY_INDEX, which indexes the delivery id where it has always been
    stored: inside trigger_event_details.
    """
    op.add_column(
        "flow_execution",
        sa.Column(
            "webhook_delivery_key",
            sa.String(length=200),
            nullable=True,
            comment=(
                "Idempotency key of the webhook delivery that created this "
                "execution: 'delivery:<provider delivery id>' or "
                "'content:<sha256 prefix>' when the source sends no delivery id"
            ),
        ),
    )

    # One execution per (flow, provider delivery). Partial so that only real
    # provider delivery ids are constrained: content fingerprints repeat
    # legitimately (the same label can be applied again next week), they are
    # deduplicated by a short time window in the service layer instead.
    op.create_index(
        UNIQUE_INDEX,
        "flow_execution",
        ["flow_id", "webhook_delivery_key"],
        unique=True,
        postgresql_where=sa.text("webhook_delivery_key LIKE 'delivery:%'"),
    )
    op.create_index(
        CONTENT_INDEX,
        "flow_execution",
        ["flow_id", "webhook_delivery_key", "start_time"],
        unique=False,
        postgresql_where=sa.text("webhook_delivery_key LIKE 'content:%'"),
    )
    # Rows created before this migration (and by paths that precreate an
    # execution, e.g. issue lifecycle pickups) only carry the delivery id in
    # JSONB. Index that expression so the guard can still find them.
    op.create_index(
        LEGACY_INDEX,
        "flow_execution",
        ["flow_id", sa.text("(trigger_event_details ->> 'delivery_id')")],
        unique=False,
        postgresql_where=sa.text(
            "(trigger_event_details ->> 'delivery_id') IS NOT NULL"
        ),
    )


def downgrade() -> None:
    """Drop the indexes and the column; no other column is touched."""
    op.drop_index(LEGACY_INDEX, table_name="flow_execution")
    op.drop_index(CONTENT_INDEX, table_name="flow_execution")
    op.drop_index(UNIQUE_INDEX, table_name="flow_execution")
    op.drop_column("flow_execution", "webhook_delivery_key")
