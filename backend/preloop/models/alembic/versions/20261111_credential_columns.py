"""Keep the legacy credential columns until no old writer can run.

Revision ID: 20261111_credential_columns
Revises: 20261110_credential_backfill

This revision originally removed ``ai_model.api_key``, ``api_key.key`` and
``tracker.jira_webhook_secret``. Helm runs migrations in a pre-upgrade hook
while the previous release still serves traffic, and that release maps these
columns on every API key and model lookup, so removing them in the same
release fails those requests until the rollout finishes. The removal moves to
a later release, which must rerun the credential backfill first because the
previous release can still write plaintext during the rollout. The revision id
stays so databases that already applied it keep a valid history.
"""

from alembic import op

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
    """Leave the legacy columns in place; the application no longer maps them."""


def downgrade() -> None:
    """Restore the nullable columns where an earlier version of this revision ran."""
    op.execute("ALTER TABLE ai_model ADD COLUMN IF NOT EXISTS api_key varchar")
    op.execute("ALTER TABLE api_key ADD COLUMN IF NOT EXISTS key varchar(100)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_api_key_key ON api_key (key)")
    op.execute(
        "ALTER TABLE tracker ADD COLUMN IF NOT EXISTS jira_webhook_secret varchar(255)"
    )
