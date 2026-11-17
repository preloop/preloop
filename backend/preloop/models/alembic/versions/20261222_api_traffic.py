"""Record REST traffic of every credential: minute rollups and problem events."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261222_api_traffic"
down_revision = "20261221_records_merge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the two new, empty tables and their indexes."""
    op.create_table(
        "api_traffic_rollup",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("principal_kind", sa.String(16), nullable=False),
        sa.Column("method", sa.String(10), nullable=False),
        sa.Column("route_template", sa.String(255), nullable=False),
        sa.Column("status_class", sa.SmallInteger(), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("total_duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("max_duration_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "account_id",
            "bucket_start",
            "principal_kind",
            "method",
            "route_template",
            "status_class",
            name="uq_api_traffic_rollup_key",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_api_traffic_rollup_bucket", "api_traffic_rollup", ["bucket_start"]
    )
    op.create_table(
        "api_traffic_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("principal_kind", sa.String(16), nullable=False),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("method", sa.String(10), nullable=False),
        sa.Column("route_template", sa.String(255), nullable=False),
        sa.Column("status_code", sa.SmallInteger(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_api_traffic_event_account_time",
        "api_traffic_event",
        ["account_id", "occurred_at"],
    )
    op.create_index("ix_api_traffic_event_time", "api_traffic_event", ["occurred_at"])


def downgrade() -> None:
    """Drop the traffic tables."""
    op.drop_table("api_traffic_event")
    op.drop_table("api_traffic_rollup")
