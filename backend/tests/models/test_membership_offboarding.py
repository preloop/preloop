"""Real membership revocation, account isolation and retained machine usability."""

from collections.abc import Iterator
from typing import Any
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_api_key,
    crud_cli_session,
    crud_managed_agent_credential,
    crud_user,
)
from preloop.models.crud.managed_agent_auth import ManagedAgentPrincipal
from preloop.services.membership_lifecycle import (
    offboard_membership,
    reactivate_membership,
)


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    with db_engine.connect() as connection:
        outer = connection.begin()
        with Session(
            bind=connection, join_transaction_mode="create_savepoint"
        ) as session:
            yield session
        outer.rollback()


@pytest.fixture
def lifecycle(db_session: Session, test_user: models.User) -> SimpleNamespace:
    account = crud_account.create(
        db_session, obj_in={"organization_name": "Sibling account", "is_active": True}
    )
    target = crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "username": "offboard-target",
            "email": "target@example.test",
            "is_active": True,
            "email_verified": True,
        },
    )
    sibling = crud_user.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "person_id": target.person_id,
            "username": "offboard-sibling",
            "email": target.email,
            "is_active": True,
            "email_verified": True,
        },
    )
    personal, personal_token = crud_api_key.issue(
        db_session,
        obj_in={
            "account_id": target.account_id,
            "user_id": target.id,
            "name": "Personal",
            "is_active": True,
            "scopes": [],
        },
    )
    other, other_token = crud_api_key.issue(
        db_session,
        obj_in={
            "account_id": sibling.account_id,
            "user_id": sibling.id,
            "name": "Sibling personal",
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
        display_name="Account automation",
        lifecycle_state="active",
        lifecycle_updated_at=now,
        last_seen_at=now,
    )
    db_session.add(agent)
    db_session.flush()
    machine, machine_token = crud_api_key.create_runtime_key(
        db_session,
        account_id=target.account_id,
        user_id=target.id,
        name="Durable machine",
        scopes=["mcp:read", "mcp:write"],
        context_data={
            "managed_agent_id": str(agent.id),
            "credential_kind": "managed_agent_durable",
        },
    )
    credential = crud_managed_agent_credential.create_for_agent(
        db_session,
        account_id=target.account_id,
        agent_id=agent.id,
        api_key_id=machine.id,
        created_by_user_id=target.id,
        name="Durable",
        scopes=machine.scopes,
    )
    cli = crud_cli_session.create(
        db_session, user_id=target.id, refresh_jti="target-jti"
    )
    other_cli = crud_cli_session.create(
        db_session, user_id=sibling.id, refresh_jti="sibling-jti"
    )
    team = models.Team(account_id=target.account_id, name="Target team")
    db_session.add(team)
    db_session.flush()
    db_session.add(models.TeamMembership(team_id=team.id, user_id=target.id))
    runner = models.FlowRunner(
        account_id=target.account_id,
        registered_by_user_id=target.id,
        name="Target runner",
        token_hash="synthetic-runner-token",
    )
    db_session.add(runner)
    db_session.commit()
    return SimpleNamespace(
        target=target,
        sibling=sibling,
        personal=personal,
        personal_token=personal_token,
        other=other,
        other_token=other_token,
        agent=agent,
        machine=machine,
        machine_token=machine_token,
        credential=credential,
        cli=cli,
        other_cli=other_cli,
        runner_id=runner.id,
    )


def test_offboarding_is_atomic_idempotent_and_membership_local(
    db_session: Session, test_user: models.User, lifecycle: SimpleNamespace
) -> None:
    f = lifecycle
    person = db_session.get(models.Person, f.target.person_id)
    original = (
        person.auth_generation,
        f.sibling.auth_generation,
        f.target.auth_generation,
    )
    result = offboard_membership(
        db_session,
        account_id=f.target.account_id,
        user_id=f.target.id,
        actor=test_user,
        reason="Synthetic removal",
    )
    assert result.counts["personal_keys"] == result.counts["cli_sessions"] == 1
    assert result.counts["team_memberships"] == result.counts["runners"] == 1
    assert result.preserved_machine_key_ids == (f.machine.id,)
    assert not f.target.is_active and not f.personal.is_active
    assert f.target.auth_generation == original[2] + 1
    assert (person.auth_generation, f.sibling.auth_generation) == original[:2]
    assert f.sibling.is_active and f.other.is_active and f.other_cli.revoked_at is None
    assert f.cli.revoked_at is not None and f.machine.is_active
    assert db_session.get(models.FlowRunner, f.runner_id) is None
    again = offboard_membership(
        db_session,
        account_id=f.target.account_id,
        user_id=f.target.id,
        actor=test_user,
        reason="Repeated removal",
    )
    assert again.already_offboarded and not again.counts
    events = list(
        db_session.scalars(
            select(models.AuditLog).where(models.AuditLog.action == "access.revoked")
        )
    )
    assert len(events) == 1
    assert events[0].details["counts"] == result.counts
    reactivate_membership(
        db_session, account_id=f.target.account_id, user_id=f.target.id, actor=test_user
    )
    assert f.target.is_active and not f.personal.is_active
    assert f.target.auth_generation == original[2] + 1
    assert f.cli.revoked_at is not None


def test_offboarding_refuses_foreign_account_and_rolls_back_audit_failure(
    db_session: Session,
    test_user: models.User,
    lifecycle: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    f = lifecycle
    with pytest.raises(ValueError):
        offboard_membership(
            db_session,
            account_id=f.sibling.account_id,
            user_id=f.target.id,
            actor=None,
            reason="Wrong account",
        )
    from preloop.services import membership_lifecycle

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Synthetic audit failure")

    monkeypatch.setattr(membership_lifecycle.crud_audit_log, "log_action", fail)
    with pytest.raises(RuntimeError, match="Synthetic audit failure"):
        offboard_membership(
            db_session,
            account_id=f.target.account_id,
            user_id=f.target.id,
            actor=test_user,
            reason="Rollback",
        )
    db_session.refresh(f.target)
    db_session.refresh(f.personal)
    assert (
        f.target.is_active and f.target.offboarded_at is None and f.personal.is_active
    )
    assert db_session.get(models.FlowRunner, f.runner_id) is not None


def test_account_machine_key_works_after_offboard_and_member_delete_only_on_runtime(
    db_session: Session, test_user: models.User, lifecycle: SimpleNamespace
) -> None:
    from preloop.api.auth.jwt import (
        _authenticate_with_api_key,
        authenticate_runtime_bearer_token,
    )
    from preloop.models.crud import managed_agent_auth

    f = lifecycle
    offboard_membership(
        db_session,
        account_id=f.target.account_id,
        user_id=f.target.id,
        actor=test_user,
        reason="Retain automation",
    )
    principal = _authenticate_with_api_key(
        db_session, f.machine, allow_managed_agent=True
    )
    assert isinstance(principal, ManagedAgentPrincipal)
    assert principal.id is None and principal.principal_id == f.agent.id
    assert principal.created_by_user_id == f.target.id
    context = authenticate_runtime_bearer_token(db_session, f.machine_token)
    assert context.managed_agent.id == f.agent.id
    with pytest.raises(HTTPException):
        _authenticate_with_api_key(db_session, f.machine)
    with pytest.raises(HTTPException):
        _authenticate_with_api_key(db_session, f.personal, allow_managed_agent=True)
    assert (
        managed_agent_auth.authenticate(
            db_session,
            account_id=f.sibling.account_id,
            key_id=f.machine.id,
            agent_id=f.agent.id,
        )
        is None
    )
    assert (
        managed_agent_auth.authenticate(
            db_session,
            account_id=f.target.account_id,
            key_id=f.machine.id,
            agent_id=uuid4(),
        )
        is None
    )
    target_id = f.target.id
    crud_user.hard_delete(db_session, user_id=target_id)
    db_session.expire_all()
    key = crud_api_key.get(db_session, id=f.machine.id)
    assert key is not None and key.is_active and key.user_id is None
    context = authenticate_runtime_bearer_token(db_session, f.machine_token)
    assert isinstance(context.user, ManagedAgentPrincipal)
    assert context.user.created_by_user_id is None
    assert f.other.is_active and f.sibling.is_active


@pytest.mark.asyncio
async def test_retained_machine_gateway_control_mcp_identity_and_rest_refusal(
    db_session: Session,
    test_user: models.User,
    lifecycle: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient
    from preloop.api.auth.jwt import (
        get_current_active_user,
        authenticate_runtime_bearer_token,
    )
    from preloop.api.endpoints.agent_control import _connection_context_from_auth
    from preloop.models.crud import agent_control_connection
    from preloop.models.db.session import get_db_session
    from preloop.services.model_gateway_auth import authenticate_bearer_token
    from preloop.services.mcp_http import PreloopBearerAuthBackend
    from preloop.services.dynamic_fastmcp import create_user_context_from_scope

    f = lifecycle
    offboard_membership(
        db_session,
        account_id=f.target.account_id,
        user_id=f.target.id,
        actor=test_user,
        reason="Runtime test",
    )
    gateway = await authenticate_bearer_token(f.machine_token, db_session)
    assert gateway is not None and gateway.account_id == f.target.account_id
    assert gateway.user.principal_id == f.agent.id and gateway.user.id is None
    from preloop.api.endpoints.desktop import _authenticate_endpoint

    desktop = _authenticate_endpoint(db_session, f.machine_token)
    assert desktop.user_id is None and desktop.bound_agent_id == f.agent.id
    from preloop.api.endpoints import agent_permission

    class BorrowedSession:
        def __enter__(self) -> Session:
            return db_session

        def __exit__(self, *args: Any) -> None:
            return None

    monkeypatch.setattr(
        agent_permission, "get_session_factory", lambda: BorrowedSession
    )
    permission = agent_permission._resolve_permission_identity(f.machine_token)
    assert permission.user_id is None and permission.managed_agent_id == f.agent.id
    snapshot = gateway.snapshot()
    assert snapshot.user.principal_id == f.agent.id
    assert snapshot.user.created_by_user_id == f.target.id
    context = authenticate_runtime_bearer_token(db_session, f.machine_token)
    control = _connection_context_from_auth(context)
    assert (
        agent_control_connection.authorize(db_session, control, claim=True).id
        == f.agent.id
    )

    from preloop.services import dynamic_fastmcp

    def sessions() -> Iterator[Session]:
        yield db_session

    monkeypatch.setattr(dynamic_fastmcp, "get_db", sessions)
    monkeypatch.setattr(db_session, "close", lambda: None)
    result = PreloopBearerAuthBackend._build_auth_result(
        f.machine_token, context.user, f.machine
    )
    mcp = create_user_context_from_scope({"user": result[1]})
    assert mcp is not None and mcp.managed_agent_id == str(f.agent.id)
    assert mcp.user_id is None and mcp.runtime_principal_id == str(f.agent.id)

    app = FastAPI()
    app.dependency_overrides[get_db_session] = lambda: db_session

    @app.get("/human")
    def human(user: Any = Depends(get_current_active_user)) -> None:
        return {"id": str(user.id)}

    with TestClient(app) as browser:
        assert (
            browser.get(
                "/human", headers={"Authorization": "Bearer " + f.machine_token}
            ).status_code
            == 403
        )
        assert (
            browser.get(
                "/human", headers={"Authorization": "Bearer " + f.personal_token}
            ).status_code
            == 401
        )


def test_oauth_and_personal_runtime_revocation_is_scoped(
    db_session: Session, test_user: models.User, lifecycle: SimpleNamespace
) -> None:
    f = lifecycle
    rows = []
    for user in (f.target, f.sibling):
        rows.append(
            [
                models.OAuthMCPAuthorizationCode(
                    account_id=user.account_id,
                    user_id=user.id,
                    code_hash=str(uuid4()),
                    client_id="synthetic",
                    redirect_uri="http://localhost/callback",
                    code_challenge="synthetic",
                    expires_at=2000000000,
                ),
                models.OAuthMCPAccessToken(
                    account_id=user.account_id,
                    user_id=user.id,
                    token_hash=str(uuid4()),
                    client_id="synthetic",
                ),
                models.OAuthMCPRefreshToken(
                    account_id=user.account_id,
                    user_id=user.id,
                    token_hash=str(uuid4()),
                    client_id="synthetic",
                ),
            ]
        )
        db_session.add_all(rows[-1])
    now = datetime.now(UTC)
    personal_session = models.RuntimeSession(
        account_id=f.target.account_id,
        session_source_type="custom",
        session_source_id=str(uuid4()),
        started_at=now,
    )
    machine_session = models.RuntimeSession(
        account_id=f.target.account_id,
        session_source_type="custom",
        session_source_id=str(uuid4()),
        started_at=now,
    )
    db_session.add_all([personal_session, machine_session])
    db_session.flush()
    f.personal.context_data = {"runtime_session_id": str(personal_session.id)}
    f.machine.context_data = {
        **f.machine.context_data,
        "runtime_session_id": str(machine_session.id),
    }
    db_session.commit()
    result = offboard_membership(
        db_session,
        account_id=f.target.account_id,
        user_id=f.target.id,
        actor=test_user,
        reason="Sessions",
    )
    assert rows[0][0].is_used and rows[0][1].is_revoked and rows[0][2].is_revoked
    assert (
        not rows[1][0].is_used
        and not rows[1][1].is_revoked
        and not rows[1][2].is_revoked
    )
    for name in (
        "oauth_mcp_authorization_code",
        "oauth_mcp_access_token",
        "oauth_mcp_refresh_token",
        "runtime_sessions",
    ):
        assert result.counts[name] == 1
    assert personal_session.ended_at is not None
    assert machine_session.ended_at is None


def test_late_personal_issuance_refused_and_scim_rollback_restores_all(
    db_session: Session, test_user: models.User, lifecycle: SimpleNamespace
) -> None:
    from preloop.models.crud import crud_flow_runner

    f = lifecycle
    offboard_membership(
        db_session,
        account_id=f.target.account_id,
        user_id=f.target.id,
        actor=None,
        reason="SCIM",
        commit=False,
    )
    for issue in (
        lambda: crud_api_key.issue(
            db_session,
            obj_in={
                "account_id": f.target.account_id,
                "user_id": f.target.id,
                "name": "Late key",
            },
        ),
        lambda: crud_api_key.create_runtime_key(
            db_session,
            account_id=f.target.account_id,
            user_id=f.target.id,
            name="Late runtime",
        ),
        lambda: crud_api_key.create_with_owner(
            db_session, obj_in={}, owner_username=f.target.username
        ),
        lambda: crud_flow_runner.create(
            db_session,
            obj_in={
                "account_id": f.target.account_id,
                "registered_by_user_id": f.target.id,
                "name": "Late runner",
                "token_hash": "late-synthetic",
            },
        ),
    ):
        with pytest.raises(ValueError, match="Inactive membership"):
            issue()
    db_session.rollback()
    db_session.refresh(f.target)
    db_session.refresh(f.personal)
    db_session.refresh(f.cli)
    assert f.target.is_active and f.personal.is_active and f.cli.revoked_at is None
    assert db_session.get(models.FlowRunner, f.runner_id) is not None


def test_reactivation_refuses_disabled_person_and_capacity_failure(
    db_session: Session,
    test_user: models.User,
    lifecycle: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.services import multiuser

    f = lifecycle
    offboard_membership(
        db_session,
        account_id=f.target.account_id,
        user_id=f.target.id,
        actor=test_user,
        reason="Remove",
    )
    person = db_session.get(models.Person, f.target.person_id)
    person.identity_status = "suspended"
    db_session.commit()
    with pytest.raises(ValueError, match="unavailable"):
        reactivate_membership(
            db_session, account_id=f.target.account_id, user_id=f.target.id
        )
    person.identity_status = "active"
    db_session.commit()

    def full(*args: Any, **kwargs: Any) -> None:
        raise HTTPException(409, "Synthetic capacity full")

    monkeypatch.setattr(multiuser, "enforce_seat_available", full)
    with pytest.raises(HTTPException):
        reactivate_membership(
            db_session, account_id=f.target.account_id, user_id=f.target.id
        )
    db_session.refresh(f.target)
    assert not f.target.is_active and not f.personal.is_active


@pytest.mark.parametrize("disabled", ["key", "credential", "agent", "account"])
def test_machine_fresh_lifecycle_gate(
    db_session: Session,
    test_user: models.User,
    lifecycle: SimpleNamespace,
    disabled: str,
) -> None:
    from preloop.api.auth.jwt import authenticate_runtime_bearer_token

    f = lifecycle
    offboard_membership(
        db_session,
        account_id=f.target.account_id,
        user_id=f.target.id,
        actor=test_user,
        reason="Remove",
    )
    if disabled == "key":
        f.machine.is_active = False
    elif disabled == "credential":
        f.credential.status = "revoked"
    elif disabled == "agent":
        f.agent.lifecycle_state = "deactivated"
    else:
        db_session.get(models.Account, f.target.account_id).is_active = False
    db_session.commit()
    with pytest.raises(HTTPException):
        authenticate_runtime_bearer_token(db_session, f.machine_token)


def test_account_delete_cascades_retained_machine_credentials(
    db_session: Session, test_user: models.User, lifecycle: SimpleNamespace
) -> None:
    from sqlalchemy import delete

    f = lifecycle
    account_id, key_id, credential_id = (
        f.target.account_id,
        f.machine.id,
        f.credential.id,
    )
    offboard_membership(
        db_session,
        account_id=account_id,
        user_id=f.target.id,
        actor=test_user,
        reason="Removal",
    )
    crud_user.hard_delete(db_session, user_id=f.target.id)
    db_session.execute(delete(models.Account).where(models.Account.id == account_id))
    db_session.flush()
    assert (
        db_session.scalar(select(models.ApiKey.id).where(models.ApiKey.id == key_id))
        is None
    )
    assert (
        db_session.scalar(
            select(models.ManagedAgentCredential.id).where(
                models.ManagedAgentCredential.id == credential_id
            )
        )
        is None
    )
    assert db_session.get(models.Account, f.sibling.account_id) is not None


def test_offboarding_migration_roundtrip_and_orphan_downgrade_refusal(
    db_engine: Engine,
) -> None:
    import importlib
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    migration = importlib.import_module(
        "preloop.models.alembic.versions.20261117_membership_offboarding"
    )
    # Isolated transactional schema never changes the shared test database head.
    schema = "offboard_migration_" + uuid4().hex
    with db_engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            connection.execute(text('CREATE TABLE "user" (id uuid PRIMARY KEY)'))
            connection.execute(
                text(
                    'CREATE TABLE api_key (id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES "user"(id) ON DELETE CASCADE)'
                )
            )
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                fk = inspect(connection).get_foreign_keys("api_key")[0]
                assert fk["options"]["ondelete"] == "SET NULL"
                migration.downgrade()
                assert not inspect(connection).get_columns("api_key")[1]["nullable"]
                migration.upgrade()
                connection.execute(
                    text("INSERT INTO api_key (id) VALUES (:id)"), {"id": uuid4()}
                )
                with pytest.raises(RuntimeError, match="deleted creators"):
                    migration.downgrade()
                assert connection.scalar(text("SELECT count(*) FROM api_key")) == 1
        finally:
            transaction.rollback()


def test_concurrent_stale_request_cannot_issue_after_offboarding(
    db_engine: Engine,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from sqlalchemy import delete, event

    ready, proceed, requesting_lock = Event(), Event(), Event()
    account_id = user_id = None
    with Session(db_engine) as setup:
        account = crud_account.create(
            setup,
            obj_in={"organization_name": "Concurrent offboarding", "is_active": True},
        )
        user = crud_user.create(
            setup,
            obj_in={
                "account_id": account.id,
                "username": "offboard-" + uuid4().hex,
                "email": uuid4().hex + "@example.test",
                "is_active": True,
            },
        )
        account_id, user_id, person_id = account.id, user.id, user.person_id

    def late_request() -> None:
        with Session(db_engine) as issuer:
            stale_user = issuer.get(models.User, user_id)
            assert stale_user is not None and stale_user.is_active
            ready.set()
            assert proceed.wait(timeout=5)

            def before_execute(
                connection: Any,
                cursor: Any,
                statement: str,
                parameters: Any,
                context: Any,
                executemany: bool,
            ) -> None:
                if "FROM account" in statement and "FOR UPDATE" in statement:
                    requesting_lock.set()

            event.listen(issuer.connection(), "before_cursor_execute", before_execute)
            with pytest.raises(ValueError, match="Inactive membership"):
                crud_api_key.issue(
                    issuer,
                    obj_in={
                        "account_id": account_id,
                        "user_id": stale_user.id,
                        "name": "Late personal key",
                    },
                )
            issuer.rollback()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(late_request)
            assert ready.wait(timeout=5)
            with Session(db_engine) as remover:
                offboard_membership(
                    remover,
                    account_id=account_id,
                    user_id=user_id,
                    reason="Concurrent removal",
                    actor=None,
                    commit=False,
                )
                proceed.set()
                assert requesting_lock.wait(timeout=5)
                assert not pending.done()
                remover.commit()
            pending.result(timeout=5)
        with Session(db_engine) as check:
            assert (
                check.scalar(
                    select(models.ApiKey.id).where(
                        models.ApiKey.account_id == account_id
                    )
                )
                is None
            )
    finally:
        proceed.set()
        with Session(db_engine) as cleanup:
            cleanup.execute(
                delete(models.Account).where(models.Account.id == account_id)
            )
            cleanup.execute(delete(models.Person).where(models.Person.id == person_id))
            cleanup.commit()
