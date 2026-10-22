"""Backfill residual credentials before removing the legacy columns.

Revision ID: 20261110_credential_backfill
Revises: 20261109_agent_usage_totals

Drain old writers before applying. This revision is an operator-visible stopping
point; the next revision removes the columns. No plaintext is reconstructed on
rollback. Keep this migration independent of the application's ORM schema.
"""

import hashlib
import uuid

from alembic import op
import sqlalchemy as sa

revision = "20261110_credential_backfill"
down_revision = "20261109_agent_usage_totals"
branch_labels = None
depends_on = None

_BATCH_SIZE = 250


def _move_secrets(table_name: str, column: str, reference: str, kind: str) -> None:
    """Move a bounded batch at a time, preserving authoritative references."""
    from preloop.config import settings
    from preloop.utils.encryption import decrypt_value, encrypt_value

    bind = op.get_bind()
    # Identifiers below are fixed migration constants, never user input.
    while (
        rows := bind.execute(
            sa.text(
                f'SELECT id, account_id, "{column}" AS value, {reference} AS secret_id '
                f'FROM {table_name} WHERE "{column}" IS NOT NULL '
                "ORDER BY id LIMIT :batch_size FOR UPDATE"
            ),
            {"batch_size": _BATCH_SIZE},
        )
        .mappings()
        .all()
    ):
        if not settings.security.encryption_key and (
            not settings.security.secret_key
            or settings.security.secret_key
            == "development_secret_key_do_not_use_in_production"
        ):
            raise RuntimeError(
                "Credential backfill requires the deployment's configured "
                "SECURITY__ENCRYPTION_KEY or SECRET_KEY"
            )
        for row in rows:
            secret_id = row["secret_id"]
            if secret_id is not None:
                existing = (
                    bind.execute(
                        sa.text(
                            "SELECT account_id, backend_type, encrypted_value, external_ref "
                            "FROM secret_reference WHERE id = :id FOR UPDATE"
                        ),
                        {"id": secret_id},
                    )
                    .mappings()
                    .first()
                )
                if existing is None or existing["account_id"] != row["account_id"]:
                    raise RuntimeError("Credential reference ownership requires repair")
                if existing["backend_type"] == "local_encrypted":
                    if not existing["encrypted_value"] or not decrypt_value(
                        existing["encrypted_value"]
                    ):
                        raise RuntimeError("Credential reference is empty")
                elif not existing["external_ref"]:
                    raise RuntimeError("External credential reference requires repair")
            elif row["value"]:
                encrypted = encrypt_value(row["value"])
                # Prove the configured encryption key round-trips before clearing.
                if decrypt_value(encrypted) != row["value"]:
                    raise RuntimeError("Credential encryption verification failed")
                secret_id = uuid.uuid4()
                bind.execute(
                    sa.text(
                        "INSERT INTO secret_reference "
                        "(id, account_id, name, backend_type, secret_kind, "
                        "encrypted_value, status, meta_data, created_at, updated_at) "
                        "VALUES (:id, :account_id, :name, 'local_encrypted', :kind, "
                        ":encrypted, 'active', '{}', now(), now())"
                    ),
                    {
                        "id": secret_id,
                        "account_id": row["account_id"],
                        "name": f"Migrated {kind}",
                        "kind": kind,
                        "encrypted": encrypted,
                    },
                )
            bind.execute(
                sa.text(
                    f"UPDATE {table_name} SET {reference} = :secret_id, "
                    f'"{column}" = NULL WHERE id = :id'
                ),
                {"secret_id": secret_id, "id": row["id"]},
            )


def upgrade() -> None:
    """Backfill atomically; fail closed on conflicting or unreadable credentials."""
    bind = op.get_bind()
    # Prevent an old writer from racing the scan. Old application processes must
    # remain stopped between this revision and the contract revision.
    bind.execute(
        sa.text("LOCK TABLE ai_model, api_key, tracker IN SHARE ROW EXCLUSIVE MODE")
    )
    _move_secrets("ai_model", "api_key", "credentials_secret_id", "ai_model_api_key")
    _move_secrets(
        "tracker", "jira_webhook_secret", "webhook_secret_id", "tracker_webhook_secret"
    )
    while (
        rows := bind.execute(
            sa.text(
                "SELECT id, key, key_hash FROM api_key WHERE key IS NOT NULL "
                "ORDER BY id LIMIT :batch_size FOR UPDATE"
            ),
            {"batch_size": _BATCH_SIZE},
        )
        .mappings()
        .all()
    ):
        for row in rows:
            if not row["key"]:
                # An empty legacy credential must never become an authenticating hash.
                bind.execute(
                    sa.text("UPDATE api_key SET key = NULL WHERE id = :id"),
                    {"id": row["id"]},
                )
                continue
            # codeql[py/weak-sensitive-data-hashing] High-entropy API token fingerprint
            fingerprint = hashlib.sha256(row["key"].encode("utf-8")).hexdigest()
            if row["key_hash"] and row["key_hash"] != fingerprint:
                raise RuntimeError("API key fingerprint conflicts with legacy value")
            bind.execute(
                sa.text(
                    "UPDATE api_key SET key_hash = :fingerprint, key_prefix = :prefix, "
                    "key = NULL WHERE id = :id"
                ),
                {
                    "id": row["id"],
                    "fingerprint": fingerprint,
                    "prefix": row["key"][:12],
                },
            )


def downgrade() -> None:
    """Retain encrypted references and hashes; never restore plaintext."""
