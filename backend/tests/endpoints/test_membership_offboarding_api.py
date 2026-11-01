"""Console paths that remove a membership go through the offboarding service."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import (
    create_access_token,
    create_refresh_token,
    decode_token,
    reject_stale_token_generation,
)
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_api_key,
    crud_managed_agent_credential,
    crud_user,
)
from preloop.models.crud.membership_lifecycle import list_member_machine_credentials
from preloop.services.membership_lifecycle import (
    offboard_membership,
    reactivate_membership,
)


@pytest.fixture
def member(db_session: Session, test_user: models.User) -> SimpleNamespace:
    """A second member with a personal key and an account-owned agent key."""
    target = crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "username": "api-offboard-target",
            "email": "api-offboard@example.com",
            "is_active": True,
            "email_verified": True,
        },
    )
    personal, _ = crud_api_key.issue(
        db_session,
        obj_in={
            "account_id": target.account_id,
            "user_id": target.id,
            "name": "Personal",
            "is_active": True,
            "scopes": [],
        },
    )
    now = datetime.now(UTC)
    agent = models.ManagedAgent(
        account_id=target.account_id,
        owner_user_id=target.id,
        agent_kind="custom",
        session_source_type="custom",
        session_source_id=str(uuid4()),
        display_name="Build agent",
        lifecycle_state="active",
        lifecycle_updated_at=now,
        last_seen_at=now,
    )
    db_session.add(agent)
    db_session.flush()
    machine, _ = crud_api_key.create_runtime_key(
        db_session,
        account_id=target.account_id,
        user_id=target.id,
        name="Durable machine",
        scopes=["mcp:read"],
        context_data={
            "managed_agent_id": str(agent.id),
            "credential_kind": "managed_agent_durable",
        },
    )
    crud_managed_agent_credential.create_for_agent(
        db_session,
        account_id=target.account_id,
        agent_id=agent.id,
        api_key_id=machine.id,
        created_by_user_id=target.id,
        name="Durable",
        scopes=machine.scopes,
    )
    db_session.flush()
    return SimpleNamespace(target=target, personal=personal, machine=machine)


def _audit(db: Session, action: str, user_id: Any) -> list[models.AuditLog]:
    return list(
        db.scalars(
            select(models.AuditLog).where(
                models.AuditLog.action == action,
                models.AuditLog.resource_id == str(user_id),
            )
        )
    )


def test_service_defaults_leave_commit_to_the_caller(
    db_session: Session,
    member: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_commit() -> None:
        raise AssertionError("service committed without commit=True")

    monkeypatch.setattr(db_session, "commit", refuse_commit)
    result = offboard_membership(
        db_session,
        account_id=member.target.account_id,
        user_id=member.target.id,
        reason="Synthetic SCIM deactivation",
    )
    assert result.counts["personal_keys"] == 1
    reactivate_membership(
        db_session, account_id=member.target.account_id, user_id=member.target.id
    )
    assert member.target.is_active


def test_deactivate_endpoint_offboards_and_audits_counts(
    client: TestClient, db_session: Session, member: SimpleNamespace
) -> None:
    response = client.post(f"/api/v1/users/{member.target.id}/deactivate")
    assert response.status_code == 204
    db_session.refresh(member.target)
    db_session.refresh(member.personal)
    db_session.refresh(member.machine)
    assert not member.target.is_active
    assert member.target.offboarded_at is not None
    assert not member.personal.is_active
    assert member.machine.is_active
    events = _audit(db_session, "access.revoked", member.target.id)
    assert len(events) == 1
    assert events[0].details["counts"]["personal_keys"] == 1
    assert events[0].details["preserved_machine_key_ids"] == [str(member.machine.id)]


def test_patch_inactive_then_active_never_restores_credentials(
    client: TestClient, db_session: Session, member: SimpleNamespace
) -> None:
    url = f"/api/v1/users/{member.target.id}"
    assert client.patch(url, json={"is_active": False}).status_code == 200
    assert client.patch(url, json={"is_active": True}).status_code == 200
    db_session.refresh(member.target)
    db_session.refresh(member.personal)
    assert member.target.is_active
    assert not member.personal.is_active
    assert len(_audit(db_session, "access.revoked", member.target.id)) == 1
    restored = _audit(db_session, "access.restored", member.target.id)
    assert len(restored) == 1
    assert restored[0].details == {"credentials_restored": 0}


def test_offboarded_member_session_tokens_fail_and_sibling_tokens_hold(
    db_session: Session, test_user: models.User, member: SimpleNamespace
) -> None:
    other_account = crud_account.create(
        db_session, obj_in={"organization_name": "Other org", "is_active": True}
    )
    sibling = crud_user.create(
        db_session,
        obj_in={
            "account_id": other_account.id,
            "person_id": member.target.person_id,
            "username": "api-offboard-sibling",
            "email": member.target.email,
            "is_active": True,
            "email_verified": True,
        },
    )
    generation = member.target.auth_generation
    tokens = [
        create_access_token(
            data={"sub": member.target.username}, auth_generation=generation
        ),
        create_refresh_token(member.target.username, auth_generation=generation),
    ]
    sibling_token = create_access_token(
        data={"sub": sibling.username}, auth_generation=sibling.auth_generation
    )
    offboard_membership(
        db_session,
        account_id=member.target.account_id,
        user_id=member.target.id,
        actor=test_user,
        reason="Synthetic removal",
    )
    for token in tokens:
        with pytest.raises(HTTPException) as exc:
            reject_stale_token_generation(member.target, decode_token(token))
        assert exc.value.status_code == 401
    reject_stale_token_generation(sibling, decode_token(sibling_token))
    assert sibling.is_active


def test_cross_account_actor_is_rejected(
    db_session: Session, member: SimpleNamespace
) -> None:
    foreign_account = crud_account.create(
        db_session, obj_in={"organization_name": "Foreign org", "is_active": True}
    )
    foreign_admin = crud_user.create(
        db_session,
        obj_in={
            "account_id": foreign_account.id,
            "username": "foreign-admin",
            "email": "foreign-admin@example.com",
            "is_active": True,
            "email_verified": True,
        },
    )
    with pytest.raises(ValueError, match="Actor does not belong"):
        offboard_membership(
            db_session,
            account_id=member.target.account_id,
            user_id=member.target.id,
            actor=foreign_admin,
            reason="Foreign removal",
        )
    db_session.refresh(member.target)
    assert member.target.is_active


def test_machine_credentials_created_by_member_are_listed_for_admins(
    client: TestClient, db_session: Session, member: SimpleNamespace
) -> None:
    listed = list_member_machine_credentials(
        db_session, account_id=member.target.account_id, user_id=member.target.id
    )
    assert [item.api_key_id for item in listed] == [member.machine.id]
    assert listed[0].kind == "managed_agent"
    assert listed[0].principal_name == "Build agent"
    response = client.get(f"/api/v1/users/{member.target.id}/machine-credentials")
    assert response.status_code == 200
    body = response.json()
    assert [row["api_key_id"] for row in body] == [str(member.machine.id)]
    assert body[0]["kind"] == "managed_agent"
    assert body[0]["is_active"] is True


def test_machine_credentials_list_is_account_scoped(
    client: TestClient, db_session: Session
) -> None:
    foreign_account = crud_account.create(
        db_session, obj_in={"organization_name": "Elsewhere", "is_active": True}
    )
    foreign = crud_user.create(
        db_session,
        obj_in={
            "account_id": foreign_account.id,
            "username": "elsewhere-member",
            "email": "elsewhere@example.com",
            "is_active": True,
            "email_verified": True,
        },
    )
    response = client.get(f"/api/v1/users/{foreign.id}/machine-credentials")
    assert response.status_code == 404


def test_platform_superuser_may_offboard_in_another_account(
    db_session: Session, member: SimpleNamespace
) -> None:
    platform = crud_account.create(
        db_session, obj_in={"organization_name": "Platform", "is_active": True}
    )
    operator = crud_user.create(
        db_session,
        obj_in={
            "account_id": platform.id,
            "username": "platform-operator",
            "email": "platform-operator@example.com",
            "is_active": True,
            "is_superuser": True,
            "email_verified": True,
        },
    )
    result = offboard_membership(
        db_session,
        account_id=member.target.account_id,
        user_id=member.target.id,
        actor=operator,
        reason="Platform removal",
    )
    assert result.counts["personal_keys"] == 1
    assert not member.target.is_active
