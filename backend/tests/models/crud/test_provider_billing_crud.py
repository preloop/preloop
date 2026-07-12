"""Tests for provider billing connection and snapshot CRUD helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from preloop.models.crud import (
    crud_provider_billing_connection,
    crud_provider_billing_snapshot,
)
from preloop.models.crud.provider_billing import snapshot_dedup_key
from preloop.models.models.provider_billing import ProviderBillingConnection
from preloop.models.models.secret_reference import SecretReference


def _create_secret(db_session, account_id) -> SecretReference:
    now = datetime.now(UTC).replace(tzinfo=None)
    secret = SecretReference(
        account_id=account_id,
        name="openai-admin",
        backend_type="local_encrypted",
        secret_kind="provider_billing_api_key",
        encrypted_value="ciphertext",
        status="active",
        meta_data={},
        created_at=now,
        updated_at=now,
    )
    db_session.add(secret)
    db_session.flush()
    return secret


def _create_connection(
    db_session, *, account_id, provider: str = "openai", active: bool = True
) -> ProviderBillingConnection:
    secret = _create_secret(db_session, account_id)
    now = datetime.now(UTC).replace(tzinfo=None)
    connection = ProviderBillingConnection(
        id=uuid4(),
        account_id=account_id,
        provider=provider,
        secret_reference_id=secret.id,
        is_active=active,
        created_at=now,
        updated_at=now,
    )
    db_session.add(connection)
    db_session.commit()
    db_session.refresh(connection)
    return connection


def test_snapshot_dedup_key_is_stable_for_same_bucket() -> None:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    first = snapshot_dedup_key(
        provider="openai",
        granularity="1d",
        bucket_start=start,
        model="gpt-4.1",
        line_item=None,
        provider_api_key_id="key-1",
        project_or_workspace_id=None,
        service_tier="default",
    )
    second = snapshot_dedup_key(
        provider="openai",
        granularity="1d",
        bucket_start=start,
        model="gpt-4.1",
        line_item=None,
        provider_api_key_id="key-1",
        project_or_workspace_id=None,
        service_tier="default",
    )
    different = snapshot_dedup_key(
        provider="openai",
        granularity="1d",
        bucket_start=start,
        model="gpt-4.1-mini",
        line_item=None,
        provider_api_key_id="key-1",
        project_or_workspace_id=None,
        service_tier="default",
    )
    assert first == second
    assert first != different
    assert len(first) == 64


def test_list_and_get_connections_scoped_to_account(db_session, create_account) -> None:
    account = create_account()
    other = create_account()
    active = _create_connection(db_session, account_id=account.id, provider="openai")
    inactive = _create_connection(
        db_session, account_id=account.id, provider="anthropic", active=False
    )
    _create_connection(db_session, account_id=other.id, provider="openai")

    all_for_account = crud_provider_billing_connection.list_for_account(
        db_session, account_id=account.id
    )
    active_only = crud_provider_billing_connection.list_for_account(
        db_session, account_id=account.id, active_only=True
    )
    fetched = crud_provider_billing_connection.get_for_account(
        db_session, account_id=account.id, connection_id=active.id
    )
    cross_account = crud_provider_billing_connection.get_for_account(
        db_session, account_id=other.id, connection_id=active.id
    )

    assert {row.provider for row in all_for_account} == {"anthropic", "openai"}
    assert [row.id for row in active_only] == [active.id]
    assert inactive.is_active is False
    assert fetched is not None
    assert fetched.id == active.id
    assert cross_account is None


def test_list_active_across_accounts(db_session, create_account) -> None:
    account_a = create_account()
    account_b = create_account()
    active_a = _create_connection(
        db_session, account_id=account_a.id, provider="openai"
    )
    _create_connection(
        db_session, account_id=account_a.id, provider="anthropic", active=False
    )
    active_b = _create_connection(
        db_session, account_id=account_b.id, provider="openai"
    )

    active = crud_provider_billing_connection.list_active(db_session)
    active_ids = {row.id for row in active}
    assert active_a.id in active_ids
    assert active_b.id in active_ids


def test_mark_synced_success_and_error(db_session, create_account) -> None:
    account = create_account()
    connection = _create_connection(db_session, account_id=account.id)
    synced_at = datetime.now(UTC)

    ok = crud_provider_billing_connection.mark_synced(
        db_session, connection=connection, synced_at=synced_at
    )
    assert ok.last_synced_at is not None
    assert ok.last_error is None

    failed = crud_provider_billing_connection.mark_synced(
        db_session,
        connection=connection,
        synced_at=synced_at + timedelta(minutes=1),
        error="provider 429",
    )
    assert failed.last_error == "provider 429"
    # Failed sync must not advance last_synced_at.
    assert failed.last_synced_at == ok.last_synced_at


def test_upsert_snapshots_is_idempotent_and_updates_cost(
    db_session, create_account
) -> None:
    account = create_account()
    start = datetime(2026, 7, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    fetched_at = datetime.now(UTC)
    row = {
        "provider": "openai",
        "granularity": "1d",
        "bucket_start": start,
        "bucket_end": end,
        "model": "gpt-4.1",
        "cost_amount": 1.25,
        "currency": "usd",
        "uncached_input_tokens": 100,
        "cached_input_tokens": 10,
        "cache_creation_tokens": 0,
        "output_tokens": 20,
        "fetched_at": fetched_at,
        "raw": {"n": 1},
    }

    written = crud_provider_billing_snapshot.upsert_snapshots(
        db_session, account_id=account.id, rows=[row]
    )
    assert written == 1

    row["cost_amount"] = 2.5
    row["output_tokens"] = 40
    row["fetched_at"] = fetched_at + timedelta(minutes=5)
    written_again = crud_provider_billing_snapshot.upsert_snapshots(
        db_session, account_id=account.id, rows=[row]
    )
    assert written_again == 1

    aggregates = crud_provider_billing_snapshot.aggregate_actuals_by_provider_day(
        db_session,
        account_id=account.id,
        start=start,
        end=end + timedelta(days=1),
    )
    assert len(aggregates) == 1
    assert aggregates[0]["provider"] == "openai"
    assert aggregates[0]["cost_amount"] == 2.5
    assert aggregates[0]["output_tokens"] == 40


def test_aggregate_actuals_filters_provider_and_window(
    db_session, create_account
) -> None:
    account = create_account()
    day1 = datetime(2026, 7, 1, tzinfo=UTC)
    day2 = datetime(2026, 7, 2, tzinfo=UTC)
    fetched_at = datetime.now(UTC)

    rows = [
        {
            "provider": "openai",
            "bucket_start": day1,
            "bucket_end": day1 + timedelta(days=1),
            "cost_amount": 1.0,
            "uncached_input_tokens": 10,
            "cached_input_tokens": 0,
            "cache_creation_tokens": 0,
            "output_tokens": 5,
            "fetched_at": fetched_at,
        },
        {
            "provider": "openai",
            "bucket_start": day2,
            "bucket_end": day2 + timedelta(days=1),
            "cost_amount": 3.0,
            "uncached_input_tokens": 30,
            "cached_input_tokens": 0,
            "cache_creation_tokens": 0,
            "output_tokens": 15,
            "fetched_at": fetched_at,
        },
        {
            "provider": "anthropic",
            "bucket_start": day1,
            "bucket_end": day1 + timedelta(days=1),
            "cost_amount": 9.0,
            "uncached_input_tokens": 90,
            "cached_input_tokens": 0,
            "cache_creation_tokens": 0,
            "output_tokens": 45,
            "fetched_at": fetched_at,
        },
    ]
    crud_provider_billing_snapshot.upsert_snapshots(
        db_session, account_id=account.id, rows=rows
    )

    openai_only = crud_provider_billing_snapshot.aggregate_actuals_by_provider_day(
        db_session,
        account_id=account.id,
        start=day1,
        end=day2 + timedelta(days=1),
        provider="openai",
    )
    assert len(openai_only) == 2
    assert all(row["provider"] == "openai" for row in openai_only)
    assert sum(row["cost_amount"] for row in openai_only) == 4.0

    day1_only = crud_provider_billing_snapshot.aggregate_actuals_by_provider_day(
        db_session,
        account_id=account.id,
        start=day1,
        end=day2,
    )
    assert {row["provider"] for row in day1_only} == {"anthropic", "openai"}
    assert sum(row["cost_amount"] for row in day1_only) == 10.0
