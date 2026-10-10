"""Synthetic restricted-runtime authorization regressions; no live provider traffic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from preloop.api.auth import jwt as auth
from preloop.models import models
from preloop.models.crud import crud_restricted_runtime as authority
from preloop.models import crud


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "websocket", None])
async def test_runtime_transport_opt_in_is_http_only(
    monkeypatch: pytest.MonkeyPatch, transport: str | None
) -> None:
    from preloop.services import mcp_http

    db = MagicMock()
    authenticate = AsyncMock(return_value=None)
    monkeypatch.setattr(mcp_http, "get_db", lambda: iter([db]))
    monkeypatch.setattr(mcp_http, "get_user_from_token_if_valid", authenticate)
    connection = MagicMock(
        scope={"type": transport},
        headers={"authorization": "Bearer synthetic-runtime-key"},
    )
    assert await mcp_http.PreloopBearerAuthBackend().authenticate(connection) is None
    authenticate.assert_awaited_once_with(
        "synthetic-runtime-key", db, allow_restricted_runtime=transport == "http"
    )
    db.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["restricted_runtime", "restricted_ci"])
async def test_refused_machine_key_cannot_try_oauth_namespace(
    state: Any, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from preloop.services import mcp_http

    issue(state)
    key = state.stored[-1]
    key.credential_type = mode
    monkeypatch.setattr(mcp_http, "get_db", lambda: iter([state.db]))
    monkeypatch.setattr(
        mcp_http, "get_user_from_token_if_valid", AsyncMock(return_value=None)
    )
    lookup = Mock(return_value=key)
    monkeypatch.setattr(crud.crud_api_key, "get_by_key", lookup)
    backend = mcp_http.PreloopBearerAuthBackend()
    alternate_auth = Mock(return_value="forbidden-owner-fallback")
    monkeypatch.setattr(backend, "_check_oauth_token", alternate_auth)
    connection = MagicMock(
        scope={"type": "http"},
        headers={"authorization": "Bearer synthetic-runtime-key"},
    )
    assert await backend.authenticate(connection) is None
    alternate_auth.assert_not_called()
    lookup.assert_called_once_with(
        state.db, key="synthetic-runtime-key", include_restricted=True
    )


NOW = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Exercise actual issuance/authorization with synthetic CRUD-owned resources."""
    monkeypatch.setenv("PRELOOP_RESTRICTED_RUNTIME_CREDENTIALS", "true")
    account_id, user_id, agent_id, enrollment_id, snapshot_id, server_id = (
        uuid4() for _ in range(6)
    )
    policy = authority.RuntimePolicy(
        enabled=True,
        user_id=user_id,
        managed_agent_id=agent_id,
        enrollment_id=enrollment_id,
        policy_snapshot_id=snapshot_id,
        scopes=["mcp:read", "mcp:write"],
        resource_scope=[
            authority.ResourceScope(server_id=server_id, tools=["read_fixture"])
        ],
        expires_at=(NOW + timedelta(hours=1)).timestamp(),
    )
    reference = SimpleNamespace(
        id=uuid4(),
        account_id=account_id,
        status="active",
        meta_data={
            "generation": 1,
            authority.POLICY_KEY: policy.model_dump(mode="json"),
        },
    )
    user = SimpleNamespace(id=user_id, account_id=account_id, is_active=True)
    agent = SimpleNamespace(
        id=agent_id,
        account_id=account_id,
        lifecycle_state="active",
        owner_user_id=user_id,
        session_source_type="synthetic",
        session_source_id="approved-agent",
    )
    enrollment = SimpleNamespace(id=enrollment_id, status="validated")
    snapshot = SimpleNamespace(id=snapshot_id, is_active=True)
    runtime = SimpleNamespace(id=uuid4(), account_id=account_id, ended_at=None)
    server = SimpleNamespace(id=server_id, status="active")
    db = MagicMock()
    stored = []

    def add(obj: Any) -> None:
        if isinstance(obj, models.ApiKey):
            if obj.id is None:
                obj.id = uuid4()
            if obj not in stored:
                stored.append(obj)

    db.add.side_effect = add
    db.query.return_value.filter.return_value.first.return_value = None
    db.query.return_value.filter.return_value.populate_existing.return_value.one_or_none.side_effect = (
        lambda: stored[-1] if stored else None
    )
    monkeypatch.setattr(
        crud.crud_secret_reference, "get_for_update", Mock(return_value=reference)
    )
    monkeypatch.setattr(
        crud.crud_account, "get", Mock(return_value=SimpleNamespace(is_active=True))
    )
    monkeypatch.setattr(crud.crud_user, "get", Mock(return_value=user))
    monkeypatch.setattr(
        crud.crud_managed_agent, "get_for_account", Mock(return_value=agent)
    )
    monkeypatch.setattr(
        crud.crud_managed_agent_enrollment,
        "get_for_agent",
        Mock(return_value=enrollment),
    )
    monkeypatch.setattr(crud.crud_policy_snapshot, "get", Mock(return_value=snapshot))
    monkeypatch.setattr(crud.crud_mcp_server, "get", Mock(return_value=server))
    monkeypatch.setattr(
        crud.crud_runtime_session, "upsert_by_source", Mock(return_value=runtime)
    )
    monkeypatch.setattr(
        crud.crud_runtime_session, "get_account_session", Mock(return_value=runtime)
    )
    return SimpleNamespace(**locals())


def issue(state: SimpleNamespace, **changes: Any) -> authority.IssuedRuntimeCredential:
    values = {
        "account_id": state.account_id,
        "policy_id": state.reference.id,
        "generation": 1,
        "external_session_id": "synthetic-provider:environment:session",
        "creator_subject": "user:synthetic-creator",
        "upstream_expires_at": NOW + timedelta(minutes=30),
        "now": NOW,
    }
    values.update(changes)
    return authority.exchange(state.db, **values)


def authorize(state: SimpleNamespace, **changes: Any) -> models.User:
    values = {
        "account_id": state.account_id,
        "api_key_id": state.stored[-1].id,
        "scope": "mcp:write",
        "server_id": state.server_id,
        "upstream_tool": "read_fixture",
        "now": NOW,
    }
    values.update(changes)
    return authority.authorize(state.db, **values)


def test_issuance_uses_hashed_marked_key_and_atomic_state(state: Any) -> None:
    grant = issue(state)
    key = state.stored[-1]
    assert key.key is None
    assert key.key_hash == crud.crud_api_key.build_key_hash(grant.token)
    assert key.credential_type == "restricted_runtime" and key.credential_version == 1
    assert grant.expires_at == NOW + timedelta(minutes=10)
    assert authorize(state) is state.user
    state.db.commit.assert_not_called()
    record = next(iter(state.reference.meta_data[authority.SESSIONS_KEY].values()))
    assert record["api_key_id"] == str(grant.api_key_id)
    assert grant.token not in repr(state.reference.meta_data)


@pytest.mark.parametrize("limit", ["upstream", "policy", "grant"])
def test_expiry_is_minimum_of_all_authorities(state: Any, limit: str) -> None:
    if limit == "policy":
        state.reference.meta_data[authority.POLICY_KEY]["expires_at"] = (
            NOW + timedelta(seconds=20)
        ).timestamp()
    if limit == "grant":
        state.reference.meta_data[authority.POLICY_KEY]["grant_ttl_seconds"] = 20
    grant = issue(
        state,
        upstream_expires_at=NOW
        + timedelta(seconds=20 if limit == "upstream" else 1800),
    )
    assert grant.expires_at == NOW + timedelta(seconds=20)


@pytest.mark.parametrize(
    "change",
    [
        "disabled",
        "generation",
        "upstream",
        "policy",
        "agent",
        "enrollment",
        "snapshot",
        "tenant",
        "scope",
        "resource",
        "tool",
    ],
)
def test_invalid_exchange_has_no_key_side_effect(
    state: Any, change: str, monkeypatch: Any
) -> None:
    args: dict[str, Any] = {}
    if change == "disabled":
        state.reference.meta_data[authority.POLICY_KEY]["enabled"] = False
    if change == "generation":
        args["generation"] = 2
    if change == "upstream":
        args["upstream_expires_at"] = NOW
    if change == "policy":
        state.reference.meta_data[authority.POLICY_KEY]["expires_at"] = NOW.timestamp()
    if change == "agent":
        state.agent.lifecycle_state = "suspended"
    if change == "enrollment":
        state.enrollment.status = "restored"
    if change == "snapshot":
        state.snapshot.is_active = False
    if change == "tenant":
        monkeypatch.setattr(
            crud.crud_secret_reference, "get_for_update", Mock(return_value=None)
        )
    if change == "scope":
        args["requested_scopes"] = ["mcp:read", "admin:all"]
    if change in {"resource", "tool"}:
        args["requested_resources"] = [
            authority.ResourceScope(
                server_id=uuid4() if change == "resource" else state.server_id,
                tools=["write_all" if change == "tool" else "read_fixture"],
            )
        ]
    with pytest.raises(authority.RestrictedRuntimeDeniedError):
        issue(state, **args)
    assert not state.stored
    assert authority.SESSIONS_KEY not in state.reference.meta_data


@pytest.mark.parametrize(
    "change",
    [
        "generation",
        "disabled",
        "agent",
        "enrollment",
        "snapshot",
        "session",
        "scope",
        "resources",
        "key_context",
        "key_marker",
        "key_expiry",
        "expiry_boundary",
        "server_disabled",
    ],
)
def test_current_authority_changes_deny_next_invocation(
    state: Any, change: str
) -> None:
    issue(state)
    args: dict[str, Any] = {}
    if change == "generation":
        state.reference.meta_data["generation"] += 1
    if change == "disabled":
        state.reference.meta_data[authority.POLICY_KEY]["enabled"] = False
    if change == "agent":
        state.agent.lifecycle_state = "suspended"
    if change == "enrollment":
        state.enrollment.status = "failed"
    if change == "snapshot":
        state.snapshot.is_active = False
    if change == "session":
        state.runtime.ended_at = NOW
    if change == "scope":
        state.reference.meta_data[authority.POLICY_KEY]["scopes"] = ["mcp:read"]
    if change == "resources":
        state.reference.meta_data[authority.POLICY_KEY]["resource_scope"][0][
            "tools"
        ] = ["other"]
    if change == "key_context":
        state.stored[-1].context_data = {"restricted_runtime": {}}
    if change == "key_marker":
        state.stored[-1].credential_version = None
    if change == "key_expiry":
        state.stored[-1].expires_at = NOW + timedelta(hours=1)
    if change == "expiry_boundary":
        args["now"] = NOW + timedelta(minutes=10)
    if change == "server_disabled":
        state.server.status = "disabled"
    with pytest.raises(authority.RestrictedRuntimeDeniedError):
        authorize(state, **args)


@pytest.mark.parametrize(
    "change", ["refresh", "generation", "expired", "revoked", "reenabled"]
)
def test_same_session_never_remints_authority(state: Any, change: str) -> None:
    issue(state)
    before = len(state.stored)
    args: dict[str, Any] = {"upstream_expires_at": NOW + timedelta(hours=1)}
    if change in {"generation", "reenabled"}:
        state.reference.meta_data["generation"] += 1
        args["generation"] = 2
    if change == "expired":
        args["now"] = NOW + timedelta(minutes=11)
    if change in {"revoked", "reenabled"}:
        authority.revoke(
            state.db,
            account_id=state.account_id,
            policy_id=state.reference.id,
            external_session_id="synthetic-provider:environment:session",
        )
    with pytest.raises(
        authority.RestrictedRuntimeDeniedError, match="already_exchanged_or_revoked"
    ):
        issue(state, **args)
    assert len(state.stored) == before


def test_unknown_session_revocation_prevents_first_exchange(state: Any) -> None:
    authority.revoke(
        state.db,
        account_id=state.account_id,
        policy_id=state.reference.id,
        external_session_id="synthetic-provider:environment:session",
    )
    with pytest.raises(authority.RestrictedRuntimeDeniedError):
        issue(state)
    assert not state.stored


def test_explicit_resource_identity_required_for_dispatch(state: Any) -> None:
    issue(state)
    for args in (
        {"server_id": uuid4()},
        {"upstream_tool": "write_all"},
        {"server_id": None},
        {"upstream_tool": None},
    ):
        with pytest.raises(authority.RestrictedRuntimeDeniedError):
            authorize(state, **args)
    assert (
        authorize(state, scope="mcp:read", server_id=None, upstream_tool=None)
        is state.user
    )
    with pytest.raises(authority.RestrictedRuntimeDeniedError):
        authorize(state, scope="mcp:read", server_id=uuid4())


def test_read_only_scope_does_not_call_any_tool(state: Any) -> None:
    issue(state, requested_scopes=["mcp:read"])
    with pytest.raises(authority.RestrictedRuntimeDeniedError):
        authorize(state)
    assert authorize(state, scope="mcp:read") is state.user


def test_generic_gate_fails_closed_when_disabled(state: Any, monkeypatch: Any) -> None:
    issue(state)
    monkeypatch.delenv("PRELOOP_RESTRICTED_RUNTIME_CREDENTIALS")
    with pytest.raises(authority.RestrictedRuntimeDeniedError):
        authorize(state)
    with pytest.raises(authority.RestrictedRuntimeDeniedError):
        issue(state, external_session_id="other-session")


def test_machine_markers_never_fall_back_to_owner(state: Any, monkeypatch: Any) -> None:
    issue(state)
    lookup = Mock(return_value=state.user)
    monkeypatch.setattr(auth.crud_user, "get", lookup)
    with pytest.raises(HTTPException) as denied:
        auth._authenticate_with_api_key(state.db, state.stored[-1])
    assert denied.value.status_code == 403
    lookup.assert_not_called()


@pytest.mark.parametrize("mode", ["legacy", "restricted_runtime", "restricted_ci"])
def test_lookup_requires_runtime_specific_opt_in(state: Any, mode: str) -> None:
    issue(state)
    key = state.stored[-1]
    key.credential_type = mode
    key.credential_version = None if mode == "legacy" else 1
    state.db.query.return_value.filter.return_value.first.return_value = key
    assert crud.crud_api_key.get_by_key(state.db, key="synthetic-key") is (
        key if mode == "legacy" else None
    )
    assert crud.crud_api_key.get_by_key(
        state.db, key="synthetic-key", include_restricted_runtime=True
    ) is (None if mode == "restricted_ci" else key)


def test_supported_transport_has_current_authority_and_no_owner_fallback(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue(state)
    current = Mock(return_value=state.user)
    monkeypatch.setattr(authority, "authorize", current)
    key = state.stored[-1]
    assert key is not None
    assert (
        auth._authenticate_with_api_key(
            state.db, state.stored[-1], allow_restricted_runtime=True
        )
        is state.user
    )
    assert getattr(state.user, "_auth_api_key", None) is state.stored[-1]
    current.assert_called_once_with(
        state.db, account_id=state.account_id, api_key_id=key.id
    )
    current.side_effect = authority.RestrictedRuntimeDeniedError("synthetic-denial")
    with pytest.raises(HTTPException) as denied:
        auth._authenticate_with_api_key(
            state.db, state.stored[-1], allow_restricted_runtime=True
        )
    assert denied.value.status_code == 403
    assert denied.value.detail == "restricted_runtime_credential_denied"


def test_revoked_key_cannot_be_reactivated_by_generic_agent_resume(state: Any) -> None:
    issue(state)
    authority.revoke(
        state.db,
        account_id=state.account_id,
        policy_id=state.reference.id,
        external_session_id="synthetic-provider:environment:session",
    )
    # Even an accidental direct flag flip cannot defeat the authoritative tombstone.
    state.stored[-1].is_active = True
    with pytest.raises(authority.RestrictedRuntimeDeniedError):
        authorize(state)


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        "malformed",
        {"restricted_runtime": []},
        {"restricted_runtime": {"policy_id": "invalid"}},
    ],
)
def test_malformed_marked_context_never_authenticates(state: Any, value: Any) -> None:
    issue(state)
    state.stored[-1].context_data = value
    with pytest.raises(authority.RestrictedRuntimeDeniedError):
        authorize(state)


@pytest.mark.parametrize("scopes", [["mcp:read", {}], ["mcp:read", "mcp:read"]])
def test_malformed_scope_state_has_a_safe_denial(state: Any, scopes: Any) -> None:
    issue(state)
    key = state.stored[-1]
    key.scopes = scopes
    record = next(iter(state.reference.meta_data[authority.SESSIONS_KEY].values()))
    record["scopes"] = scopes
    with pytest.raises(authority.RestrictedRuntimeDeniedError):
        authorize(state)


@pytest.mark.parametrize("scopes", [["mcp:read", {}], ["mcp:read", "mcp:read"]])
def test_malformed_requested_scope_never_mints(state: Any, scopes: Any) -> None:
    with pytest.raises(authority.RestrictedRuntimeDeniedError):
        issue(state, requested_scopes=scopes)
    assert not state.stored


def test_replay_state_is_compact_and_still_binds_the_complete_context(
    state: Any,
) -> None:
    issue(state)
    record = next(iter(state.reference.meta_data[authority.SESSIONS_KEY].values()))
    assert "context" not in record
    assert len(record["context_digest"]) == 64
    key = state.stored[-1]
    key.context_data = {**key.context_data, "unexpected_owner_context": "changed"}
    with pytest.raises(authority.RestrictedRuntimeDeniedError):
        authorize(state)


@pytest.mark.parametrize("with_user", [False, True])
@pytest.mark.parametrize("mode", ["restricted_runtime", "restricted_ci"])
def test_model_gateway_never_falls_back_to_machine_key_owner(
    state: Any, monkeypatch: pytest.MonkeyPatch, with_user: bool, mode: str
) -> None:
    from preloop.services import model_gateway_auth as gateway_auth

    issue(state)
    key = state.stored[-1]
    key.credential_type = mode
    state.user._auth_api_key = key
    lookup = Mock(return_value=key)
    monkeypatch.setattr(crud.crud_api_key, "get_by_key", lookup)
    alternate_auth = Mock(return_value=SimpleNamespace(is_revoked=False))
    monkeypatch.setattr(
        gateway_auth.crud_oauth_mcp_access_token, "get_by_token", alternate_auth
    )
    assert (
        gateway_auth._resolve_bearer_context(
            "synthetic-key", state.db, state.user if with_user else None
        )
        is None
    )
    alternate_auth.assert_not_called()
    lookup.assert_called_once_with(
        state.db, key="synthetic-key", include_restricted=True
    )
    monkeypatch.setattr(crud.crud_api_key, "get", Mock(return_value=key))
    assert (
        gateway_auth.build_runtime_key_auth_context(
            state.db, token="synthetic-key", api_key_id=str(key.id)
        )
        is None
    )


def test_runtime_control_and_native_permission_never_resolve_machine_key(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.api.endpoints import agent_permission

    issue(state)
    state.db.query.return_value.filter.return_value.first.return_value = state.stored[
        -1
    ]
    state.db.__enter__.return_value = state.db
    monkeypatch.setattr(
        agent_permission, "get_session_factory", lambda: lambda: state.db
    )
    for resolve in (
        lambda: auth.authenticate_runtime_bearer_token(state.db, "synthetic-key"),
        lambda: agent_permission._resolve_permission_identity("synthetic-key"),
    ):
        with pytest.raises(HTTPException) as denied:
            resolve()
        assert denied.value.status_code == 401
    state.db.commit.assert_not_called()
