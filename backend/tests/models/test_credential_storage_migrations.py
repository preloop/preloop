"""Exercise staged credential migration against isolated PostgreSQL schemas."""

import hashlib
import importlib
import uuid
from collections.abc import Iterator

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, inspect, text

from preloop.models.crud import crud_api_key
from preloop.utils.encryption import decrypt_value, encrypt_value

backfill = importlib.import_module(
    "preloop.models.alembic.versions.20261110_credential_backfill"
)
contract = importlib.import_module(
    "preloop.models.alembic.versions.20261111_credential_columns"
)


@pytest.fixture
def legacy(db_engine, monkeypatch) -> Iterator[Connection]:
    """Create only the historical columns, never modify the application's schema."""
    from preloop.config import settings
    from preloop.utils.encryption import reset_encryption_cache

    monkeypatch.setattr(settings.security, "secret_key", "synthetic-migration-test-key")
    reset_encryption_cache()
    with db_engine.connect() as conn, conn.begin():
        schema = f"credential_test_{uuid.uuid4().hex}"
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        for ddl in (
            "CREATE TABLE secret_reference (id uuid PRIMARY KEY, account_id uuid, name text, "
            "backend_type text, secret_kind text, encrypted_value text, external_ref text, "
            "status text, meta_data jsonb, created_at timestamp, updated_at timestamp)",
            "CREATE TABLE ai_model (id uuid PRIMARY KEY, account_id uuid, api_key text, "
            "credentials_secret_id uuid REFERENCES secret_reference(id))",
            "CREATE TABLE tracker (id uuid PRIMARY KEY, account_id uuid, "
            "jira_webhook_secret varchar(255), webhook_secret_id uuid REFERENCES secret_reference(id))",
            "CREATE TABLE api_key (id uuid PRIMARY KEY, key varchar(100), "
            "key_hash varchar(64) UNIQUE, key_prefix varchar(16))",
        ):
            conn.execute(text(ddl))
        with Operations.context(MigrationContext.configure(conn)):
            yield conn
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    reset_encryption_cache()


def seed(conn: Connection) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed user/global model credentials, webhook credentials and a legacy key."""
    account, model_id, key_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conn.execute(
        text("INSERT INTO ai_model VALUES (:id, :account, :value, NULL)"),
        {"id": model_id, "account": account, "value": "synthetic-model-secret"},
    )
    conn.execute(
        text("INSERT INTO ai_model VALUES (:id, NULL, :value, NULL)"),
        {"id": uuid.uuid4(), "value": "synthetic-global-secret"},
    )
    conn.execute(
        text("INSERT INTO tracker VALUES (:id, :account, :value, NULL)"),
        {"id": uuid.uuid4(), "account": account, "value": "synthetic-webhook-secret"},
    )
    conn.execute(
        text("INSERT INTO api_key VALUES (:id, :key, NULL, NULL)"),
        {"id": key_id, "key": "synthetic-user-token"},
    )
    return account, model_id, key_id


def legacy_columns(conn: Connection) -> set[tuple[str, str]]:
    """Return which legacy credential columns still exist."""
    return {
        (table, column)
        for table, column in contract._COLUMNS
        if column in {c["name"] for c in inspect(conn).get_columns(table)}
    }


def test_backfill_then_contract_preserves_credentials_and_is_idempotent(
    legacy,
) -> None:
    seed(legacy)
    backfill.upgrade()
    secrets = legacy.execute(
        text("SELECT secret_kind, encrypted_value FROM secret_reference")
    ).all()
    assert len(secrets) == 3
    assert {decrypt_value(row.encrypted_value) for row in secrets} == {
        "synthetic-model-secret",
        "synthetic-global-secret",
        "synthetic-webhook-secret",
    }
    assert all(not row.encrypted_value.startswith("synthetic") for row in secrets)
    row = legacy.execute(text("SELECT * FROM api_key")).one()
    assert row.key is None
    assert row.key_hash == crud_api_key.build_key_hash("synthetic-user-token")
    assert row.key_prefix == "synthetic-us"
    backfill.upgrade()
    assert (
        legacy.execute(text("SELECT count(*) FROM secret_reference")).scalar_one() == 3
    )
    contract.upgrade()
    # The previous release still maps these columns during a rolling upgrade.
    assert legacy_columns(legacy) == set(contract._COLUMNS)
    contract.downgrade()
    assert legacy_columns(legacy) == set(contract._COLUMNS)
    for table, column in contract._COLUMNS:
        assert (
            legacy.execute(
                text(f'SELECT count(*) FROM {table} WHERE "{column}" IS NOT NULL')
            ).scalar_one()
            == 0
        )
    backfill.downgrade()
    assert (
        legacy.execute(text("SELECT count(*) FROM secret_reference")).scalar_one() == 3
    )


def test_existing_reference_wins_over_stale_plaintext(legacy) -> None:
    account, model_id, _ = seed(legacy)
    secret_id = uuid.uuid4()
    legacy.execute(
        text(
            "INSERT INTO secret_reference (id, account_id, backend_type, encrypted_value) VALUES (:id, :account, 'local_encrypted', :encrypted)"
        ),
        {
            "id": secret_id,
            "account": account,
            "encrypted": encrypt_value("current-secret"),
        },
    )
    legacy.execute(
        text("UPDATE ai_model SET credentials_secret_id = :secret WHERE id = :id"),
        {"secret": secret_id, "id": model_id},
    )
    backfill.upgrade()
    row = legacy.execute(
        text("SELECT api_key, credentials_secret_id FROM ai_model WHERE id = :id"),
        {"id": model_id},
    ).one()
    assert row.api_key is None and row.credentials_secret_id == secret_id
    assert (
        decrypt_value(
            legacy.execute(
                text("SELECT encrypted_value FROM secret_reference WHERE id = :id"),
                {"id": secret_id},
            ).scalar_one()
        )
        == "current-secret"
    )


@pytest.mark.parametrize("failure", ["ownership", "ciphertext", "hash", "encrypt"])
def test_backfill_failure_rolls_back_without_losing_legacy_values(
    legacy, monkeypatch, failure
) -> None:
    account, model_id, _ = seed(legacy)
    if failure in {"ownership", "ciphertext"}:
        secret_id = uuid.uuid4()
        legacy.execute(
            text(
                "INSERT INTO secret_reference (id, account_id, backend_type, encrypted_value) VALUES (:id, :account, 'local_encrypted', :encrypted)"
            ),
            {
                "id": secret_id,
                "account": uuid.uuid4() if failure == "ownership" else account,
                "encrypted": "invalid"
                if failure == "ciphertext"
                else encrypt_value("current-secret"),
            },
        )
        legacy.execute(
            text("UPDATE ai_model SET credentials_secret_id = :secret WHERE id = :id"),
            {"secret": secret_id, "id": model_id},
        )
    elif failure == "hash":
        legacy.execute(text("UPDATE api_key SET key_hash = 'conflicting-fingerprint'"))
    else:

        def fail(value: str) -> str:
            raise ValueError("Encryption unavailable")

        monkeypatch.setattr("preloop.utils.encryption.encrypt_value", fail)
    with pytest.raises((ValueError, RuntimeError)), legacy.begin_nested():
        backfill.upgrade()
    assert (
        legacy.execute(
            text("SELECT api_key FROM ai_model WHERE id = :id"), {"id": model_id}
        ).scalar_one()
        == "synthetic-model-secret"
    )
    assert (
        legacy.execute(text("SELECT key FROM api_key")).scalar_one()
        == "synthetic-user-token"
    )


def test_plaintext_from_an_old_writer_is_kept_for_the_next_backfill(legacy) -> None:
    seed(legacy)
    backfill.upgrade()
    # A pod of the previous release keeps serving while the hook runs.
    key_id = uuid.uuid4()
    legacy.execute(
        text("INSERT INTO api_key VALUES (:id, 'old-writer-token', NULL, NULL)"),
        {"id": key_id},
    )
    contract.upgrade()
    by_id = text("SELECT key, key_hash FROM api_key WHERE id = :id")
    assert legacy.execute(by_id, {"id": key_id}).one().key == "old-writer-token"
    backfill.upgrade()
    row = legacy.execute(by_id, {"id": key_id}).one()
    assert row.key is None
    assert row.key_hash == crud_api_key.build_key_hash("old-writer-token")


def test_downgrade_restores_columns_removed_by_an_earlier_version(legacy) -> None:
    for table, column in contract._COLUMNS:
        legacy.execute(text(f'ALTER TABLE {table} DROP COLUMN "{column}"'))
    contract.downgrade()
    assert legacy_columns(legacy) == set(contract._COLUMNS)


def test_empty_legacy_values_do_not_create_credentials(legacy) -> None:
    seed(legacy)
    for table, column in contract._COLUMNS:
        legacy.execute(text(f'UPDATE {table} SET "{column}" = :value'), {"value": ""})
    # 20260307_add_hashed_api_key_support fingerprinted every non-NULL key,
    # including the empty string.
    legacy.execute(
        text("UPDATE api_key SET key_hash = :fingerprint, key_prefix = ''"),
        {"fingerprint": hashlib.sha256(b"").hexdigest()},
    )
    backfill.upgrade()
    assert (
        legacy.execute(text("SELECT count(*) FROM secret_reference")).scalar_one() == 0
    )
    assert legacy.execute(text("SELECT key_hash, key_prefix FROM api_key")).one() == (
        None,
        None,
    )
    contract.upgrade()


def test_backfill_rejects_default_development_encryption_key(
    legacy, monkeypatch
) -> None:
    from preloop.config import settings

    seed(legacy)
    monkeypatch.setattr(settings.security, "encryption_key", "")
    monkeypatch.setattr(
        settings.security,
        "secret_key",
        "development_secret_key_do_not_use_in_production",
    )
    with pytest.raises(RuntimeError, match="configured"):
        backfill.upgrade()
