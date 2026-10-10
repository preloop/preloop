"""Real policy-lock serialization in a disposable schema on the CI PostgreSQL DB."""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import Engine, Table, create_engine, event, text
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_api_key, crud_secret_reference
from preloop.models.crud import crud_restricted_runtime as authority

NOW = datetime(2030, 1, 1, tzinfo=UTC)
EXTERNAL_SESSION = "synthetic-provider:environment:session"


@pytest.fixture
def restricted_database(db_engine: Engine) -> Iterator[Engine]:
    """Create real model tables and FK dependencies in one private test schema."""
    schema = "restricted_runtime_" + uuid4().hex
    engine = create_engine(
        db_engine.url, connect_args={"options": f"-csearch_path={schema},public"}
    )
    pending: list[Table] = [
        cast(Table, model.__table__)
        for model in (
            models.Account,
            models.User,
            models.ApiKey,
            models.RuntimeSession,
            models.ManagedAgent,
            models.ManagedAgentEnrollment,
            models.PolicySnapshot,
            models.MCPServer,
            models.SecretReference,
        )
    ]
    tables: dict[str, Table] = {}
    while pending:
        table = pending.pop()
        if table.name in tables:
            continue
        tables[table.name] = table
        pending.extend(fk.column.table for fk in table.foreign_keys)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        models.Base.metadata.create_all(
            connection, tables=list(tables.values()), checkfirst=False
        )
    try:
        yield engine
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


@pytest.fixture
def approved_policy(
    restricted_database: Engine, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    """Commit the trusted resources so independent exchange transactions see them."""
    monkeypatch.setenv("PRELOOP_RESTRICTED_RUNTIME_CREDENTIALS", "true")
    with Session(restricted_database) as db:
        account = models.Account(
            organization_name="Synthetic restricted runtime account"
        )
        db.add(account)
        db.flush()
        user = models.User(
            account_id=account.id,
            username="synthetic-caller",
            email="caller@example.com",
            hashed_password="synthetic-not-a-login",
            is_active=True,
            user_source="local",
        )
        db.add(user)
        db.flush()
        agent = models.ManagedAgent(
            account_id=account.id,
            owner_user_id=user.id,
            agent_kind="synthetic",
            session_source_type="synthetic",
            session_source_id="approved-agent",
            display_name="Approved fixture",
            lifecycle_state="active",
            lifecycle_updated_at=NOW,
            last_seen_at=NOW,
        )
        db.add(agent)
        db.flush()
        enrollment = models.ManagedAgentEnrollment(
            account_id=account.id,
            managed_agent_id=agent.id,
            created_by_user_id=user.id,
            status="validated",
            enrollment_type="cli_managed_config",
            last_validated_at=NOW,
        )
        snapshot = models.PolicySnapshot(
            account_id=account.id, version_number=1, snapshot_data={}, is_active=True
        )
        server = models.MCPServer(
            account_id=account.id,
            name="Protected fixture",
            url="https://fixture.example.com/mcp",
            status="active",
        )
        db.add_all([enrollment, snapshot, server])
        db.flush()
        policy = authority.RuntimePolicy(
            enabled=True,
            user_id=user.id,
            managed_agent_id=agent.id,
            enrollment_id=enrollment.id,
            policy_snapshot_id=snapshot.id,
            scopes=["mcp:read", "mcp:write"],
            resource_scope=[
                authority.ResourceScope(server_id=server.id, tools=["read_fixture"])
            ],
            expires_at=(NOW + timedelta(hours=1)).timestamp(),
        )
        reference = models.SecretReference(
            account_id=account.id,
            name="Synthetic authority",
            backend_type="local",
            secret_kind="synthetic_external_session_policy",
            status="active",
            meta_data={
                "generation": 1,
                authority.POLICY_KEY: policy.model_dump(mode="json"),
            },
        )
        db.add(reference)
        db.flush()
        fixture = SimpleNamespace(
            account_id=account.id,
            policy_id=reference.id,
            user_id=user.id,
            server_id=server.id,
            agent_id=agent.id,
            enrollment_id=enrollment.id,
            snapshot_id=snapshot.id,
        )
        db.commit()
    return fixture


def _issue(
    db: Session, policy: SimpleNamespace, *, generation: int = 1
) -> authority.IssuedRuntimeCredential:
    return authority.exchange(
        db,
        account_id=policy.account_id,
        policy_id=policy.policy_id,
        generation=generation,
        external_session_id=EXTERNAL_SESSION,
        creator_subject="user:synthetic-creator",
        upstream_expires_at=NOW + timedelta(minutes=30),
        now=NOW,
    )


def _revoke(db: Session, policy: SimpleNamespace) -> None:
    authority.revoke(
        db,
        account_id=policy.account_id,
        policy_id=policy.policy_id,
        external_session_id=EXTERNAL_SESSION,
    )


def _wait_for_policy_lock(engine: Engine, pid: int) -> None:
    """Prove that PostgreSQL is actually waiting for the competing transaction."""
    deadline = time.monotonic() + 3
    with engine.connect() as connection:
        while time.monotonic() < deadline:
            if (
                connection.execute(
                    text("SELECT cardinality(pg_blocking_pids(:pid))"), {"pid": pid}
                ).scalar_one()
                > 0
            ):
                return
            time.sleep(0.01)
    raise AssertionError("Contender never blocked on the authoritative row lock")


@pytest.mark.parametrize("first_action", ["exchange", "revoke"])
def test_concurrent_exchange_cannot_duplicate_or_resurrect(
    restricted_database: Engine, approved_policy: SimpleNamespace, first_action: str
) -> None:
    """A second exchange waits, then observes issued state or a revocation tombstone."""
    attempting = Event()
    worker = SimpleNamespace(pid=None)

    def contender() -> str:
        with Session(restricted_database) as db:
            worker.pid = db.execute(text("SELECT pg_backend_pid()")).scalar_one()
            db.execute(text("SET LOCAL lock_timeout = '5s'"))

            def before_lock(
                connection: Any,
                cursor: Any,
                statement: str,
                parameters: Any,
                context: Any,
                executemany: bool,
            ) -> None:
                if "secret_reference" in statement and "FOR UPDATE" in statement:
                    attempting.set()

            event.listen(db.connection(), "before_cursor_execute", before_lock)
            try:
                _issue(db, approved_policy)
            except authority.RestrictedRuntimeDeniedError as exc:
                return str(exc)
            raise AssertionError("Competing exchange minted another secret")

    with ThreadPoolExecutor(max_workers=1) as executor:
        with Session(restricted_database) as first:
            if first_action == "exchange":
                _issue(first, approved_policy)
            else:
                _revoke(first, approved_policy)
            future = executor.submit(contender)
            try:
                assert attempting.wait(timeout=3)
                _wait_for_policy_lock(restricted_database, worker.pid)
                first.commit()
                assert (
                    future.result(timeout=5)
                    == "runtime_session_already_exchanged_or_revoked"
                )
            finally:
                first.rollback()
    with Session(restricted_database) as db:
        count = (
            db.query(models.ApiKey)
            .filter_by(account_id=approved_policy.account_id)
            .count()
        )
        assert count == (1 if first_action == "exchange" else 0)


def test_revocation_waits_for_issuance_and_survives_reenable(
    restricted_database: Engine, approved_policy: SimpleNamespace
) -> None:
    """A revoke after issuance disables the actual key and prevents refreshed replay."""
    attempting = Event()
    worker = SimpleNamespace(pid=None)

    def revoker() -> None:
        with Session(restricted_database) as db:
            worker.pid = db.execute(text("SELECT pg_backend_pid()")).scalar_one()
            db.execute(text("SET LOCAL lock_timeout = '5s'"))

            def before_lock(
                connection: Any,
                cursor: Any,
                statement: str,
                parameters: Any,
                context: Any,
                executemany: bool,
            ) -> None:
                if "secret_reference" in statement and "FOR UPDATE" in statement:
                    attempting.set()

            event.listen(db.connection(), "before_cursor_execute", before_lock)
            _revoke(db, approved_policy)
            db.commit()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with Session(restricted_database) as first:
            grant = _issue(first, approved_policy)
            future = executor.submit(revoker)
            try:
                assert attempting.wait(timeout=3)
                _wait_for_policy_lock(restricted_database, worker.pid)
                first.commit()
                future.result(timeout=5)
            finally:
                first.rollback()
    with Session(restricted_database) as db:
        key = crud_api_key.get_by_key(
            db, key=grant.token, include_restricted_runtime=True
        )
        assert key is not None and not key.is_active
        with pytest.raises(authority.RestrictedRuntimeDeniedError):
            authority.authorize(
                db,
                account_id=approved_policy.account_id,
                api_key_id=grant.api_key_id,
                now=NOW,
            )
        reference = crud_secret_reference.get_for_update(
            db,
            secret_id=approved_policy.policy_id,
            account_id=approved_policy.account_id,
        )
        assert reference is not None
        reference.meta_data = {**(reference.meta_data or {}), "generation": 2}
        db.commit()
        with pytest.raises(
            authority.RestrictedRuntimeDeniedError, match="already_exchanged_or_revoked"
        ):
            _issue(db, approved_policy, generation=2)
        db.rollback()
        assert (
            db.query(models.ApiKey)
            .filter_by(account_id=approved_policy.account_id)
            .count()
            == 1
        )


@pytest.mark.parametrize(
    "kind,field,value",
    [
        ("account", "is_active", False),
        ("user", "is_active", False),
        ("agent", "lifecycle_state", "suspended"),
        ("enrollment", "status", "failed"),
        ("snapshot", "is_active", False),
        ("server", "status", "disabled"),
        ("runtime", "ended_at", NOW),
    ],
)
def test_current_authority_ignores_stale_identity_map(
    restricted_database: Engine,
    approved_policy: SimpleNamespace,
    kind: str,
    field: str,
    value: Any,
) -> None:
    """A reused caller session must observe independently committed revocation."""
    with Session(restricted_database) as db:
        grant = _issue(db, approved_policy)
        db.commit()
    rows = {
        "account": (models.Account, approved_policy.account_id),
        "user": (models.User, approved_policy.user_id),
        "agent": (models.ManagedAgent, approved_policy.agent_id),
        "enrollment": (models.ManagedAgentEnrollment, approved_policy.enrollment_id),
        "snapshot": (models.PolicySnapshot, approved_policy.snapshot_id),
        "server": (models.MCPServer, approved_policy.server_id),
        "runtime": (models.RuntimeSession, grant.runtime_session_id),
    }
    model, row_id = rows[kind]
    with Session(restricted_database, expire_on_commit=False) as reader:
        cached = reader.get(model, row_id)
        assert cached is not None
        authority.authorize(
            reader,
            account_id=approved_policy.account_id,
            api_key_id=grant.api_key_id,
            now=NOW,
        )
        reader.commit()
        with Session(restricted_database) as writer:
            current = writer.get(model, row_id)
            assert current is not None
            setattr(current, field, value)
            writer.commit()
        assert getattr(cached, field) != value
        with pytest.raises(authority.RestrictedRuntimeDeniedError):
            authority.authorize(
                reader,
                account_id=approved_policy.account_id,
                api_key_id=grant.api_key_id,
                now=NOW,
            )


@pytest.mark.parametrize("target", ["key", "policy_authorize", "policy_exchange"])
def test_pending_deactivation_is_preserved_before_authority_refresh(
    restricted_database: Engine, approved_policy: SimpleNamespace, target: str
) -> None:
    """Authority refresh cannot discard a caller's unflushed key/policy disable."""
    with Session(restricted_database) as db:
        grant = _issue(db, approved_policy)
        db.commit()
    with Session(restricted_database, autoflush=False) as db:
        if target == "key":
            key_row = db.get(models.ApiKey, grant.api_key_id)
            assert key_row is not None
            key_row.is_active = False
            denial = "restricted_runtime_credential_denied"
        else:
            policy_row = db.get(models.SecretReference, approved_policy.policy_id)
            assert policy_row is not None
            metadata = policy_row.meta_data or {}
            policy_row.meta_data = {
                **metadata,
                authority.POLICY_KEY: {
                    **metadata[authority.POLICY_KEY],
                    "enabled": False,
                },
            }
            denial = "runtime_policy_disabled"
        with pytest.raises(authority.RestrictedRuntimeDeniedError, match=denial):
            if target == "policy_exchange":
                _issue(db, approved_policy)
            else:
                authority.authorize(
                    db,
                    account_id=approved_policy.account_id,
                    api_key_id=grant.api_key_id,
                    now=NOW,
                )
        db.commit()
    with Session(restricted_database) as reader:
        if target == "key":
            key = reader.get(models.ApiKey, grant.api_key_id)
            assert key is not None and key.is_active is False
        else:
            reference = reader.get(models.SecretReference, approved_policy.policy_id)
            assert reference is not None and reference.meta_data is not None
            assert reference.meta_data[authority.POLICY_KEY]["enabled"] is False


def test_authorization_refuses_in_progress_policy_writer_without_waiting(
    restricted_database: Engine, approved_policy: SimpleNamespace
) -> None:
    """An in-progress revoke cannot leave timed-out auth workers holding DB slots."""
    with Session(restricted_database) as db:
        grant = _issue(db, approved_policy)
        db.commit()
    with Session(restricted_database) as writer:
        _revoke(writer, approved_policy)
        with Session(restricted_database) as reader:
            reader.execute(text("SET LOCAL lock_timeout = '2s'"))
            started = time.monotonic()
            with pytest.raises(authority.RestrictedRuntimeDeniedError):
                authority.authorize(
                    reader,
                    account_id=approved_policy.account_id,
                    api_key_id=grant.api_key_id,
                    now=NOW,
                )
            assert time.monotonic() - started < 1
            assert reader.execute(text("SELECT 1")).scalar_one() == 1
        writer.rollback()
