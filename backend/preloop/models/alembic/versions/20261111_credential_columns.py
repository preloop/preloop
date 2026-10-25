"""Remove the legacy credential columns after the backfill stopping point.

Revision ID: 20261111_credential_columns
Revises: 20261110_credential_backfill
"""

from alembic import op
import sqlalchemy as sa

revision = "20261111_credential_columns"
down_revision = "20261110_credential_backfill"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("ai_model", "api_key"),
    ("api_key", "key"),
    ("tracker", "jira_webhook_secret"),
)


def upgrade() -> None:
    """Refuse removal if an old writer reintroduced any legacy material."""
    bind = op.get_bind()
    bind.execute(
        sa.text("LOCK TABLE ai_model, api_key, tracker IN ACCESS EXCLUSIVE MODE")
    )
    for table, column in _COLUMNS:
        if bind.execute(
            sa.text(
                f'SELECT EXISTS (SELECT 1 FROM {table} WHERE "{column}" IS NOT NULL)'
            )
        ).scalar():
            raise RuntimeError(
                "Legacy credentials remain; stop old writers and rerun the credential backfill"
            )
    for table, column in _COLUMNS:
        op.drop_column(table, column)


def downgrade() -> None:
    """Restore nullable schema only; hashes and encrypted references stay intact."""
    op.add_column("ai_model", sa.Column("api_key", sa.String(), nullable=True))
    op.add_column("api_key", sa.Column("key", sa.String(100), nullable=True))
    op.create_index("ix_api_key_key", "api_key", ["key"], unique=True)
    op.add_column(
        "tracker", sa.Column("jira_webhook_secret", sa.String(255), nullable=True)
    )
