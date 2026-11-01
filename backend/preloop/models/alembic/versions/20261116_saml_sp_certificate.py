"""Persist the public certificate paired with the SAML SP secret key.

Revision ID: 20261116_saml_sp_certificate
Revises: 20261115_sso_enforcement
"""

from alembic import op
import sqlalchemy as sa

revision = "20261116_saml_sp_certificate"
down_revision = "20261115_sso_enforcement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "account_sso_connection",
        sa.Column("saml_sp_certificate", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("account_sso_connection", "saml_sp_certificate")
