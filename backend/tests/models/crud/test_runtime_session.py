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
