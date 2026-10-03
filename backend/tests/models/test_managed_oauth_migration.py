"""PostgreSQL upgrade/rollback preserves legacy GitHub and manual credentials."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from preloop.models import models


def test_managed_oauth_upgrade_rollback_preserves_legacy(db_session: Session) -> None:
    """Exercise real DDL twice inside the fixture's rollback-only transaction."""
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20261002_managed_oauth.py"
    )
    spec = importlib.util.spec_from_file_location("oauth_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    connection = db_session.connection()
    migration.op = Operations(MigrationContext.configure(connection))
    account = models.Account(organization_name="Synthetic legacy account")
    db_session.add(account)
    db_session.flush()
    user = models.User(
        account_id=account.id,
        username=str(uuid4()),
        email=f"{uuid4()}@example.com",
        hashed_password="synthetic",
    )
    db_session.add(user)
    db_session.flush()
    tracker = models.Tracker(
        account_id=account.id,
        name="Manual",
        tracker_type="github",
        auth_type="oauth_token",
        api_key="synthetic-manual",
    )
    db_session.add(tracker)
    db_session.flush()
    grant_id = uuid4()
    # Raw fixture insertion is intentional: it uses only pre-migration columns.
    connection.execute(
        text("""INSERT INTO oauth_token
        (id, account_id, user_id, provider, access_token_encrypted, token_type, expires_at)
        VALUES (:id, :account, :user, 'github', 'synthetic-legacy-ciphertext', 'bearer', :expiry)"""),
        {
            "id": grant_id,
            "account": account.id,
            "user": user.id,
            "expiry": datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
        },
    )
    secret = models.SecretReference(
        account_id=account.id,
        name="Consumer",
        backend_type="local_encrypted",
        secret_kind="oauth_client_secret",
        encrypted_value="synthetic-ciphertext",
    )
    db_session.add(secret)
    db_session.flush()
    config = models.OAuthProviderConfiguration(
        account_id=account.id,
        provider="bitbucket",
        canonical_instance="https://example.com",
        context="workspace",
        client_id="synthetic",
        client_secret_id=secret.id,
        callback_uri="https://example.com/callback",
        selected_permissions=[],
    )
    db_session.add(config)
    db_session.flush()
    transaction = models.OAuthConnectionTransaction(
        account_id=account.id,
        user_id=user.id,
        state_hash="a" * 64,
        session_hash="b" * 64,
        provider="bitbucket",
        configuration_id=config.id,
        configuration_version=1,
        callback_uri=config.callback_uri,
        return_path="/trackers",
        expires_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        status="claimed",
    )
    db_session.add(transaction)
    db_session.flush()
    pending = models.OAuthToken(
        account_id=account.id,
        user_id=user.id,
        provider="bitbucket",
        auth_mode="managed_oauth",
        status="pending",
        configuration_id=config.id,
        configuration_version=1,
        canonical_instance=config.canonical_instance,
        provider_subject="synthetic",
        connection_transaction_id=transaction.id,
        access_token_encrypted="synthetic-ciphertext",
    )
    db_session.add(pending)
    db_session.flush()
    transaction.pending_grant_id = pending.id
    db_session.flush()
    for _ in range(2):
        migration.downgrade()
        assert (
            connection.execute(
                text("SELECT id FROM secret_reference WHERE id=:id"), {"id": secret.id}
            ).first()
            is None
        )
        assert (
            connection.execute(
                text("SELECT id FROM oauth_token WHERE id=:id"), {"id": pending.id}
            ).first()
            is None
        )
        assert (
            "oauth_provider_configuration" not in inspect(connection).get_table_names()
        )
        assert "auth_mode" not in {
            c["name"] for c in inspect(connection).get_columns("oauth_token")
        }
        assert (
            connection.execute(
                text("SELECT access_token_encrypted FROM oauth_token WHERE id=:id"),
                {"id": grant_id},
            ).scalar_one()
            == "synthetic-legacy-ciphertext"
        )
        # A non-UTC session must not reinterpret old naive UTC expiry.
        connection.execute(text("SET LOCAL TIME ZONE 'Europe/Madrid'"))
        migration.upgrade()
        row = connection.execute(
            text(
                "SELECT auth_mode, tracker_id, rotation_version, expires_at FROM oauth_token WHERE id=:id"
            ),
            {"id": grant_id},
        ).one()
        assert (
            row.auth_mode is None
            and row.tracker_id is None
            and row.rotation_version == 0
        )
        assert row.expires_at.timestamp() == 1767268800
        manual = connection.execute(
            text("SELECT api_key, auth_type FROM tracker WHERE id=:id"),
            {"id": tracker.id},
        ).one()
        assert (
            manual.api_key == "synthetic-manual" and manual.auth_type == "oauth_token"
        )
