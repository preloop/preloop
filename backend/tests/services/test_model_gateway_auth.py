"""Tests for model gateway auth helpers."""

from unittest.mock import AsyncMock, patch

import pytest

from preloop.models.crud import crud_api_key
from preloop.services.model_gateway_auth import (
    authenticate_bearer_token,
    build_runtime_key_auth_context,
)


from datetime import datetime
from preloop.models.models.runtime_session import RuntimeSession


@pytest.mark.asyncio
async def test_authenticate_bearer_token_preserves_api_key_context(
    db_session, test_user
):
    """Runtime API key auth should preserve the ApiKey object and context_data."""
    session_id = "12345678-1234-5678-1234-567812345678"

    runtime_session = RuntimeSession(
        id=session_id,
        account_id=test_user.account_id,
        session_source_type="flow_execution",
        session_source_id="flow-123",
        started_at=datetime.utcnow(),
    )
    db_session.add(runtime_session)
    db_session.commit()

    api_key, presented_token = crud_api_key.create_runtime_key(
        db_session,
        name="Gateway Runtime Token",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={
            "flow_execution_id": "flow-123",
            "runtime_session_id": session_id,
            "runtime_principal": {"type": "flow_execution", "id": "flow-123"},
        },
    )

    with patch(
        "preloop.services.model_gateway_auth.get_user_from_token_if_valid",
        new=AsyncMock(return_value=test_user),
    ):
        auth_context = await authenticate_bearer_token(presented_token, db_session)

    assert auth_context is not None
    assert auth_context.user.id == test_user.id
    assert auth_context.api_key is not None
    assert auth_context.api_key.id == api_key.id
    assert auth_context.api_key.context_data["flow_execution_id"] == "flow-123"
    assert auth_context.api_key.context_data["runtime_session_id"] == session_id


def test_build_runtime_key_auth_context_resolves_key_and_user(db_session, test_user):
    """A minted runtime key resolves to a context without re-authenticating."""
    api_key, presented_token = crud_api_key.create_runtime_key(
        db_session,
        name="Flow Execution Token",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={
            "runtime_principal": {"type": "flow_execution", "id": "flow-777"},
        },
    )

    auth_context = build_runtime_key_auth_context(
        db_session, token=presented_token, api_key_id=str(api_key.id)
    )

    assert auth_context is not None
    assert auth_context.token == presented_token
    assert auth_context.user.id == test_user.id
    assert auth_context.api_key is not None
    assert auth_context.api_key.id == api_key.id


def test_build_runtime_key_auth_context_rejects_deactivated_key(db_session, test_user):
    """A deactivated key yields no principal so callers fail closed."""
    api_key, presented_token = crud_api_key.create_runtime_key(
        db_session,
        name="Flow Execution Token",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={},
    )
    crud_api_key.deactivate(db_session, key_id=api_key.id)

    assert (
        build_runtime_key_auth_context(
            db_session, token=presented_token, api_key_id=str(api_key.id)
        )
        is None
    )


def test_build_runtime_key_auth_context_requires_a_token(db_session, test_user):
    """No token means no context, even when the key row exists."""
    api_key, _ = crud_api_key.create_runtime_key(
        db_session,
        name="Flow Execution Token",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={},
    )

    assert (
        build_runtime_key_auth_context(db_session, token="", api_key_id=str(api_key.id))
        is None
    )
