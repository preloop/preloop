"""Add an opt-in GitHub repository_dispatch sink to CI completion endpoints.

Revision ID: 20261205_ci_dispatch_sink
Revises: 20261204_ci_principal_retire

A restricted CI completion subscription can deliver ``flow.execution.finished``
as a GitHub ``repository_dispatch`` instead of an HTTP POST to a customer
receiver. The dispatch credential is stored encrypted on the endpoint row,
separately from the signing secret, and is never returned by any read.
Existing rows keep ``delivery_kind = 'http'``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261205_ci_dispatch_sink"
down_revision: Union[str, None] = "20261204_ci_principal_retire"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the delivery kind and the encrypted dispatch settings."""
    op.add_column(
        "webhook_endpoint",
        sa.Column(
            "delivery_kind",
            sa.String(length=32),
            nullable=False,
            server_default="http",
        ),
    )
    op.add_column(
        "webhook_endpoint",
        sa.Column("dispatch_event_type", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "webhook_endpoint",
        sa.Column("dispatch_token_encrypted", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_webhook_endpoint_delivery_kind",
        "webhook_endpoint",
        "delivery_kind IN ('http', 'github_dispatch')",
    )
    op.create_check_constraint(
        "ck_webhook_endpoint_dispatch_settings",
        "webhook_endpoint",
        "(delivery_kind = 'http' AND dispatch_event_type IS NULL "
        "AND dispatch_token_encrypted IS NULL) OR "
        "(delivery_kind = 'github_dispatch' AND dispatch_event_type IS NOT NULL "
        "AND dispatch_token_encrypted IS NOT NULL "
        "AND ci_subscription_binding IS NOT NULL)",
    )


def downgrade() -> None:
    """Drop dispatch endpoints first: they cannot exist as plain HTTP rows."""
    op.execute("DELETE FROM webhook_endpoint WHERE delivery_kind = 'github_dispatch'")
    op.drop_constraint(
        "ck_webhook_endpoint_dispatch_settings", "webhook_endpoint", type_="check"
    )
    op.drop_constraint(
        "ck_webhook_endpoint_delivery_kind", "webhook_endpoint", type_="check"
    )
    op.drop_column("webhook_endpoint", "dispatch_token_encrypted")
    op.drop_column("webhook_endpoint", "dispatch_event_type")
    op.drop_column("webhook_endpoint", "delivery_kind")
