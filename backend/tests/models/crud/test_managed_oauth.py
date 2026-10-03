"""Real PostgreSQL regression tests for tenant-bound managed OAuth storage."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, delete, event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.managed_oauth import (
    CRUDManagedOAuth,
    OAuthConflictError,
    TokenPair,
)
from preloop.models.crud.oauth_token import crud_oauth_token
from preloop.utils.encryption import decrypt_value

CALLBACK = "https://example.com/oauth/callback"
PAIR = TokenPair(
    "synthetic-access",
    "synthetic-refresh",
    datetime.now(timezone.utc) + timedelta(hours=1),
)


class LockWaiter:
    """Observe actual PostgreSQL lock contention by the selected worker session."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.thread_id: int | None = None
        self.pid: int | None = None
        self.ready = threading.Event()

    def __enter__(self) -> "LockWaiter":
        event.listen(self.engine, "before_cursor_execute", self.capture)
        return self

    def __exit__(self, *_: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self.capture)

    def capture(
        self,
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if threading.get_ident() == self.thread_id and "FOR UPDATE" in statement:
            self.pid = connection.connection.driver_connection.info.backend_pid
            self.ready.set()

    def run(self, operation: Callable[[], Any]) -> Any:
        self.thread_id = threading.get_ident()
        return operation()

    def wait_until_blocked(self) -> None:
        assert self.ready.wait(5), "worker never attempted its row lock"
        deadline = time.monotonic() + 5
        with self.engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            while time.monotonic() < deadline:
                blocked = connection.execute(
                    text(
                        "SELECT wait_event_type = 'Lock' AND cardinality(pg_blocking_pids(pid)) > 0 "
                        "FROM pg_stat_activity WHERE pid = :pid"
                    ),
                    {"pid": self.pid},
                ).scalar()
                if blocked:
                    return
                time.sleep(0.01)
        raise AssertionError("PostgreSQL never observed the worker waiting on a lock")


@pytest.fixture
def storage() -> Iterator[tuple[CRUDManagedOAuth, list[tuple[UUID, UUID]]]]:
    engine = create_engine(os.environ["DATABASE_URL"])
    assert engine.dialect.name == "postgresql"
    owners = []
    with Session(engine) as db, db.begin():
        for _ in range(2):
            account = models.Account(organization_name="Synthetic OAuth tenant")
            db.add(account)
            db.flush()
            user = models.User(
                account_id=account.id,
                username=str(uuid4()),
                email=f"{uuid4()}@example.com",
                hashed_password="synthetic",
            )
            db.add(user)
            db.flush()
            owners.append((account.id, user.id))
    try:
        yield CRUDManagedOAuth(engine), owners
    finally:
        with engine.begin() as conn:
            for account_id, _ in owners:
                conn.execute(
                    delete(models.Account).where(models.Account.id == account_id)
                )
        engine.dispose()


def configuration(crud: CRUDManagedOAuth, account_id: UUID) -> dict:
    return crud.create_configuration(
        account_id=account_id,
        provider="bitbucket",
        instance="https://EXAMPLE.com:443/bitbucket/",
        context="workspace",
        client_id="synthetic-client",
        client_secret="synthetic-client-secret",
        callback_uri=CALLBACK,
        selected_permissions=["repository:read"],
    )


def pending(
    crud: CRUDManagedOAuth, owner: tuple[UUID, UUID], config: dict | None = None
) -> tuple[dict, dict, dict]:
    account_id, user_id = owner
    config = config or configuration(crud, account_id)
    transaction, state = crud.begin_connection(
        account_id=account_id,
        user_id=user_id,
        session_id="synthetic-session",
        configuration_id=config["id"],
        return_path="/trackers",
        pkce_verifier="synthetic-verifier",
    )
    binding = dict(
        account_id=account_id,
        user_id=user_id,
        session_id="synthetic-session",
        state=state,
        callback_uri=CALLBACK,
    )
    claimed, credentials = crud.claim_callback(**binding)
    assert claimed["status"] == "claimed"
    assert credentials.pkce_verifier == "synthetic-verifier"
    assert credentials.client_secret == "synthetic-client-secret"
    grant = crud.store_pending_grant(
        **binding, provider_subject="same-provider-subject", tokens=PAIR
    )
    return binding, grant, config


def active(
    crud: CRUDManagedOAuth, owner: tuple[UUID, UUID], config: dict | None = None
) -> tuple[dict, dict, dict]:
    binding, grant, config = pending(crud, owner, config)
    tracker = crud.complete_connection(**binding, tracker_name="Synthetic tracker")
    grant = crud.get_grant(account_id=owner[0], tracker_id=tracker)
    return binding, grant, config


def test_tenant_isolation_ciphertext_and_legacy_lookup(storage: tuple) -> None:
    crud, owners = storage
    _, first, config = active(crud, owners[0])
    _, second, _ = active(crud, owners[1])
    assert first["provider_subject"] == second["provider_subject"]
    assert first["id"] != second["id"]
    assert first["tracker_id"] != second["tracker_id"]
    assert config["canonical_instance"] == "https://example.com/bitbucket"
    assert crud.get_grant(account_id=owners[1][0], grant_id=first["id"]) is None
    assert (
        crud.get_configuration(account_id=owners[1][0], configuration_id=config["id"])
        is None
    )
    with crud._session() as db:
        grant = db.get(models.OAuthToken, first["id"])
        assert grant.access_token_encrypted != PAIR.access_token
        assert decrypt_value(grant.refresh_token_encrypted) == PAIR.refresh_token
        assert grant.expires_at.tzinfo is not None
        assert "synthetic-access" not in repr(grant)
        assert "access_token_encrypted" not in grant.to_dict()
        assert "refresh_token_encrypted" not in grant.to_dict()
        config_row = db.get(models.OAuthProviderConfiguration, config["id"])
        secret = db.get(models.SecretReference, config_row.client_secret_id)
        assert decrypt_value(secret.encrypted_value) == "synthetic-client-secret"
        assert "client_secret_id" not in config_row.to_dict()
        assert "synthetic-client-secret" not in repr(config_row)
        tracker = db.get(models.Tracker, first["tracker_id"])
        assert tracker.api_key is None and tracker.credentials_secret_id is None
        assert tracker.resolved_api_key == ""
        assert "_resolved_api_key_cache" not in tracker.__dict__
        assert (
            crud_oauth_token.get_by_user_and_provider(
                db, provider="bitbucket", user_id=owners[0][1]
            )
            is None
        )


@pytest.mark.parametrize(
    "field", ["account_id", "user_id", "session_id", "callback_uri", "state"]
)
def test_callback_binding_and_replay(storage: tuple, field: str) -> None:
    crud, owners = storage
    binding, grant, _ = pending(crud, owners[0])
    wrong = {
        **binding,
        field: owners[1][0]
        if field == "account_id"
        else owners[1][1]
        if field == "user_id"
        else "wrong",
    }
    with pytest.raises(OAuthConflictError):
        crud.complete_connection(**wrong, tracker_name="Forbidden")
    with pytest.raises(OAuthConflictError):
        crud.claim_callback(**binding)
    with crud._session() as db:
        row = db.scalar(
            select(models.OAuthConnectionTransaction).where(
                models.OAuthConnectionTransaction.pending_grant_id == grant["id"]
            )
        )
        assert row.state_hash != binding["state"]
        assert row.session_hash != binding["session_id"]
        assert (
            not {"state_hash", "session_hash", "pkce_verifier_encrypted"}
            & row.to_dict().keys()
        )


def test_concurrent_completion_returns_one_tracker(storage: tuple) -> None:
    crud, owners = storage
    binding, _, _ = pending(crud, owners[0])
    barrier = threading.Barrier(2)

    def complete() -> UUID:
        barrier.wait(timeout=5)
        return crud.complete_connection(**binding, tracker_name="Concurrent")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(complete) for _ in range(2)]
        trackers = [f.result(timeout=10) for f in futures]
    assert trackers[0] == trackers[1]
    with crud._session() as db:
        assert (
            len(
                db.scalars(
                    select(models.Tracker).where(
                        models.Tracker.account_id == owners[0][0]
                    )
                ).all()
            )
            == 1
        )
        tx = db.scalar(
            select(models.OAuthConnectionTransaction).where(
                models.OAuthConnectionTransaction.tracker_id == trackers[0]
            )
        )
        tx.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
    assert crud.complete_connection(**binding, tracker_name="Retry") == trackers[0]


def test_tenant_fk_rejects_cross_account_tracker(storage: tuple) -> None:
    crud, owners = storage
    _, first, _ = active(crud, owners[0])
    _, second, _ = active(crud, owners[1])
    crud.disconnect(account_id=owners[1][0], grant_id=second["id"], expected_version=0)
    with (
        crud._session() as db,
        pytest.raises(IntegrityError, match="fk_oauth_grant_tracker_tenant"),
    ):
        # A forged account association must fail even outside the CRUD checks.
        db.execute(
            text("UPDATE oauth_token SET tracker_id=:tracker WHERE id=:grant"),
            {"tracker": second["tracker_id"], "grant": first["id"]},
        )
        db.commit()


def test_two_independent_refresh_sessions_reread_version(storage: tuple) -> None:
    crud, owners = storage
    _, grant, _ = active(crud, owners[0])
    entered, release = threading.Event(), threading.Event()
    calls = []

    def provider(pair: TokenPair, credentials: object, timeout: float) -> TokenPair:
        calls.append(pair.refresh_token)
        entered.set()
        assert release.wait(5)
        return TokenPair("rotated-access", "rotated-refresh", PAIR.expires_at)

    def rotate() -> dict:
        return crud.rotate(
            account_id=owners[0][0],
            grant_id=grant["id"],
            expected_version=0,
            refresh=provider,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(rotate)
        assert entered.wait(5)
        with LockWaiter(crud._engine) as waiter:
            second = pool.submit(waiter.run, rotate)
            try:
                waiter.wait_until_blocked()
            finally:
                release.set()
        assert first.result(timeout=10)["rotation_version"] == 1
        with pytest.raises(OAuthConflictError):
            second.result(timeout=10)
    assert calls == ["synthetic-refresh"]
    with crud._session() as db:
        row = db.get(models.OAuthToken, grant["id"])
        assert decrypt_value(row.access_token_encrypted) == "rotated-access"
        assert decrypt_value(row.refresh_token_encrypted) == "rotated-refresh"


def test_reconnect_disconnect_reject_stale_rotation(storage: tuple) -> None:
    crud, owners = storage
    _, grant, config = active(crud, owners[0])
    binding, _, _ = pending(crud, owners[0], config)
    assert (
        crud.complete_connection(
            **binding,
            tracker_name="Reconnect",
            tracker_id=grant["tracker_id"],
            expected_rotation_version=0,
        )
        == grant["tracker_id"]
    )
    with pytest.raises(OAuthConflictError):
        crud.rotate(
            account_id=owners[0][0],
            grant_id=grant["id"],
            expected_version=0,
            refresh=lambda *_: PAIR,
        )
    crud.disconnect(account_id=owners[0][0], grant_id=grant["id"], expected_version=1)
    with pytest.raises(OAuthConflictError):
        crud.rotate(
            account_id=owners[0][0],
            grant_id=grant["id"],
            expected_version=2,
            refresh=lambda *_: PAIR,
        )
    with crud._session() as db:
        row = db.get(models.OAuthToken, grant["id"])
        assert (
            row.tracker_id is None
            and row.access_token_encrypted == ""
            and row.refresh_token_encrypted is None
        )
        assert db.get(models.OAuthProviderConfiguration, config["id"]) is not None
    # A disconnected tracker can acquire a new grant. The old id remains stale.
    binding, replacement, _ = pending(crud, owners[0], config)
    crud.complete_connection(
        **binding, tracker_name="Reconnect", tracker_id=grant["tracker_id"]
    )
    assert (
        crud.get_grant(account_id=owners[0][0], tracker_id=grant["tracker_id"])["id"]
        == replacement["id"]
    )


def test_timeout_and_failure_roll_back_without_committing_caller(
    storage: tuple,
) -> None:
    crud, owners = storage
    _, grant, _ = active(crud, owners[0])
    release = threading.Event()

    def slow(*_: object) -> TokenPair:
        release.wait(5)
        return TokenPair("late", "late")

    with crud._session() as caller:
        account = caller.get(models.Account, owners[0][0])
        account.organization_name = "Uncommitted unrelated work"
        caller.flush()
        try:
            with pytest.raises(TimeoutError):
                crud.rotate(
                    account_id=owners[0][0],
                    grant_id=grant["id"],
                    expected_version=0,
                    refresh=slow,
                    timeout=0.02,
                )
        finally:
            release.set()
        assert caller.in_transaction()
        caller.rollback()

    def fail(*_: object) -> TokenPair:
        raise RuntimeError("Synthetic provider failure")

    with pytest.raises(RuntimeError):
        crud.rotate(
            account_id=owners[0][0],
            grant_id=grant["id"],
            expected_version=0,
            refresh=fail,
        )
    with crud._session() as db:
        assert (
            db.get(models.Account, owners[0][0]).organization_name
            == "Synthetic OAuth tenant"
        )
        row = db.get(models.OAuthToken, grant["id"])
        assert row.rotation_version == 0
        assert decrypt_value(row.access_token_encrypted) == PAIR.access_token
    assert (
        crud.rotate(
            account_id=owners[0][0],
            grant_id=grant["id"],
            expected_version=0,
            refresh=lambda *_: PAIR,
        )["rotation_version"]
        == 1
    )


def test_configuration_replacement_waits_for_rotation_and_invalidates_pending(
    storage: tuple,
) -> None:
    crud, owners = storage
    _, grant, config = active(crud, owners[0])
    binding, pending_grant, _ = pending(crud, owners[0], config)
    entered, release = threading.Event(), threading.Event()

    def refresh(*_: object) -> TokenPair:
        entered.set()
        assert release.wait(5)
        return TokenPair("old-config-rotation", "old-config-refresh")

    def replace() -> dict:
        return crud.replace_configuration(
            account_id=owners[0][0],
            configuration_id=config["id"],
            expected_version=1,
            client_id="replacement",
            client_secret="replacement-secret",
            callback_uri=CALLBACK,
            selected_permissions=[],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        rotation = pool.submit(
            crud.rotate,
            account_id=owners[0][0],
            grant_id=grant["id"],
            expected_version=0,
            refresh=refresh,
        )
        assert entered.wait(5)
        with LockWaiter(crud._engine) as waiter:
            replacement = pool.submit(waiter.run, replace)
            try:
                waiter.wait_until_blocked()
            finally:
                release.set()
        rotation.result(timeout=10)
        assert replacement.result(timeout=10)["version"] == 2
    with pytest.raises(OAuthConflictError):
        crud.complete_connection(**binding, tracker_name="Invalidated")
    with pytest.raises(OAuthConflictError):
        crud.rotate(
            account_id=owners[0][0],
            grant_id=grant["id"],
            expected_version=2,
            refresh=lambda *_: PAIR,
        )
    with crud._session() as db:
        for grant_id in (grant["id"], pending_grant["id"]):
            row = db.get(models.OAuthToken, grant_id)
            assert row.access_token_encrypted == "" and row.status == "invalidated"


def test_expired_cleanup_and_account_delete(storage: tuple) -> None:
    crud, owners = storage
    binding, grant, _ = pending(crud, owners[0])
    with crud._session() as db, db.begin():
        row = db.scalar(
            select(models.OAuthConnectionTransaction).where(
                models.OAuthConnectionTransaction.pending_grant_id == grant["id"]
            )
        )
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(OAuthConflictError):
        crud.complete_connection(**binding, tracker_name="Expired")
    assert crud.cleanup_expired(account_id=owners[1][0]) == 0
    assert crud.cleanup_expired(account_id=owners[0][0]) == 1
    assert crud.get_grant(account_id=owners[0][0], grant_id=grant["id"]) is None
    _, active_grant, _ = active(crud, owners[0])
    with crud._session() as db, db.begin():
        db.execute(delete(models.Account).where(models.Account.id == owners[0][0]))
    assert crud.get_grant(account_id=owners[0][0], grant_id=active_grant["id"]) is None


def test_naive_expiry_rejected_and_pending_tokens_rolled_back(storage: tuple) -> None:
    crud, owners = storage
    config = configuration(crud, owners[0][0])
    tx, state = crud.begin_connection(
        account_id=owners[0][0],
        user_id=owners[0][1],
        session_id="session",
        configuration_id=config["id"],
        return_path="/trackers",
    )
    binding = dict(
        account_id=owners[0][0],
        user_id=owners[0][1],
        session_id="session",
        state=state,
        callback_uri=CALLBACK,
    )
    crud.claim_callback(**binding)
    with pytest.raises(ValueError, match="timezone-aware"):
        crud.store_pending_grant(
            **binding,
            provider_subject="subject",
            tokens=TokenPair("access", expires_at=datetime(2026, 1, 1)),
        )
    with crud._session() as db:
        assert (
            db.get(models.OAuthConnectionTransaction, tx["id"]).pending_grant_id is None
        )


def test_two_trackers_refresh_independently_under_same_consumer(storage: tuple) -> None:
    crud, owners = storage
    _, first, config = active(crud, owners[0])
    _, second, _ = active(crud, owners[0], config)
    barrier = threading.Barrier(2)

    def refresh(*_: object) -> TokenPair:
        barrier.wait(timeout=5)
        return TokenPair("independent-access", "independent-refresh")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                crud.rotate,
                account_id=owners[0][0],
                grant_id=grant["id"],
                expected_version=0,
                refresh=refresh,
            )
            for grant in (first, second)
        ]
        assert [f.result(timeout=10)["rotation_version"] for f in futures] == [1, 1]


def test_disconnect_waits_for_inflight_rotation_and_erases_rotated_pair(
    storage: tuple,
) -> None:
    crud, owners = storage
    _, grant, _ = active(crud, owners[0])
    entered, release = threading.Event(), threading.Event()

    def refresh(*_: object) -> TokenPair:
        entered.set()
        assert release.wait(5)
        return TokenPair("rotated", "rotated")

    with ThreadPoolExecutor(max_workers=2) as pool:
        rotation = pool.submit(
            crud.rotate,
            account_id=owners[0][0],
            grant_id=grant["id"],
            expected_version=0,
            refresh=refresh,
        )
        assert entered.wait(5)
        with LockWaiter(crud._engine) as waiter:
            disconnect = pool.submit(
                waiter.run,
                lambda: crud.disconnect(
                    account_id=owners[0][0], grant_id=grant["id"], expected_version=1
                ),
            )
            try:
                waiter.wait_until_blocked()
            finally:
                release.set()
        rotation.result(timeout=10)
        disconnect.result(timeout=10)
    row = crud.get_grant(account_id=owners[0][0], grant_id=grant["id"])
    assert row["rotation_version"] == 2 and row["status"] == "disconnected"
    with crud._session() as db:
        assert db.get(models.OAuthToken, grant["id"]).access_token_encrypted == ""


def test_pkce_encryption_ttl_and_configuration_version_binding(storage: tuple) -> None:
    crud, owners = storage
    config = configuration(crud, owners[0][0])
    transaction, state = crud.begin_connection(
        account_id=owners[0][0],
        user_id=owners[0][1],
        session_id="session",
        configuration_id=config["id"],
        return_path="/trackers",
        pkce_verifier="synthetic-verifier",
    )
    with crud._session() as db:
        row = db.get(models.OAuthConnectionTransaction, transaction["id"])
        assert row.pkce_verifier_encrypted != "synthetic-verifier"
        assert decrypt_value(row.pkce_verifier_encrypted) == "synthetic-verifier"
        assert (
            590 < (row.expires_at - datetime.now(timezone.utc)).total_seconds() <= 600
        )
        assert row.configuration_version == 1
    crud.replace_configuration(
        account_id=owners[0][0],
        configuration_id=config["id"],
        expected_version=1,
        client_id="new",
        client_secret="new",
        callback_uri=CALLBACK,
        selected_permissions=[],
    )
    with pytest.raises(OAuthConflictError):
        crud.claim_callback(
            account_id=owners[0][0],
            user_id=owners[0][1],
            session_id="session",
            state=state,
            callback_uri=CALLBACK,
        )
    with crud._session() as db:
        row = db.get(models.OAuthConnectionTransaction, transaction["id"])
        assert row.status == "invalidated" and row.pkce_verifier_encrypted is None


def test_disconnected_tracker_cannot_reconnect_to_another_instance(
    storage: tuple,
) -> None:
    crud, owners = storage
    _, grant, _ = active(crud, owners[0])
    crud.disconnect(account_id=owners[0][0], grant_id=grant["id"], expected_version=0)
    config = crud.create_configuration(
        account_id=owners[0][0],
        provider="bitbucket",
        instance="https://other.example.com",
        client_id="synthetic-client",
        client_secret="synthetic-client-secret",
        callback_uri=CALLBACK,
        selected_permissions=[],
    )
    binding, pending_grant, _ = pending(crud, owners[0], config)
    with pytest.raises(OAuthConflictError):
        crud.complete_connection(
            **binding, tracker_name="Wrong instance", tracker_id=grant["tracker_id"]
        )
    row = crud.get_grant(account_id=owners[0][0], grant_id=pending_grant["id"])
    assert row["status"] == "pending" and row["tracker_id"] is None


def test_reconnect_waits_for_rotation_then_replaces_pair_atomically(
    storage: tuple,
) -> None:
    crud, owners = storage
    _, grant, config = active(crud, owners[0])
    binding, _, _ = pending(crud, owners[0], config)
    entered, release = threading.Event(), threading.Event()

    def refresh(*_: object) -> TokenPair:
        entered.set()
        assert release.wait(5)
        return TokenPair("intermediate-rotation", "intermediate-refresh")

    with ThreadPoolExecutor(max_workers=2) as pool:
        rotation = pool.submit(
            crud.rotate,
            account_id=owners[0][0],
            grant_id=grant["id"],
            expected_version=0,
            refresh=refresh,
        )
        assert entered.wait(5)
        with LockWaiter(crud._engine) as waiter:
            reconnect = pool.submit(
                waiter.run,
                lambda: crud.complete_connection(
                    **binding,
                    tracker_name="Concurrent reconnect",
                    tracker_id=grant["tracker_id"],
                    expected_rotation_version=1,
                ),
            )
            try:
                waiter.wait_until_blocked()
            finally:
                release.set()
        assert rotation.result(timeout=10)["rotation_version"] == 1
        assert reconnect.result(timeout=10) == grant["tracker_id"]
    with crud._session() as db:
        row = db.get(models.OAuthToken, grant["id"])
        assert row.rotation_version == 2
        assert decrypt_value(row.access_token_encrypted) == PAIR.access_token
        assert decrypt_value(row.refresh_token_encrypted) == PAIR.refresh_token

    # The old refresh version cannot overwrite the reconnected pair or invoke I/O.
    def stale_provider(*_: object) -> TokenPair:
        pytest.fail("stale rotation must not invoke provider")

    with pytest.raises(OAuthConflictError):
        crud.rotate(
            account_id=owners[0][0],
            grant_id=grant["id"],
            expected_version=1,
            refresh=stale_provider,
        )


@pytest.mark.parametrize("transition", ["reconnect", "disconnect"])
def test_stale_rotation_waiting_on_lifecycle_write_cannot_invoke_provider(
    storage: tuple, transition: str
) -> None:
    crud, owners = storage
    _, grant, config = active(crud, owners[0])
    binding, _, _ = pending(crud, owners[0], config)
    written, release = threading.Event(), threading.Event()
    writer_thread = []

    def hold_uncommitted_write(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if writer_thread == [threading.get_ident()] and statement.startswith(
            "UPDATE oauth_token "
        ):
            written.set()
            assert release.wait(5)

    def lifecycle_write() -> None:
        writer_thread.append(threading.get_ident())
        if transition == "reconnect":
            crud.complete_connection(
                **binding,
                tracker_name="Reconnect",
                tracker_id=grant["tracker_id"],
                expected_rotation_version=0,
            )
        else:
            crud.disconnect(
                account_id=owners[0][0], grant_id=grant["id"], expected_version=0
            )

    def provider(*_: object) -> TokenPair:
        pytest.fail("stale rotation must not reach provider I/O")

    event.listen(crud._engine, "after_cursor_execute", hold_uncommitted_write)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            lifecycle = pool.submit(lifecycle_write)
            assert written.wait(5)
            with LockWaiter(crud._engine) as waiter:
                rotation = pool.submit(
                    waiter.run,
                    lambda: crud.rotate(
                        account_id=owners[0][0],
                        grant_id=grant["id"],
                        expected_version=0,
                        refresh=provider,
                    ),
                )
                try:
                    waiter.wait_until_blocked()
                finally:
                    release.set()
            lifecycle.result(timeout=10)
            with pytest.raises(OAuthConflictError):
                rotation.result(timeout=10)
    finally:
        release.set()
        event.remove(crud._engine, "after_cursor_execute", hold_uncommitted_write)
    row = crud.get_grant(account_id=owners[0][0], grant_id=grant["id"])
    assert row["rotation_version"] == 1
    assert row["status"] == ("active" if transition == "reconnect" else "disconnected")


def test_configuration_replacement_invalidates_completed_callback_and_tracker(
    storage: tuple,
) -> None:
    crud, owners = storage
    binding, grant, config = active(crud, owners[0])
    crud.replace_configuration(
        account_id=owners[0][0],
        configuration_id=config["id"],
        expected_version=1,
        client_id="replacement",
        client_secret="synthetic-client-secret",
        callback_uri=CALLBACK,
        selected_permissions=[],
    )
    with pytest.raises(OAuthConflictError):
        crud.complete_connection(**binding, tracker_name="Stale completed callback")
    with crud._session() as db:
        assert db.get(models.Tracker, grant["tracker_id"]).is_active is False
    new_binding, _, _ = pending(crud, owners[0], config)
    assert (
        crud.complete_connection(
            **new_binding,
            tracker_name="Reconnect",
            tracker_id=grant["tracker_id"],
            expected_rotation_version=1,
        )
        == grant["tracker_id"]
    )
    with crud._session() as db:
        assert db.get(models.Tracker, grant["tracker_id"]).is_active is True


def test_credential_replacement_preserves_disabled_configuration(
    storage: tuple,
) -> None:
    crud, owners = storage
    config = configuration(crud, owners[0][0])
    args = dict(
        account_id=owners[0][0],
        configuration_id=config["id"],
        client_id="replacement",
        client_secret="synthetic",
        callback_uri=CALLBACK,
        selected_permissions=[],
    )
    crud.replace_configuration(**args, expected_version=1, enabled=False)
    assert crud.replace_configuration(**args, expected_version=2)["enabled"] is False
    with pytest.raises(OAuthConflictError):
        crud.begin_connection(
            account_id=owners[0][0],
            user_id=owners[0][1],
            session_id="session",
            configuration_id=config["id"],
            return_path="/trackers",
        )
    assert (
        crud.replace_configuration(**args, expected_version=3, enabled=True)["enabled"]
        is True
    )


def test_unknown_provider_is_rejected_before_persisting_consumer(
    storage: tuple,
) -> None:
    crud, owners = storage
    with pytest.raises(ValueError, match="Unsupported tracker provider"):
        crud.create_configuration(
            account_id=owners[0][0],
            provider="unknown-provider",
            instance="https://example.com",
            client_id="client",
            client_secret="secret",
            callback_uri=CALLBACK,
            selected_permissions=[],
        )
    with crud._session() as db:
        assert (
            db.scalar(
                select(models.OAuthProviderConfiguration).where(
                    models.OAuthProviderConfiguration.account_id == owners[0][0]
                )
            )
            is None
        )
        assert (
            db.scalar(
                select(models.SecretReference).where(
                    models.SecretReference.account_id == owners[0][0]
                )
            )
            is None
        )


def test_cross_tenant_disconnect_is_opaque_and_preserves_grant(storage: tuple) -> None:
    crud, owners = storage
    _, grant, _ = active(crud, owners[0])
    with pytest.raises(OAuthConflictError):
        crud.disconnect(
            account_id=owners[1][0], grant_id=grant["id"], expected_version=0
        )
    assert (
        crud.get_grant(account_id=owners[0][0], grant_id=grant["id"])["status"]
        == "active"
    )
