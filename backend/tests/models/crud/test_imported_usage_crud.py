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
    assert row.endpoint == "/usage/import/cursor"
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


def test_out_of_window_reconciled_does_not_zero_in_window_estimates(
    db_session, create_account
):
    """A later billing export must not hide spend that fell in this window."""
    account = create_account()
    window_start = datetime(2026, 8, 1, tzinfo=timezone.utc).replace(tzinfo=None)
    window_end = datetime(2026, 8, 8, tzinfo=timezone.utc).replace(tzinfo=None)

    _log_imported(
        db_session,
        account_id=account.id,
        timestamp=datetime(2026, 8, 3, tzinfo=timezone.utc).replace(tzinfo=None),
        cost_usd=2.00,
        cost_basis="estimated",
        conversation_id="conv-1",
        import_fingerprint="fp-est-aug",
    )
    _log_imported(
        db_session,
        account_id=account.id,
        timestamp=datetime(2026, 8, 10, tzinfo=timezone.utc).replace(tzinfo=None),
        cost_usd=1.80,
        cost_basis="reconciled",
        conversation_id="conv-1",
        import_fingerprint="fp-rec-aug",
    )

    in_window = crud_api_usage.get_imported_usage_summary(
        db_session,
        account_id=str(account.id),
        start_date=window_start,
        end_date=window_end,
    )
    later = crud_api_usage.get_imported_usage_summary(
        db_session,
        account_id=str(account.id),
        start_date=window_end,
        end_date=datetime(2026, 8, 15, tzinfo=timezone.utc).replace(tzinfo=None),
    )

    assert abs(in_window["imported_cost"] - 2.00) < 1e-9
    assert abs(later["imported_cost"] - 1.80) < 1e-9


def test_by_conversation_groups_threads_and_splits_cost_bases(
    db_session, create_account
):
    """Conversations roll up with estimated and reconciled kept apart.

    The design-partner rail: billed (reconciled) amounts must stay visibly
    separate from size-proxy (estimated) amounts — the aggregation exposes
    them as two fields and never one combined number.
    """
    account = create_account()
    now = _utcnow_naive()

    # Parent thread: one estimated record, one reconciled record.
    _log_imported(
        db_session,
        account_id=account.id,
        conversation_id="conv-parent",
        cost_usd=2.00,
        cost_basis="estimated",
        total_tokens=1000,
        import_fingerprint="fp-conv-p1",
    )
    _log_imported(
        db_session,
        account_id=account.id,
        conversation_id="conv-parent",
        cost_usd=1.75,
        cost_basis="reconciled",
        prompt_tokens=None,
        completion_tokens=None,
        import_fingerprint="fp-conv-p2",
    )
    # Subagent worker billed on its own conversation, spawned from the parent.
    _log_imported(
        db_session,
        account_id=account.id,
        conversation_id="conv-worker",
        parent_conversation_id="conv-parent",
        cost_usd=0.40,
        cost_basis="estimated",
        total_tokens=300,
        import_fingerprint="fp-conv-w1",
    )
    # A CSV-style row without a conversation id never joins the rollup.
    _log_imported(
        db_session,
        account_id=account.id,
        cost_usd=9.99,
        import_fingerprint="fp-conv-none",
    )

    rows = crud_api_usage.get_imported_usage_by_conversation(
        db_session,
        account_id=str(account.id),
        start_date=now - timedelta(hours=1),
        end_date=now + timedelta(hours=1),
    )

    assert {row["conversation_id"] for row in rows} == {"conv-parent", "conv-worker"}
    by_id = {row["conversation_id"]: row for row in rows}

    parent = by_id["conv-parent"]
    assert parent["parent_conversation_id"] is None
    assert parent["event_count"] == 2
    # The reconciled row was logged token-free, so only the estimated
    # row's 1000 tokens count; token-free rows contribute NULL, not 0.
    assert parent["total_tokens"] == 1000
    assert abs(parent["estimated_cost"] - 2.00) < 1e-9
    assert abs(parent["reconciled_cost"] - 1.75) < 1e-9
    assert parent["last_event_at"] is not None
    assert parent["source"] == "cursor"

    worker = by_id["conv-worker"]
    assert worker["parent_conversation_id"] == "conv-parent"
    assert worker["event_count"] == 1
    assert abs(worker["estimated_cost"] - 0.40) < 1e-9
    # No reconciled record exists for the worker: null, never 0.0.
    assert worker["reconciled_cost"] is None


def test_by_conversation_nulls_stay_null(db_session, create_account):
    """Lifecycle-only conversations report nulls, never fabricated zeros."""
    account = create_account()
    now = _utcnow_naive()

    # A hook lifecycle event: no model, no tokens, no cost.
    row = crud_api_usage.log_imported_usage_event(
        db_session,
        account_id=str(account.id),
        timestamp=now,
        model_alias=None,
        source="cursor",
        conversation_id="conv-lifecycle",
        cost_basis="estimated",
        import_fingerprint="fp-conv-lc1",
    )
    assert row is not None

    rows = crud_api_usage.get_imported_usage_by_conversation(
        db_session,
        account_id=str(account.id),
        start_date=now - timedelta(hours=1),
        end_date=now + timedelta(hours=1),
    )

    assert len(rows) == 1
    only = rows[0]
    assert only["conversation_id"] == "conv-lifecycle"
    assert only["event_count"] == 1
    assert only["total_tokens"] is None
    assert only["estimated_cost"] is None
    assert only["reconciled_cost"] is None


def test_by_conversation_is_account_scoped_windowed_and_filterable(
    db_session, create_account
):
    """Rollup honors the account boundary, the window, and filters."""
    account = create_account()
    other = create_account()
    now = _utcnow_naive()

    _log_imported(
        db_session,
        account_id=account.id,
        conversation_id="conv-in",
        cost_usd=0.10,
        cost_basis="estimated",
        runtime_principal_id="ws-A",
        import_fingerprint="fp-scope-1",
    )
    _log_imported(
        db_session,
        account_id=account.id,
        conversation_id="conv-old",
        cost_usd=0.20,
        cost_basis="estimated",
        timestamp=now - timedelta(days=40),
        import_fingerprint="fp-scope-2",
    )
    _log_imported(
        db_session,
        account_id=account.id,
        conversation_id="conv-windsurf",
        source="windsurf",
        cost_usd=0.30,
        cost_basis="estimated",
        runtime_principal_id="ws-B",
        import_fingerprint="fp-scope-3",
    )
    _log_imported(
        db_session,
        account_id=other.id,
        conversation_id="conv-other-account",
        cost_usd=0.40,
        cost_basis="estimated",
        import_fingerprint="fp-scope-4",
    )

    window = dict(
        account_id=str(account.id),
        start_date=now - timedelta(days=30),
        end_date=now + timedelta(hours=1),
    )

    rows = crud_api_usage.get_imported_usage_by_conversation(db_session, **window)
    assert {row["conversation_id"] for row in rows} == {"conv-in", "conv-windsurf"}

    by_source = crud_api_usage.get_imported_usage_by_conversation(
        db_session, source="windsurf", **window
    )
    assert [row["conversation_id"] for row in by_source] == ["conv-windsurf"]

    by_principal = crud_api_usage.get_imported_usage_by_conversation(
        db_session, runtime_principal_id="ws-A", **window
    )
    assert [row["conversation_id"] for row in by_principal] == ["conv-in"]
