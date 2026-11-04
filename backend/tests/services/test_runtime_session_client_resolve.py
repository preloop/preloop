"""Resolve a client session key to the runtime session the gateway would use (#1358)."""

from datetime import datetime, timezone

from preloop.models.crud import (
    crud_account,
    crud_api_key,
    crud_runtime_session,
    crud_user,
)
from preloop.models.models.runtime_session import RuntimeSession
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService


def _key(db_session, user, *, name="Console key", context_data=None):
    api_key, _token = crud_api_key.create_runtime_key(
        db_session,
        name=name,
        account_id=user.account_id,
        user_id=user.id,
        context_data=context_data,
    )
    return api_key


def _principal_key(db_session, user, principal_id="static-agent"):
    return _key(
        db_session,
        user,
        name="Static Agent",
        context_data={
            "runtime_principal": {
                "type": "custom",
                "id": principal_id,
                "name": "Static Agent",
            }
        },
    )


def _service(db_session, user, api_key, client_session_id):
    return OpenAIGatewayService(
        db_session,
        ModelGatewayAuthContext(token="t", user=user, api_key=api_key),
        client_session_id=client_session_id,
        client_session_id_is_explicit=True,
    )


def _resolve(db_session, user, api_key, client_session_id, *, create):
    return _service(
        db_session, user, api_key, client_session_id
    ).resolve_client_runtime_session(create=create)


def _other_user(db_session):
    account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )
    return crud_user.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "email": "other@example.com",
            "username": "otheruser",
            "full_name": "Jane Doe",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "testpassword",
            "user_source": "local",
        },
    )


def test_lookup_without_create_finds_nothing_and_creates_nothing(db_session, test_user):
    api_key = _key(db_session, test_user)
    assert _resolve(db_session, test_user, api_key, "conv-1", create=False) == (
        None,
        False,
    )
    assert (
        db_session.query(RuntimeSession)
        .filter(RuntimeSession.account_id == test_user.account_id)
        .count()
        == 0
    )


def test_create_then_lookup_returns_same_row_as_gateway_keying(db_session, test_user):
    api_key = _key(db_session, test_user)
    created_id, created = _resolve(
        db_session, test_user, api_key, "conv-1", create=True
    )
    assert created is True
    row = crud_runtime_session.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="api_key",
        session_source_id=f"{api_key.id}:conv-1",
    )
    assert row is not None and str(row.id) == created_id

    assert _resolve(db_session, test_user, api_key, "conv-1", create=True) == (
        created_id,
        False,
    )
    assert _resolve(db_session, test_user, api_key, "conv-1", create=False) == (
        created_id,
        False,
    )
    # The gateway attributes the same key to the same row.
    gateway = _service(db_session, test_user, api_key, "conv-1")
    assert gateway._resolve_runtime_session() == created_id


def test_principal_key_uses_run_id_suffix(db_session, test_user):
    api_key = _principal_key(db_session, test_user)
    created_id, created = _resolve(db_session, test_user, api_key, "run-7", create=True)
    assert created is True
    row = crud_runtime_session.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="custom",
        session_source_id="static-agent:run-7",
    )
    assert row is not None and str(row.id) == created_id


def test_idle_generation_rows_are_never_matched(db_session, test_user):
    api_key = _principal_key(db_session, test_user)
    # Signal-less traffic lands on the base row (no client id).
    base = _service(db_session, test_user, api_key, None)._resolve_runtime_session()
    assert base is not None
    assert _resolve(db_session, test_user, api_key, "run-1", create=False) == (
        None,
        False,
    )


def test_session_bound_key_returns_its_pinned_session(db_session, test_user):
    pinned = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="custom",
        session_source_id="pinned-source",
        started_at=datetime.now(timezone.utc),
    )
    api_key = _key(
        db_session,
        test_user,
        name="Pinned",
        context_data={"runtime_session_id": str(pinned.id)},
    )
    assert _resolve(db_session, test_user, api_key, "anything", create=False) == (
        str(pinned.id),
        False,
    )


def test_other_account_key_never_sees_the_session(db_session, test_user):
    api_key = _key(db_session, test_user)
    created_id, _ = _resolve(db_session, test_user, api_key, "conv-1", create=True)
    assert created_id

    other = _other_user(db_session)
    other_key = _key(db_session, other, name="Other key")
    found, created = _resolve(db_session, other, other_key, "conv-1", create=False)
    assert found is None and created is False
    made, created = _resolve(db_session, other, other_key, "conv-1", create=True)
    assert made != created_id and created is True


def test_resolved_runtime_session_id_never_triggers_resolution(db_session, test_user):
    api_key = _key(db_session, test_user)
    service = _service(db_session, test_user, api_key, "conv-9")
    assert service.resolved_runtime_session_id is None
    assert (
        db_session.query(RuntimeSession)
        .filter(RuntimeSession.account_id == test_user.account_id)
        .count()
        == 0
    )
    resolved = service._resolve_runtime_session()
    assert resolved is not None
    assert service.resolved_runtime_session_id == resolved
