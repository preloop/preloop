"""Tests for runtime session CRUD helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from uuid import uuid4

from preloop.models.crud.runtime_session import (
    _latest_gateway_usage_for_sessions,
    _summary_columns_cache,
    crud_runtime_session,
)
from preloop.models.models.ai_model import AIModel
from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.runtime_session import RuntimeSession


def test_get_latest_by_principal_scopes_to_account(db_session, create_account) -> None:
    """Principal lookup must not return another account's session."""
    principal_type = "openclaw"
    principal_id = "octavia-shared"

    other_account = create_account()
    current_account = create_account()

    other_session = RuntimeSession(
        id=uuid4(),
        account_id=other_account.id,
        session_source_type=principal_type,
        session_source_id=principal_id,
        session_reference="other",
        runtime_principal_type=principal_type,
        runtime_principal_id=principal_id,
        started_at=datetime.now(UTC),
        last_activity_at=datetime.now(UTC),
    )
    current_session = RuntimeSession(
        id=uuid4(),
        account_id=current_account.id,
        session_source_type=principal_type,
        session_source_id=principal_id,
        session_reference="current",
        runtime_principal_type=principal_type,
        runtime_principal_id=principal_id,
        started_at=datetime.now(UTC),
        last_activity_at=datetime.now(UTC),
    )
    db_session.add_all([other_session, current_session])
    db_session.commit()

    latest = crud_runtime_session.get_latest_by_principal(
        db_session,
        account_id=str(current_account.id),
        principal_type=principal_type,
        principal_id=principal_id,
    )

    assert latest is not None
    assert str(latest.id) == str(current_session.id)
    assert str(latest.account_id) == str(current_account.id)


def test_latest_gateway_usage_for_sessions_returns_latest_per_session(
    db_session,
    create_account,
) -> None:
    """Batch latest-gateway lookup should return one row per session."""
    account = create_account()
    account_id = str(account.id)
    session_a = uuid4()
    session_b = uuid4()
    now = datetime.now(UTC)

    db_session.add_all(
        [
            RuntimeSession(
                id=session_a,
                account_id=account_id,
                session_source_type="openclaw",
                session_source_id="session-a",
                session_reference="session-a",
                runtime_principal_type="openclaw",
                runtime_principal_id="session-a",
                started_at=now,
                last_activity_at=now,
            ),
            RuntimeSession(
                id=session_b,
                account_id=account_id,
                session_source_type="openclaw",
                session_source_id="session-b",
                session_reference="session-b",
                runtime_principal_type="openclaw",
                runtime_principal_id="session-b",
                started_at=now,
                last_activity_at=now,
            ),
        ]
    )
    db_session.flush()

    db_session.add_all(
        [
            ApiUsage(
                id=uuid4(),
                account_id=account_id,
                action_type="model_gateway",
                runtime_session_id=session_a,
                endpoint="/v1/chat",
                method="POST",
                status_code=200,
                duration=0.1,
                model_alias="old-model",
                provider_name="openai",
                timestamp=now,
            ),
            ApiUsage(
                id=uuid4(),
                account_id=account_id,
                action_type="model_gateway",
                runtime_session_id=session_a,
                endpoint="/v1/chat",
                method="POST",
                status_code=200,
                duration=0.1,
                model_alias="new-model",
                provider_name="openai",
                timestamp=now + timedelta(seconds=5),
            ),
            ApiUsage(
                id=uuid4(),
                account_id=account_id,
                action_type="model_gateway",
                runtime_session_id=session_b,
                endpoint="/v1/chat",
                method="POST",
                status_code=200,
                duration=0.1,
                model_alias="session-b-model",
                provider_name="anthropic",
                timestamp=now,
            ),
        ]
    )
    db_session.commit()

    latest = _latest_gateway_usage_for_sessions(
        db_session,
        account_id=account_id,
        runtime_session_ids=[str(session_a), str(session_b)],
    )

    assert set(latest) == {str(session_a), str(session_b)}
    assert latest[str(session_a)].model_alias == "new-model"
    assert latest[str(session_b)].model_alias == "session-b-model"


def test_summary_columns_available_caches_per_bind(db_session) -> None:
    """Summary-column detection should introspect the schema only once per bind."""
    _summary_columns_cache.clear()
    bind = db_session.get_bind() or db_session.bind
    assert bind is not None
    inspector = MagicMock()
    inspector.get_columns.return_value = [
        {"name": "summary"},
        {"name": "summary_updated_at"},
        {"name": "title"},
        {"name": "title_request_count"},
        {"name": "id"},
    ]
    inspect_mock = MagicMock(return_value=inspector)

    with patch("preloop.models.crud.runtime_session.inspect", inspect_mock):
        assert crud_runtime_session._summary_columns_available(db_session) is True
        assert crud_runtime_session._summary_columns_available(db_session) is True

    inspect_mock.assert_called_once_with(bind)
    assert inspector.get_columns.call_count == 1
    assert id(bind) in _summary_columns_cache


def _create_ai_model(db_session, account_id, name):
    """Create one account-owned model configuration for usage rows to point at."""
    ai_model = AIModel(
        name=name,
        provider_name="openai",
        model_identifier="gpt-4o",
        account_id=account_id,
    )
    db_session.add(ai_model)
    db_session.flush()
    return ai_model


def test_count_active_sessions_by_model_matches_the_per_model_list(
    db_session, create_account
) -> None:
    """The grouped count must equal what the per-model list would total.

    The Models page reads this instead of calling the runtime-session list
    endpoint once per model, so the two have to agree.
    """
    account = create_account()
    account_id = str(account.id)
    model_a = _create_ai_model(db_session, account_id, "model-a").id
    model_b = _create_ai_model(db_session, account_id, "model-b").id
    now = datetime.now(UTC)

    open_a = uuid4()
    open_a_second = uuid4()
    ended_a = uuid4()
    open_b = uuid4()
    db_session.add_all(
        [
            RuntimeSession(
                id=session_id,
                account_id=account_id,
                session_source_type="openclaw",
                session_source_id=str(session_id),
                session_reference=str(session_id),
                runtime_principal_type="openclaw",
                runtime_principal_id=str(session_id),
                started_at=now - timedelta(hours=1),
                last_activity_at=now,
                ended_at=now if session_id == ended_a else None,
            )
            for session_id in (open_a, open_a_second, ended_a, open_b)
        ]
    )

    def usage(session_id, model_id, minutes_ago=5):
        return ApiUsage(
            id=uuid4(),
            account_id=account_id,
            endpoint="/api/v1/gateway/chat/completions",
            method="POST",
            status_code=200,
            duration=0.1,
            action_type="model_gateway",
            ai_model_id=model_id,
            runtime_session_id=session_id,
            timestamp=now - timedelta(minutes=minutes_ago),
        )

    db_session.add_all(
        [
            usage(open_a, model_a),
            # Two requests from one session must count as one session.
            usage(open_a, model_a, minutes_ago=4),
            usage(open_a_second, model_a),
            usage(ended_a, model_a),
            usage(open_b, model_b),
            # Outside the window, so it must not count.
            usage(open_b, model_b, minutes_ago=60 * 24 * 40),
        ]
    )
    db_session.commit()

    counts = crud_runtime_session.count_active_sessions_by_model(
        db_session,
        account_id=account_id,
        ai_model_ids=[str(model_a), str(model_b)],
        start_date=now - timedelta(days=7),
        end_date=now + timedelta(minutes=1),
    )

    for model_id in (model_a, model_b):
        listed = crud_runtime_session.list_account_sessions(
            db_session,
            account_id=account_id,
            ai_model_id=str(model_id),
            status="active",
            start_date=now - timedelta(days=7),
            end_date=now + timedelta(minutes=1),
        )
        assert counts.get(str(model_id), 0) == listed["total"], model_id

    assert counts[str(model_a)] == 2
    assert counts[str(model_b)] == 1


def test_count_active_sessions_by_model_scopes_to_the_account(
    db_session, create_account
) -> None:
    """Another account's open session never lands in these counts."""
    account = create_account()
    other_account = create_account()
    model_id = _create_ai_model(db_session, str(other_account.id), "other-model").id
    now = datetime.now(UTC)

    other_session = uuid4()
    db_session.add(
        RuntimeSession(
            id=other_session,
            account_id=str(other_account.id),
            session_source_type="openclaw",
            session_source_id="other",
            session_reference="other",
            runtime_principal_type="openclaw",
            runtime_principal_id="other",
            started_at=now,
            last_activity_at=now,
        )
    )
    db_session.add(
        ApiUsage(
            id=uuid4(),
            account_id=str(other_account.id),
            endpoint="/api/v1/gateway/chat/completions",
            method="POST",
            status_code=200,
            duration=0.1,
            action_type="model_gateway",
            ai_model_id=model_id,
            runtime_session_id=other_session,
            timestamp=now,
        )
    )
    db_session.commit()

    counts = crud_runtime_session.count_active_sessions_by_model(
        db_session, account_id=str(account.id), ai_model_ids=[str(model_id)]
    )

    assert counts == {}


def test_count_active_sessions_by_model_without_models_skips_the_query(
    db_session, create_account
) -> None:
    """No models on the page means no query at all."""
    account = create_account()
    db = MagicMock()

    assert (
        crud_runtime_session.count_active_sessions_by_model(
            db, account_id=str(account.id), ai_model_ids=[]
        )
        == {}
    )
    db.query.assert_not_called()
