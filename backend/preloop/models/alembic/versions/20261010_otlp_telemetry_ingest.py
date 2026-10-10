"""OTLP telemetry ingest: telemetry_estimate cost source, dedup and series state.

Revision ID: 20261010_otlp_telemetry_ingest
Revises: 20261010_callback_receipt
Create Date: 2026-10-10

Issue #1412. Claude clients export OTLP telemetry to Preloop. Usage rows
created from it carry ``cost_source='telemetry_estimate'`` (the client's own
cost figure). ``telemetry_ingest_dedup`` makes re-sent batches no-ops through
a unique index, ``telemetry_metric_series`` keeps cumulative metric state for
delta conversion, and four partial indexes make the request id matching
between gateway rows and telemetry rows indexable on both sides.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20261010_otlp_telemetry_ingest"
down_revision: Union[str, None] = "20261010_callback_receipt"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

_WITH_TELEMETRY = (
    "cost_source IS NULL OR cost_source IN "
    "('override', 'model_config', 'provider', 'catalog', 'subscription', "
    "'unpriced', 'reconciled', 'imported', 'telemetry_estimate')"
)
_WITHOUT_TELEMETRY = (
    "cost_source IS NULL OR cost_source IN "
    "('override', 'model_config', 'provider', 'catalog', 'subscription', "
    "'unpriced', 'reconciled', 'imported')"
)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    """Allow telemetry_estimate, create the state tables and indexes."""
    op.drop_constraint("ck_api_usage_cost_source", "api_usage", type_="check")
    op.create_check_constraint("ck_api_usage_cost_source", "api_usage", _WITH_TELEMETRY)

    op.create_table(
        "telemetry_ingest_dedup",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("dedup_key", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("seen_at", sa.DateTime(), nullable=False),
        *_timestamps(),
    )
    op.create_index(
        "uq_telemetry_ingest_dedup_account_key",
        "telemetry_ingest_dedup",
        ["account_id", "dedup_key"],
        unique=True,
    )
    op.create_index(
        "ix_telemetry_ingest_dedup_seen_at", "telemetry_ingest_dedup", ["seen_at"]
    )

    op.create_table(
        "telemetry_metric_series",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("series_key", sa.String(64), nullable=False),
        sa.Column("last_value", sa.Float(), nullable=False),
        sa.Column("last_time", sa.DateTime(), nullable=False),
        sa.Column("start_time", sa.DateTime(), nullable=True),
        *_timestamps(),
    )
    op.create_index(
        "uq_telemetry_metric_series_account_key",
        "telemetry_metric_series",
        ["account_id", "series_key"],
        unique=True,
    )
    op.create_index(
        "ix_telemetry_metric_series_updated_at",
        "telemetry_metric_series",
        ["updated_at"],
    )

    op.create_index(
        "ix_api_usage_account_upstream_request_id",
        "api_usage",
        ["account_id", "upstream_request_id"],
        postgresql_where=sa.text("upstream_request_id IS NOT NULL"),
    )
    op.create_index(
        "ix_api_usage_gateway_client_request_id",
        "api_usage",
        ["account_id", sa.text("(meta_data->>'client_request_id')")],
        postgresql_where=sa.text(
            "action_type = 'model_gateway' "
            "AND meta_data->>'client_request_id' IS NOT NULL"
        ),
    )
    op.create_index(
        "ix_api_usage_otlp_client_request_id",
        "api_usage",
        ["account_id", sa.text("(meta_data->>'otlp_client_request_id')")],
        postgresql_where=sa.text(
            "action_type = 'imported_usage' "
            "AND meta_data->>'otlp_client_request_id' IS NOT NULL"
        ),
    )
    op.create_index(
        "ix_api_usage_otlp_request_id",
        "api_usage",
        ["account_id", sa.text("(meta_data->>'otlp_request_id')")],
        postgresql_where=sa.text(
            "action_type = 'imported_usage' "
            "AND meta_data->>'otlp_request_id' IS NOT NULL"
        ),
    )


def downgrade() -> None:
    """Drop the indexes and tables and restore the previous constraint.

    Rows priced from client telemetry keep their amount and fall back to the
    ``imported`` marker so the old constraint can be reinstated.
    """
    for name in (
        "ix_api_usage_otlp_request_id",
        "ix_api_usage_otlp_client_request_id",
        "ix_api_usage_gateway_client_request_id",
        "ix_api_usage_account_upstream_request_id",
    ):
        op.drop_index(name, table_name="api_usage")
    op.drop_index(
        "ix_telemetry_metric_series_updated_at", table_name="telemetry_metric_series"
    )
    op.drop_index(
        "uq_telemetry_metric_series_account_key", table_name="telemetry_metric_series"
    )
    op.drop_table("telemetry_metric_series")
    op.drop_index(
        "ix_telemetry_ingest_dedup_seen_at", table_name="telemetry_ingest_dedup"
    )
    op.drop_index(
        "uq_telemetry_ingest_dedup_account_key", table_name="telemetry_ingest_dedup"
    )
    op.drop_table("telemetry_ingest_dedup")
    op.execute(
        "UPDATE api_usage SET cost_source = 'imported' "
        "WHERE cost_source = 'telemetry_estimate'"
    )
    op.drop_constraint("ck_api_usage_cost_source", "api_usage", type_="check")
    op.create_check_constraint(
        "ck_api_usage_cost_source", "api_usage", _WITHOUT_TELEMETRY
    )
