"""The session search backfill: bounds, watermark, completion and failures."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from preloop.config import settings
from preloop.models.crud import (
    crud_account,
    crud_api_usage,
    crud_runtime_session,
    crud_runtime_session_activity,
    crud_session_search_backfill_state,
    crud_session_search_document,
)
from preloop.models.models.session_search_document import (
    SOURCE_KIND_GATEWAY_INTERACTION,
    SOURCE_KIND_TOOL_CALL,
)
from preloop.services import session_search_backfill as backfill
from preloop.services.gateway_usage_search import GatewayUsageSearchService

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def enabled_backfill(monkeypatch):
    """Tests exercise the job itself; the deployment default is off."""
    monkeypatch.setattr(
        settings, "session_search_backfill_enabled", True, raising=False
    )
    monkeypatch.setattr(settings, "session_search_index_enabled", True, raising=False)
    monkeypatch.setattr(
        settings, "session_search_backfill_max_rows_per_pass", 1000, raising=False
    )
    monkeypatch.setattr(
        settings, "session_search_backfill_max_rows_per_account", 1000, raising=False
    )
    monkeypatch.setattr(
        settings, "session_search_backfill_max_seconds", 60, raising=False
    )
    monkeypatch.setattr(
        settings, "session_search_backfill_max_age_days", 183, raising=False
    )
    monkeypatch.setattr(settings, "model_gateway_capture_content", True, raising=False)


@contextmanager
def _history_written_before_search_existed():
    """Write fixture rows with on-write indexing off.

    The backfill exists for sessions that were written before the corpus did,
    so the fixtures have to look like that: source rows on disk, no chunks.
    """
    previous = settings.session_search_index_enabled
    settings.session_search_index_enabled = False
    try:
        yield
    finally:
        settings.session_search_index_enabled = previous


def _session(db_session, account_id, *, source_id: str, started_at: datetime):
    return crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account_id,
        session_source_type="custom",
        session_source_id=source_id,
        session_reference=source_id,
        runtime_principal_type="agent",
        runtime_principal_id="agent-1",
        runtime_principal_name="Test Agent",
        started_at=started_at,
        last_activity_at=started_at,
    )


def _gateway_interaction(db_session, *, account_id, user_id, session, timestamp):
    """One metered call with the gateway corpus row it already has."""
    usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(user_id),
        account_id=str(account_id),
        runtime_session_id=str(session.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        meta_data={"requested_model": "openai/gpt-5"},
    )
    usage.timestamp = timestamp
    db_session.flush()
    GatewayUsageSearchService(db_session).index_interaction(
        usage=usage,
        request_payload={"input": "how do I rotate a signing key"},
        response_payload={"output_text": "rotate it from the console"},
    )
    return usage


def _tool_call(db_session, *, account_id, session, timestamp, tool_name="read_file"):
    return crud_runtime_session_activity.log_tool_call(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        server_name="files",
        tool_name=tool_name,
        status="success",
        summary="read the incident notes",
        timestamp=timestamp,
        commit=False,
    )


def _history(db_session, test_user, *, sessions: int = 3):
    """Build `sessions` sessions, one gateway call each, newest last."""
    built = []
    with _history_written_before_search_existed():
        for index in range(sessions):
            started_at = NOW - timedelta(days=index + 1)
            session = _session(
                db_session,
                test_user.account_id,
                source_id=f"session-{index}",
                started_at=started_at,
            )
            _gateway_interaction(
                db_session,
                account_id=test_user.account_id,
                user_id=test_user.id,
                session=session,
                timestamp=started_at,
            )
            built.append(session)
    # Committed on purpose: the pass rolls its session back when an account
    # fails, and fixture rows that only live in the caller's transaction
    # would vanish with it.
    db_session.commit()
    return built


def _corpus_count(db_session, account_id) -> int:
    return len(
        crud_session_search_document.search_account_chunks(
            db_session, account_id=account_id, limit=1000
        )
    )


def _utc(value: datetime) -> datetime:
    """Normalise, because runtime_session.started_at is a naive column."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _state(db_session, account_id):
    return crud_session_search_backfill_state.get_for_account(
        db_session, account_id=account_id
    )


def test_a_pass_stops_at_its_row_budget_mid_account(db_session, test_user):
    """The budget is a hard stop, and the account is left half walked."""
    _history(db_session, test_user, sessions=3)
    settings.session_search_backfill_max_rows_per_pass = 2

    result = backfill.run_session_search_backfill(db_session, now=NOW)

    assert result.rows_written == 2
    assert _corpus_count(db_session, test_user.account_id) == 2
    assert result.budget_exhausted is True
    state = _state(db_session, test_user.account_id)
    assert state.completed_at is None
    assert state.rows_written == 2


def test_the_account_row_budget_bounds_one_account(db_session, test_user):
    """One large account cannot consume the whole pass."""
    _history(db_session, test_user, sessions=3)
    settings.session_search_backfill_max_rows_per_pass = 100
    settings.session_search_backfill_max_rows_per_account = 1

    result = backfill.run_session_search_backfill(db_session, now=NOW)

    assert result.rows_written == 1
    assert _corpus_count(db_session, test_user.account_id) == 1


def test_re_running_a_pass_writes_no_new_rows(db_session, test_user):
    """Idempotent by content hash: the second walk is a read."""
    _history(db_session, test_user, sessions=2)

    first = backfill.run_session_search_backfill(db_session, now=NOW)
    after_first = _corpus_count(db_session, test_user.account_id)
    chunk_ids = {
        str(row.id)
        for row in crud_session_search_document.search_account_chunks(
            db_session, account_id=test_user.account_id, limit=1000
        )
    }

    # The account finished, so the next pass skips it entirely.
    skipped = backfill.run_session_search_backfill(db_session, now=NOW)
    assert skipped.rows_written == 0
    assert skipped.accounts == 0

    # And a forced re-walk (watermark cleared) still writes nothing new.
    state = _state(db_session, test_user.account_id)
    state.completed_at = None
    state.cursor_started_at = None
    state.cursor_session_id = None
    db_session.flush()
    replayed = backfill.run_session_search_backfill(db_session, now=NOW)

    assert first.rows_written == after_first > 0
    assert replayed.rows_written == 0
    assert _corpus_count(db_session, test_user.account_id) == after_first
    assert {
        str(row.id)
        for row in crud_session_search_document.search_account_chunks(
            db_session, account_id=test_user.account_id, limit=1000
        )
    } == chunk_ids


def test_a_restarted_sweeper_resumes_from_the_watermark(db_session, test_user):
    """The watermark is persisted, and the next pass starts below it."""
    sessions = _history(db_session, test_user, sessions=3)
    settings.session_search_backfill_max_rows_per_pass = 1

    backfill.run_session_search_backfill(db_session, now=NOW)
    first_watermark = _state(db_session, test_user.account_id).cursor_started_at
    walked_from = []
    original = crud_runtime_session.list_for_search_backfill

    def _recording(db, **kwargs):
        walked_from.append(kwargs.get("before_started_at"))
        return original(db, **kwargs)

    crud_runtime_session.list_for_search_backfill = _recording
    try:
        backfill.run_session_search_backfill(db_session, now=NOW)
    finally:
        crud_runtime_session.list_for_search_backfill = original

    second_watermark = _state(db_session, test_user.account_id).cursor_started_at
    # The newest session is walked first, so the watermark is its start.
    assert _utc(first_watermark) == _utc(sessions[0].started_at)
    assert _utc(walked_from[0]) == _utc(sessions[0].started_at)
    assert _utc(second_watermark) == _utc(sessions[1].started_at)
    assert _corpus_count(db_session, test_user.account_id) == 2


def test_indexed_through_moves_backwards_as_the_backfill_progresses(
    db_session, test_user
):
    """The value a search response carries reaches further back each pass."""
    _history(db_session, test_user, sessions=3)
    settings.session_search_backfill_max_rows_per_pass = 1

    before_any = backfill.indexed_through_for_account(
        db_session, account_id=test_user.account_id
    )
    assert before_any.indexed_through is None
    assert before_any.state == backfill.BACKFILL_STATE_NOT_STARTED

    backfill.run_session_search_backfill(db_session, now=NOW)
    after_first = backfill.indexed_through_for_account(
        db_session, account_id=test_user.account_id
    )
    backfill.run_session_search_backfill(db_session, now=NOW)
    after_second = backfill.indexed_through_for_account(
        db_session, account_id=test_user.account_id
    )

    assert after_second.indexed_through < after_first.indexed_through
    assert after_first.state == backfill.BACKFILL_STATE_IN_PROGRESS
    assert after_first.complete is False
    # The shape the search endpoint returns.
    assert set(after_first.as_dict()) == {
        "indexed_through",
        "backfill_complete",
        "backfill_state",
    }

    settings.session_search_backfill_max_rows_per_pass = 1000
    backfill.run_session_search_backfill(db_session, now=NOW)
    finished = backfill.indexed_through_for_account(
        db_session, account_id=test_user.account_id
    )

    assert finished.complete is True
    assert finished.state == backfill.BACKFILL_STATE_COMPLETE
    assert finished.indexed_through < after_second.indexed_through


def test_a_completed_account_is_skipped_without_scanning_its_sessions(
    db_session, test_user
):
    """A finished account costs one indexed read, not a session walk."""
    _history(db_session, test_user, sessions=1)
    backfill.run_session_search_backfill(db_session, now=NOW)
    assert _state(db_session, test_user.account_id).completed_at is not None

    original = crud_runtime_session.list_for_search_backfill

    def _forbidden(db, **kwargs):
        raise AssertionError("a completed account must not be scanned again")

    crud_runtime_session.list_for_search_backfill = _forbidden
    try:
        result = backfill.run_session_search_backfill(db_session, now=NOW)
    finally:
        crud_runtime_session.list_for_search_backfill = original

    assert result.accounts == 0
    assert result.rows_written == 0
    assert (
        test_user.account_id
        not in crud_session_search_backfill_state.pending_account_ids(db_session)
    )


def test_one_failing_account_does_not_stop_the_pass(db_session, test_user):
    """A broken account is logged and skipped; the others still get indexed."""
    sessions = _history(db_session, test_user, sessions=1)
    other = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )
    with _history_written_before_search_existed():
        other_session = _session(
            db_session,
            other.id,
            source_id="other-session",
            started_at=NOW - timedelta(days=1),
        )
        _tool_call(
            db_session,
            account_id=other.id,
            session=other_session,
            timestamp=NOW - timedelta(days=1),
        )
    db_session.commit()

    original = crud_runtime_session.list_for_search_backfill

    def _fails_for_the_first_account(db, **kwargs):
        if str(kwargs.get("account_id")) == str(test_user.account_id):
            raise RuntimeError("session scan blew up")
        return original(db, **kwargs)

    crud_runtime_session.list_for_search_backfill = _fails_for_the_first_account
    try:
        result = backfill.run_session_search_backfill(db_session, now=NOW)
    finally:
        crud_runtime_session.list_for_search_backfill = original

    assert result.accounts_failed == 1
    assert result.rows_written > 0
    assert _corpus_count(db_session, other.id) > 0
    assert _corpus_count(db_session, test_user.account_id) == 0
    failed_state = _state(db_session, test_user.account_id)
    assert failed_state is not None
    assert "session scan blew up" in (failed_state.last_error or "")
    assert failed_state.completed_at is None
    assert sessions  # the failing account still has its history on disk


def test_tool_calls_and_gateway_interactions_are_both_backfilled(db_session, test_user):
    """Both source kinds named in the issue land as chunks."""
    with _history_written_before_search_existed():
        session = _session(
            db_session,
            test_user.account_id,
            source_id="session-mixed",
            started_at=NOW - timedelta(days=2),
        )
        _gateway_interaction(
            db_session,
            account_id=test_user.account_id,
            user_id=test_user.id,
            session=session,
            timestamp=NOW - timedelta(days=2),
        )
        _tool_call(
            db_session,
            account_id=test_user.account_id,
            session=session,
            timestamp=NOW - timedelta(days=2),
        )
    db_session.commit()

    result = backfill.run_session_search_backfill(db_session, now=NOW)

    kinds = {
        row.source_kind
        for row in crud_session_search_document.search_account_chunks(
            db_session, account_id=test_user.account_id, limit=1000
        )
    }
    assert kinds == {SOURCE_KIND_GATEWAY_INTERACTION, SOURCE_KIND_TOOL_CALL}
    assert result.sources_indexed == 2


def test_history_older_than_the_age_bound_is_left_alone(db_session, test_user):
    """The walk stops at the retention horizon instead of running forever."""
    with _history_written_before_search_existed():
        old_session = _session(
            db_session,
            test_user.account_id,
            source_id="session-ancient",
            started_at=NOW - timedelta(days=400),
        )
        _gateway_interaction(
            db_session,
            account_id=test_user.account_id,
            user_id=test_user.id,
            session=old_session,
            timestamp=NOW - timedelta(days=400),
        )
    db_session.commit()

    result = backfill.run_session_search_backfill(db_session, now=NOW)

    assert result.rows_written == 0
    assert _corpus_count(db_session, test_user.account_id) == 0
    assert _state(db_session, test_user.account_id).completed_at is not None


def test_a_disabled_backfill_writes_nothing(db_session, test_user):
    """The setting is the kill switch for the pass as well as the sweeper."""
    _history(db_session, test_user, sessions=1)
    settings.session_search_backfill_enabled = False

    result = backfill.run_session_search_backfill(db_session, now=NOW)

    assert result.skipped_reason == "disabled"
    assert _corpus_count(db_session, test_user.account_id) == 0
    assert _state(db_session, test_user.account_id) is None


def test_a_disabled_corpus_stops_the_backfill(db_session, test_user):
    """With the corpus switched off there is nothing to write to."""
    _history(db_session, test_user, sessions=1)
    settings.session_search_index_enabled = False

    result = backfill.run_session_search_backfill(db_session, now=NOW)

    assert result.skipped_reason == "corpus_disabled"
    assert _corpus_count(db_session, test_user.account_id) == 0


@pytest.mark.asyncio
async def test_the_sweeper_does_not_start_when_disabled(monkeypatch):
    """Registration is the assertion: no task, no loop."""
    monkeypatch.setattr(
        settings, "session_search_backfill_enabled", False, raising=False
    )
    backfill.reset_session_search_backfill_sweeper()
    sweeper = backfill.get_session_search_backfill_sweeper()

    await sweeper.start()

    assert sweeper.running is False
    assert sweeper._task is None
    backfill.reset_session_search_backfill_sweeper()


@pytest.mark.asyncio
async def test_the_sweeper_starts_when_enabled(monkeypatch):
    """The same registration with the setting on does create the loop."""
    monkeypatch.setattr(
        settings, "session_search_backfill_enabled", True, raising=False
    )
    monkeypatch.setattr(backfill, "background_passes_allowed", lambda: True)
    backfill.reset_session_search_backfill_sweeper()
    sweeper = backfill.get_session_search_backfill_sweeper()

    await sweeper.start()
    try:
        assert sweeper.running is True
        assert sweeper._task is not None
    finally:
        await sweeper.stop()
        backfill.reset_session_search_backfill_sweeper()

    assert sweeper.running is False


def test_a_locked_account_is_not_recorded_as_failed(db_session, test_user, monkeypatch):
    """Another replica holding the walk is a skip, not a last_error stamp."""
    monkeypatch.setattr(
        backfill.crud_session_search_backfill_state,
        "lock_for_account",
        lambda db, *, account_id: None,
    )

    result = backfill.backfill_account(
        db_session,
        account_id=test_user.account_id,
        row_budget=10,
        now=NOW,
        commit=False,
    )
    summary = backfill.run_session_search_backfill(
        db_session, now=NOW, account_ids=[test_user.account_id]
    )

    assert result is None
    assert summary.accounts_failed == 0
    assert summary.accounts == 0
    assert _state(db_session, test_user.account_id) is None
