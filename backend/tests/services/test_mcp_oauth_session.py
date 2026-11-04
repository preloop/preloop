"""MCP HTTP opaque sessions use the canonical identity and policy gates."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import HTTPConnection

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_account, crud_api_key, crud_person, crud_user
from preloop.models.crud.oauth_mcp_token import crud_oauth_mcp_access_token
from preloop.models.crud.person import IdentityUnavailableError
from preloop.services import authentication_policy, mcp_http


@pytest.fixture
def oauth_session(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Use transient models and mocked CRUD; never open a database connection."""
    db = MagicMock()
    user = models.User(
        id=uuid4(),
        account_id=uuid4(),
        username="example-user",
        email="user@example.com",
        is_active=True,
        auth_generation=2,
    )
    person = models.Person(
        id=uuid4(),
        identity_status="active",
        email_verified_at=datetime.now(UTC),
    )
    account = models.Account(id=user.account_id, is_active=True)
    record = models.OAuthMCPAccessToken(
        user_id=user.id,
        account_id=user.account_id,
        is_revoked=False,
        expires_at=4102444800,
        auth_generation=2,
        auth_context={"amr": ["password"]},
        client_id="example-client",
        scopes=[],
    )
    user_lookup = MagicMock(return_value=user)
    person_lookup = MagicMock(return_value=person)
    account_lookup = MagicMock(return_value=account)
    token_lookup = MagicMock(return_value=record)
    gate = MagicMock()
    monkeypatch.setattr(crud_user, "get", user_lookup)
    monkeypatch.setattr(crud_person, "resolve_for_membership", person_lookup)
    monkeypatch.setattr(crud_person, "has_external_identity", lambda *a, **kw: False)
    monkeypatch.setattr(crud_account, "get", account_lookup)
    monkeypatch.setattr(crud_oauth_mcp_access_token, "get_by_token", token_lookup)
    monkeypatch.setattr(crud_api_key, "get_by_key", lambda *a, **kw: None)
    monkeypatch.setattr(mcp_http, "get_db", lambda: iter([db]))
    monkeypatch.setattr(
        mcp_http, "get_user_from_token_if_valid", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        authentication_policy,
        "get_plugin_manager",
        lambda: SimpleNamespace(get_service=lambda name: gate),
    )
    monkeypatch.setattr(settings, "require_email_verification", True)
    conn = HTTPConnection(
        {
            "type": "http",
            "path": "/mcp/v1",
            "headers": [(b"authorization", b"Bearer synthetic-session-token")],
        }
    )
    return SimpleNamespace(
        db=db,
        user=user,
        person=person,
        account=account,
        record=record,
        user_lookup=user_lookup,
        person_lookup=person_lookup,
        account_lookup=account_lookup,
        token_lookup=token_lookup,
        gate=gate,
        conn=conn,
    )


@pytest.mark.asyncio
async def test_current_oauth_session_retains_transport_identity(
    oauth_session: SimpleNamespace,
) -> None:
    result = await mcp_http.PreloopBearerAuthBackend().authenticate(oauth_session.conn)

    assert result is not None
    credentials, authenticated = result
    assert credentials.scopes == []
    assert authenticated.access_token.user is oauth_session.user
    assert authenticated.access_token.client_id == str(oauth_session.user.id)
    assert authenticated.access_token.token == "synthetic-session-token"
    oauth_session.gate.authorize.assert_called_once_with(
        oauth_session.db,
        user=oauth_session.user,
        claims={"amr": ["password"]},
        purpose="access",
        source_ip=None,
    )
    oauth_session.db.close.assert_called_once()


@pytest.mark.parametrize(
    "condition",
    [
        "missing_membership",
        "missing_person",
        "disabled_person",
        "disabled_membership",
        "missing_account",
        "disabled_account",
        "unverified_email",
        "stale_generation",
        "wrong_user",
        "wrong_account",
        "missing_account_binding",
        "malformed_account_binding",
        "invalid_context",
        "policy_denied",
    ],
)
@pytest.mark.asyncio
async def test_oauth_session_rejects_canonical_denials(
    oauth_session: SimpleNamespace, condition: str
) -> None:
    s = oauth_session
    if condition == "missing_membership":
        s.user_lookup.return_value = None
    elif condition == "missing_person":
        s.person_lookup.side_effect = IdentityUnavailableError("unavailable")
    elif condition == "disabled_person":
        s.person.identity_status = "disabled"
    elif condition == "disabled_membership":
        s.user.is_active = False
    elif condition == "missing_account":
        s.account_lookup.return_value = None
    elif condition == "disabled_account":
        s.account.is_active = False
    elif condition == "unverified_email":
        s.person.email_verified_at = None
    elif condition == "stale_generation":
        s.record.auth_generation = 1
    elif condition == "wrong_user":
        s.record.user_id = uuid4()
    elif condition == "wrong_account":
        s.record.account_id = uuid4()
    elif condition == "missing_account_binding":
        s.record.account_id = None
    elif condition == "malformed_account_binding":
        s.record.account_id = "invalid"
    elif condition == "invalid_context":
        s.record.auth_context = {"amr": ["unknown"]}
    elif condition == "policy_denied":
        s.gate.authorize.side_effect = HTTPException(403, "Policy requires sign-in")

    assert await mcp_http.PreloopBearerAuthBackend().authenticate(s.conn) is None
    s.db.close.assert_called_once()


@pytest.mark.parametrize("condition", ["missing", "revoked", "expired"])
@pytest.mark.asyncio
async def test_invalid_oauth_record_stops_before_identity_lookup(
    oauth_session: SimpleNamespace, condition: str
) -> None:
    s = oauth_session
    if condition == "missing":
        s.token_lookup.return_value = None
    elif condition == "revoked":
        s.record.is_revoked = True
    else:
        s.record.expires_at = 1

    assert await mcp_http.PreloopBearerAuthBackend().authenticate(s.conn) is None
    s.user_lookup.assert_not_called()
    s.gate.authorize.assert_not_called()
    s.db.close.assert_called_once()


@pytest.mark.asyncio
async def test_oauth_session_operational_failure_propagates(
    oauth_session: SimpleNamespace,
) -> None:
    oauth_session.gate.authorize.side_effect = RuntimeError("policy unavailable")

    with pytest.raises(RuntimeError, match="policy unavailable"):
        await mcp_http.PreloopBearerAuthBackend().authenticate(oauth_session.conn)

    oauth_session.db.close.assert_called_once()
