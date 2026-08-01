"""Real-DB tests for imported-usage ledger writes (issue #123).

Pins the cost-ledger contract for spend observed outside the model gateway:

  1. Imported events land as ``action_type='imported_usage'`` rows labeled
     ``usage_source='imported'`` (and ``cost_source='imported'`` when priced).
  2. Re-importing the same event (same fingerprint) never double-counts.
  3. Imported aggregations are account-scoped and window/principal/source
     filterable.
  4. Gateway aggregations are structurally blind to imported rows, so
     budgets and gateway spend can never silently mix in imported spend.
"""

from datetime import datetime, timedelta, timezone

from preloop.models.crud import crud_api_usage


def _utcnow_naive() -> datetime:
    """Ledger timestamps are stored naive-UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _log_imported(db, *, account_id, **overrides):
    """Log one imported usage event with sensible defaults."""
    params = dict(
        account_id=str(account_id),
        timestamp=_utcnow_naive(),
        model_alias="composer",
        source="cursor",
        prompt_tokens=100,
        completion_tokens=50,
        cost_usd=0.25,
    )
    params.update(overrides)
    return crud_api_usage.log_imported_usage_event(db, **params)


def test_log_imported_usage_event_writes_labeled_ledger_row(
    db_session, create_account, create_user
):
    """The row must carry the imported markers and source metadata."""
    account = create_account()
    user = create_user(account=account)

    row = _log_imported(
        db_session,
        account_id=account.id,
        user_id=str(user.id),
        runtime_principal_type="cursor",
        runtime_principal_id="cursor-ws-1",
        runtime_principal_name="Cursor (laptop)",
        import_fingerprint="fp-write-1",
        meta_data={"source_kind": "Usage-based"},
    )

    assert row is not None
    assert row.action_type == "imported_usage"
    assert row.usage_source == "imported"
    assert row.cost_source == "imported"
    assert row.currency == "USD"
    assert row.estimated_cost == 0.25
    assert row.total_tokens == 150  # derived from prompt + completion
    assert row.provider_name == "cursor"
    assert row.runtime_principal_id == "cursor-ws-1"
    assert row.meta_data["import_source"] == "cursor"
    assert row.meta_data["import_fingerprint"] == "fp-write-1"
    assert row.meta_data["source_kind"] == "Usage-based"


def test_tokens_only_event_has_no_cost_markers(db_session, create_account):
    """Unpriced (e.g. 'Included') events carry tokens but no cost labels."""
    account = create_account()

    row = _log_imported(db_session, account_id=account.id, cost_usd=None)

    assert row is not None
    assert row.estimated_cost is None
    assert row.cost_source is None
    assert row.currency is None
    assert row.usage_source == "imported"


def test_duplicate_fingerprint_is_skipped(db_session, create_account):
    """Replaying an event with a known fingerprint must return None."""
    account = create_account()

    first = _log_imported(
        db_session, account_id=account.id, import_fingerprint="fp-dupe"
    )
    second = _log_imported(
        db_session, account_id=account.id, import_fingerprint="fp-dupe"
    )

    assert first is not None
    assert second is None

    summary = crud_api_usage.get_imported_usage_summary(
        db_session,
        account_id=str(account.id),
        start_date=_utcnow_naive() - timedelta(hours=1),
        end_date=_utcnow_naive() + timedelta(hours=1),
    )
    assert summary["event_count"] == 1


def test_db_index_closes_dedupe_race(db_session, create_account, monkeypatch):
    """The unique partial index catches duplicates the fast-path check missed.

    Simulates the TOCTOU interleaving: a concurrent import commits the same
    fingerprint after this transaction's existence check ran. Disabling the
    fast-path check forces the insert to hit the DB index, which must skip
    the row (return None) instead of double-recording spend or breaking the
    session/batch.
    """
    account = create_account()

    first = _log_imported(
        db_session, account_id=account.id, import_fingerprint="fp-race"
    )
    assert first is not None

    monkeypatch.setattr(
        crud_api_usage,
        "_imported_fingerprint_exists",
        lambda *args, **kwargs: False,
    )
    second = _log_imported(
        db_session, account_id=account.id, import_fingerprint="fp-race"
    )

    assert second is None
    # The session survives the caught unique violation: further writes and
    # reads in the same batch still work.
    third = _log_imported(
        db_session, account_id=account.id, import_fingerprint="fp-race-other"
    )
    assert third is not None

    summary = crud_api_usage.get_imported_usage_summary(
        db_session,
        account_id=str(account.id),
        start_date=_utcnow_naive() - timedelta(hours=1),
        end_date=_utcnow_naive() + timedelta(hours=1),
    )
    assert summary["event_count"] == 2


def test_fingerprintless_rows_never_conflict(db_session, create_account):
    """Rows without a fingerprint are exempt from the unique index (NULLs)."""
    account = create_account()

    first = _log_imported(db_session, account_id=account.id)
    second = _log_imported(db_session, account_id=account.id)

    assert first is not None
    assert second is not None


def test_same_fingerprint_in_other_account_still_lands(db_session, create_account):
    """Dedupe is account-scoped; another account may carry the same key."""
    account_a = create_account()
    account_b = create_account()

    row_a = _log_imported(
        db_session, account_id=account_a.id, import_fingerprint="fp-shared"
    )
    row_b = _log_imported(
        db_session, account_id=account_b.id, import_fingerprint="fp-shared"
    )

    assert row_a is not None
    assert row_b is not None


def test_summary_is_account_scoped_and_windowed(db_session, create_account):
    """Totals cover only the account's rows inside the requested window."""
    account = create_account()
    other = create_account()
    now = _utcnow_naive()

    _log_imported(db_session, account_id=account.id, cost_usd=0.30)
    _log_imported(
        db_session,
        account_id=account.id,
        cost_usd=0.70,
        timestamp=now - timedelta(days=40),  # outside window
    )
    _log_imported(db_session, account_id=other.id, cost_usd=9.99)

    summary = crud_api_usage.get_imported_usage_summary(
        db_session,
        account_id=str(account.id),
        start_date=now - timedelta(days=30),
        end_date=now + timedelta(hours=1),
    )

    assert summary["event_count"] == 1
    assert summary["imported_cost"] == 0.30
    assert summary["total_tokens"] == 150


def test_summary_filters_by_principal_and_source(db_session, create_account):
    """runtime_principal_id and source restrict the aggregation."""
    account = create_account()
    now = _utcnow_naive()

    _log_imported(
        db_session,
        account_id=account.id,
        runtime_principal_id="ws-A",
        source="cursor",
        cost_usd=0.10,
    )
    _log_imported(
        db_session,
        account_id=account.id,
        runtime_principal_id="ws-B",
        source="windsurf",
        cost_usd=0.20,
    )

    window = dict(
        account_id=str(account.id),
        start_date=now - timedelta(hours=1),
        end_date=now + timedelta(hours=1),
    )

    by_principal = crud_api_usage.get_imported_usage_summary(
        db_session, runtime_principal_id="ws-A", **window
    )
    assert by_principal["event_count"] == 1
    assert by_principal["imported_cost"] == 0.10

    by_source = crud_api_usage.get_imported_usage_summary(
        db_session, source="windsurf", **window
    )
    assert by_source["event_count"] == 1
    assert by_source["imported_cost"] == 0.20


def test_by_model_grouping(db_session, create_account):
    """Grouped rows report per-model counts, tokens, and cost."""
    account = create_account()
    now = _utcnow_naive()

    _log_imported(
        db_session, account_id=account.id, model_alias="composer", cost_usd=0.10
    )
    _log_imported(
        db_session,
        account_id=account.id,
        model_alias="composer",
        cost_usd=0.15,
        import_fingerprint="fp-model-2",
    )
    _log_imported(
        db_session,
        account_id=account.id,
        model_alias="claude-4.5-sonnet",
        cost_usd=0.50,
    )

    rows = crud_api_usage.get_imported_usage_by_model(
        db_session,
        account_id=str(account.id),
        start_date=now - timedelta(hours=1),
        end_date=now + timedelta(hours=1),
    )

    assert [row["model_alias"] for row in rows] == [
        "composer",
        "claude-4.5-sonnet",
    ]
    composer = rows[0]
    assert composer["request_count"] == 2
    assert composer["imported_cost"] == 0.25
    assert composer["source"] == "cursor"
    assert composer["last_event_at"] is not None


def test_gateway_aggregations_are_blind_to_imported_rows(
    db_session, create_account, create_user
):
    """Imported spend must never surface in gateway-metered aggregations."""
    account = create_account()
    user = create_user(account=account)
    now = _utcnow_naive()

    _log_imported(
        db_session,
        account_id=account.id,
        user_id=str(user.id),
        cost_usd=123.45,
        total_tokens=99999,
    )

    gateway = crud_api_usage.get_gateway_usage_summary(
        db_session,
        account_id=str(account.id),
        start_date=now - timedelta(hours=1),
        end_date=now + timedelta(hours=1),
    )

    assert gateway["request_count"] == 0
    assert gateway["estimated_cost"] == 0.0
    assert gateway["total_tokens"] == 0
