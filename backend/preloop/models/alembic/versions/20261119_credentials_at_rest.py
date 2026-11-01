"""Encrypt stored credentials at rest.

Revision ID: 20261119_credentials_at_rest
Revises: 20261118_scim_provisioning

Schema only, as the migration hook requires. Three plaintext varchar
credential columns become ``text`` so they can hold a sealed value
(``enc:v1:<fernet>``); varchar to text is binary coercible in PostgreSQL, so
the tables are not rewritten. One row per sealed column is registered in
``agent_usage_backfill``; the post-deploy runner
(``preloop.models.credential_encryption``) seals existing rows in committed
batches of 200 from a per-column cursor. The application reads both sealed and
legacy plaintext values, so nothing waits for the backfill.

Downgrade removes the registry rows and keeps the ``text`` type and any sealed
values. Before running a release without encryption at rest, run
``python -m preloop.models.credential_encryption --decrypt``.
"""

from alembic import op

revision = "20261119_credentials_at_rest"
down_revision = "20261118_scim_provisioning"
branch_labels = None
depends_on = None

#: Plaintext varchar credential columns widened to text for sealed values.
_TEXT_COLUMNS = (
    ("tracker", "api_key"),
    ("organization", "webhook_secret"),
    ("webhook", "secret"),
)

#: Registry rows, one per column in preloop.models.credential_encryption.
_BACKFILLS = (
    "cred_tracker_api_key",
    "cred_org_webhook_secret",
    "cred_webhook_secret",
    "cred_mcp_auth_config",
    "cred_flow_webhook_config",
    "cred_approval_config",
    "cred_policy_snapshot",
    "cred_agent_discovered",
    "cred_agent_managed",
    "cred_agent_backup",
)


def upgrade() -> None:
    """Widen credential columns and register the sealing backfills."""
    for table, column in _TEXT_COLUMNS:
        op.execute(f'ALTER TABLE {table} ALTER COLUMN "{column}" TYPE text')
    for name in _BACKFILLS:
        op.execute(
            "INSERT INTO agent_usage_backfill(name) "
            f"VALUES ('{name}') ON CONFLICT DO NOTHING"
        )


def downgrade() -> None:
    """Unregister the backfills; sealed values and text columns stay."""
    names = ", ".join(f"'{name}'" for name in _BACKFILLS)
    op.execute(f"DELETE FROM agent_usage_backfill WHERE name IN ({names})")
