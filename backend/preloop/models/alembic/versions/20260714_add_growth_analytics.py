"""Add growth analytics tables and Event enrichment columns.

Revision ID: 20260714_growth_analytics
Revises: 20260713_approval_summary
Create Date: 2026-07-14

Adds the first-party analytics schema consumed by the EE growth plugin:

- ``visitor``: one row per browser install (client-generated UUID), holding
  first-touch attribution and the latest device profile.
- ``identity_link``: the cross-device identity graph (visitor / fingerprint /
  cli_client / mobile_device / oss_instance ↔ user/account).
- ``account_milestone``: idempotent first-time activation records.
- ``event`` gains nullable ``visitor_id``, ``client_type``, ``browser``,
  ``os`` enrichment columns.

On OSS deployments these tables exist but stay empty — all writers live in
the EE growth plugin.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "20260714_growth_analytics"
down_revision: Union[str, None] = "20260713_approval_summary"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static analysis
# treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Create analytics tables and Event columns."""
    op.create_table(
        "visitor",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("fingerprint", sa.String(128), nullable=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_landing_path", sa.String(512), nullable=True),
        sa.Column("first_referrer", sa.String(512), nullable=True),
        sa.Column("utm_source", sa.String(128), nullable=True),
        sa.Column("utm_medium", sa.String(128), nullable=True),
        sa.Column("utm_campaign", sa.String(128), nullable=True),
        sa.Column("utm_term", sa.String(128), nullable=True),
        sa.Column("utm_content", sa.String(128), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("browser", sa.String(64), nullable=True),
        sa.Column("browser_version", sa.String(32), nullable=True),
        sa.Column("os", sa.String(64), nullable=True),
        sa.Column("os_version", sa.String(32), nullable=True),
        sa.Column("device_type", sa.String(16), nullable=True),
        sa.Column("screen_width", sa.Integer(), nullable=True),
        sa.Column("screen_height", sa.Integer(), nullable=True),
        sa.Column("languages", JSONB(), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("last_country", sa.String(100), nullable=True),
    )
    op.create_index("ix_visitor_fingerprint", "visitor", ["fingerprint"])
    op.create_index("ix_visitor_user_id", "visitor", ["user_id"])
    op.create_index("ix_visitor_last_seen", "visitor", ["last_seen"])

    op.create_table(
        "identity_link",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("principal_type", sa.String(32), nullable=False),
        sa.Column("principal_id", sa.String(128), nullable=False),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("source", sa.String(32), nullable=True),
        sa.Column("first_linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("meta", JSONB(), nullable=True),
        sa.UniqueConstraint(
            "principal_type",
            "principal_id",
            "user_id",
            name="uq_identity_link_principal_user",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index("ix_identity_link_principal_id", "identity_link", ["principal_id"])
    op.create_index("ix_identity_link_user_id", "identity_link", ["user_id"])
    op.create_index("ix_identity_link_account_id", "identity_link", ["account_id"])

    op.create_table(
        "account_milestone",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("milestone", sa.String(64), nullable=False),
        sa.Column("achieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("meta", JSONB(), nullable=True),
        sa.UniqueConstraint(
            "account_id",
            "milestone",
            name="uq_account_milestone_account_milestone",
        ),
    )
    op.create_index(
        "ix_account_milestone_account_id", "account_milestone", ["account_id"]
    )
    op.create_index(
        "ix_account_milestone_milestone", "account_milestone", ["milestone"]
    )
    op.create_index(
        "ix_account_milestone_achieved_at", "account_milestone", ["achieved_at"]
    )

    op.add_column(
        "event",
        sa.Column(
            "visitor_id",
            UUID(as_uuid=True),
            nullable=True,
            comment="Client-generated visitor id (see Visitor model)",
        ),
    )
    op.add_column(
        "event",
        sa.Column(
            "client_type",
            sa.String(20),
            nullable=True,
            comment="web | mobile_web | ios | android | cli | oss_instance | api | system",
        ),
    )
    op.add_column(
        "event",
        sa.Column(
            "browser",
            sa.String(64),
            nullable=True,
            comment="Browser family parsed from user agent",
        ),
    )
    op.add_column(
        "event",
        sa.Column(
            "os",
            sa.String(64),
            nullable=True,
            comment="Operating system parsed from user agent",
        ),
    )
    op.create_index("ix_event_visitor_id", "event", ["visitor_id"])
    op.create_index("ix_event_client_type", "event", ["client_type"])


def downgrade() -> None:
    """Drop analytics tables and Event columns."""
    op.drop_index("ix_event_client_type", table_name="event")
    op.drop_index("ix_event_visitor_id", table_name="event")
    op.drop_column("event", "os")
    op.drop_column("event", "browser")
    op.drop_column("event", "client_type")
    op.drop_column("event", "visitor_id")

    op.drop_index("ix_account_milestone_achieved_at", table_name="account_milestone")
    op.drop_index("ix_account_milestone_milestone", table_name="account_milestone")
    op.drop_index("ix_account_milestone_account_id", table_name="account_milestone")
    op.drop_table("account_milestone")

    op.drop_index("ix_identity_link_account_id", table_name="identity_link")
    op.drop_index("ix_identity_link_user_id", table_name="identity_link")
    op.drop_index("ix_identity_link_principal_id", table_name="identity_link")
    op.drop_table("identity_link")

    op.drop_index("ix_visitor_last_seen", table_name="visitor")
    op.drop_index("ix_visitor_user_id", table_name="visitor")
    op.drop_index("ix_visitor_fingerprint", table_name="visitor")
    op.drop_table("visitor")
