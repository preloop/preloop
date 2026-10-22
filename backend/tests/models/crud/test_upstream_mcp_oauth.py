"""Real PostgreSQL worker isolation and atomic upstream OAuth lifecycle tests."""

import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker

from preloop.models import models
from preloop.models.crud import crud_mcp_server
from preloop.models.crud.upstream_mcp_oauth import (
    CRUDUpstreamMCPOAuth,
    OAuthContextError,
)
from preloop.services.upstream_mcp_oauth import (
    OAuthHTTP,
    OAuthTokenError,
    UpstreamMCPOAuthService,
)

RESOURCE = "https://resource.example.com/mcp"
ISSUER = "https://identity.example.com"
CALLBACK = "https://console.example.com/api/v1/mcp-servers/oauth/callback"
CONFIG = {
    "issuer": ISSUER,
    "resource": RESOURCE,
    "client_id": "synthetic-client",
    "client_secret": "synthetic-client-secret",
    "token_endpoint": ISSUER + "/token",
    "authorization_endpoint": ISSUER + "/authorize",
    "redirect_uri": CALLBACK,
}


@pytest.fixture
def storage():
    engine = create_engine(os.environ["DATABASE_URL"])
    factory = sessionmaker(engine)
    with factory() as db:
        account = models.Account(organization_name="Synthetic MCP OAuth")
        db.add(account)
        db.flush()
        user = models.User(
            account_id=account.id,
            username=str(uuid4()),
            email=f"{uuid4()}@example.com",
            hashed_password="synthetic",
            is_active=True,
        )
        db.add(user)
        db.flush()
        server = crud_mcp_server.create(
            db,
            obj_in=dict(
                account_id=account.id,
                name="Synthetic OAuth",
                url=RESOURCE,
                auth_type="oauth",
                auth_config=CONFIG,
                status="active",
            ),
        )
        ids = account.id, user.id, server.id
    yield factory, ids
    with factory() as db:
        crud_mcp_server.remove(db, id=ids[2], account_id=str(ids[0]))
        db.execute(delete(models.User).where(models.User.id == ids[1]))
        db.execute(delete(models.Account).where(models.Account.id == ids[0]))
        db.commit()
    engine.dispose()


def begin(storage):
    factory, (account, user, server) = storage
    store = CRUDUpstreamMCPOAuth()
    with factory() as db:
        snapshot = store.setup(db, server, account)
        store.begin(
            db,
            snapshot,
            user_id=user,
            state="synthetic-state",
            browser_nonce="synthetic-browser",
            verifier="synthetic-verifier",
            config=CONFIG,
        )
    return store


def consume(store, db, **overrides):
    return store.consume(
        db,
        **{
            "state": "synthetic-state",
            "browser_nonce": "synthetic-browser",
            "redirect_uri": CALLBACK,
            **overrides,
        },
    )


def grant(storage, **overrides):
    store = begin(storage)
    factory, _ = storage
    with factory() as db:
        snapshot = consume(store, db)
        assert store.complete(
            db,
            snapshot,
            {
                **CONFIG,
                "access_token": "synthetic-old-access",
                "refresh_token": "synthetic-old-refresh",
                "expires_at": time.time() - 1,
                **overrides,
            },
        )
    return store


def test_cross_worker_authorization_is_encrypted_and_single_use(storage):
    begin(storage)
    factory, (_, _, server) = storage
    with factory() as db:
        row = db.query(models.UpstreamMCPOAuthTransaction).one()
        assert "synthetic" not in row.context_encrypted
        assert row.state_hash != "synthetic-state"
        assert row.to_dict().keys() == {"expires_at"}
        snapshot = consume(CRUDUpstreamMCPOAuth(), db)
        assert snapshot.verifier == "synthetic-verifier"
        assert "synthetic-client-secret" not in repr(snapshot)
        assert CRUDUpstreamMCPOAuth().complete(
            db, snapshot, {**CONFIG, "access_token": "synthetic-access"}
        )
        stored = db.get(models.MCPServer, server)
        assert "access_token" not in stored.auth_config
        assert "client_secret" not in stored.auth_config
        assert CRUDUpstreamMCPOAuth().state(db, stored) == "connected"
    with factory() as db, pytest.raises(OAuthContextError):
        consume(CRUDUpstreamMCPOAuth(), db)


def test_concurrent_consumption_has_one_winner(storage):
    begin(storage)
    factory, _ = storage
    barrier = threading.Barrier(2)

    def worker():
        with factory() as db:
            barrier.wait()
            try:
                return consume(CRUDUpstreamMCPOAuth(), db)
            except OAuthContextError:
                return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert sum(result is not None for result in results) == 1


@pytest.mark.parametrize(
    "override",
    [
        {"browser_nonce": "wrong"},
        {"account_id": uuid4()},
        {"server_id": uuid4()},
        {"issuer": "https://other.example.com"},
        {"redirect_uri": CALLBACK + "/wrong"},
    ],
)
def test_mismatched_binding_cannot_consume(storage, override):
    store = begin(storage)
    with storage[0]() as db, pytest.raises(OAuthContextError):
        consume(store, db, **override)
    with storage[0]() as db:
        assert consume(store, db).server_id == storage[1][2]


def test_expired_and_revoked_principal_cannot_consume(storage):
    store = begin(storage)
    with storage[0]() as db:
        transaction = db.query(models.UpstreamMCPOAuthTransaction).one()
        transaction.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        with pytest.raises(OAuthContextError):
            consume(store, db)
        db.rollback()
        assert store.cleanup(db) == 1
        db.commit()
    begin(storage)
    with storage[0]() as db:
        db.get(models.User, storage[1][1]).auth_generation = 1
        db.commit()
        with pytest.raises(OAuthContextError):
            consume(store, db)


@pytest.mark.parametrize("action", ["replacement", "remove", "disconnect"])
def test_configuration_change_invalidates_pending_state(storage, action):
    store = begin(storage)
    factory, (account, _, server) = storage
    with factory() as db:
        if action == "replacement":
            crud_mcp_server.update(
                db,
                db_obj=db.get(models.MCPServer, server),
                obj_in={"auth_config": {**CONFIG, "client_id": "replacement"}},
            )
        elif action == "remove":
            crud_mcp_server.remove(db, id=server, account_id=str(account))
        else:
            store.disconnect(db, server, account)
    with factory() as db, pytest.raises(OAuthContextError):
        consume(store, db)


@pytest.mark.asyncio
async def test_two_workers_refresh_once_and_rotate_atomically(storage):
    grant(storage, expires_at=time.time() + 30)
    factory, (_, _, server) = storage
    calls = []

    class Provider(OAuthHTTP):
        async def post(self, url, **kwargs):
            calls.append(kwargs["data"])
            await asyncio.sleep(0.1)
            return {
                "access_token": "synthetic-new-access",
                "refresh_token": "synthetic-new-refresh",
                "expires_in": 3600,
            }

    services = [
        UpstreamMCPOAuthService(session_factory=factory, http_factory=Provider)
        for _ in range(2)
    ]
    results = await asyncio.gather(
        *(service.credentials(server) for service in services)
    )
    assert len(calls) == 1
    assert {result.version for result in results} == {results[0].version}
    assert all(
        result.config["access_token"] == "synthetic-new-access"
        and result.config["refresh_token"] == "synthetic-new-refresh"
        for result in results
    )
    with factory() as db:
        row = db.query(models.UpstreamMCPOAuthConnection).one()
        assert row.lease_id is None
        secret = db.get(models.SecretReference, row.secret_id)
        assert "synthetic-new-access" not in secret.encrypted_value
        assert "synthetic-new-refresh" not in str(
            db.get(models.MCPServer, server).auth_config
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expires,refresh,expected",
    [
        (None, None, "connected"),
        (time.time() + 3600, None, "connected"),
        (time.time() - 1, None, "reconnect_required"),
    ],
)
async def test_expiry_and_missing_refresh(storage, expires, refresh, expected):
    grant(storage, expires_at=expires, refresh_token=refresh)
    service = UpstreamMCPOAuthService(session_factory=storage[0])
    if expected == "connected":
        assert (await service.credentials(storage[1][2])).lease_id is None
    else:
        with pytest.raises(OAuthContextError, match="expired"):
            await service.credentials(storage[1][2])


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after", [None, 20])
async def test_failed_refresh_releases_lease_without_blind_retry(storage, retry_after):
    grant(storage)
    calls = []

    class Provider(OAuthHTTP):
        async def post(self, url, **kwargs):
            calls.append(1)
            raise OAuthTokenError("Synthetic failure", retry_after=retry_after)

    service = UpstreamMCPOAuthService(session_factory=storage[0], http_factory=Provider)
    for _ in range(2):
        with pytest.raises(ValueError):
            await service.credentials(storage[1][2])
    assert calls == [1]
    with storage[0]() as db:
        row = db.query(models.UpstreamMCPOAuthConnection).one()
        assert row.lease_id is None
        assert row.state == ("connected" if retry_after else "reconnect_required")


def test_abandoned_refresh_does_not_reuse_possibly_consumed_token(storage):
    store = grant(storage)
    with storage[0]() as db:
        assert store.prepare(db, storage[1][2]).lease_id
        row = db.query(models.UpstreamMCPOAuthConnection).one()
        row.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        with pytest.raises(OAuthContextError):
            store.prepare(db, storage[1][2])
        assert db.query(models.UpstreamMCPOAuthConnection).one().lease_id is None


def test_refresh_cannot_overwrite_reconnected_credential(storage):
    store = grant(storage)
    with storage[0]() as db:
        old = store.prepare(db, storage[1][2])
    # A new authorization replaces the version while the old request is in flight.
    grant(storage, access_token="reconnected", expires_at=time.time() + 3600)
    with storage[0]() as db:
        assert not store.complete(db, old, {**CONFIG, "access_token": "stale"})
        assert store.prepare(db, storage[1][2]).config["access_token"] == "reconnected"


def test_stale_redacted_edit_preserves_newer_rotated_tokens(storage):
    from preloop.models.schemas.mcp_server import redact_auth_config

    store = grant(storage)
    with storage[0]() as db:
        server = db.get(models.MCPServer, storage[1][2])
        echoed = redact_auth_config(store.config_for_edit(db, server))
        snapshot = store.prepare(db, server.id)
        store.complete(
            db,
            snapshot,
            {
                **CONFIG,
                "access_token": "rotated-access",
                "refresh_token": "rotated-refresh",
                "expires_at": time.time() + 3600,
            },
        )
        # The edit was based on the previous version's markers, not its secrets.
        server = crud_mcp_server.update(
            db, db_obj=server, obj_in={"auth_config": echoed}
        )
        config = store.config_for_edit(db, server)
        assert config["access_token"] == "rotated-access"
        assert config["refresh_token"] == "rotated-refresh"
        assert config["expires_at"] > time.time() + 3500


def test_setup_and_disconnect_are_account_scoped(storage):
    store = grant(storage, expires_at=time.time() + 3600)
    for method in ("setup", "disconnect"):
        with storage[0]() as db, pytest.raises(OAuthContextError):
            getattr(store, method)(db, storage[1][2], uuid4())
    with storage[0]() as db:
        assert (
            store.prepare(db, storage[1][2]).config["access_token"]
            == "synthetic-old-access"
        )


def test_nested_introspection_secrets_stay_encrypted(storage):
    store = grant(
        storage,
        expires_at=time.time() + 3600,
        introspection={
            "endpoint": ISSUER + "/introspect",
            "client_id": "synthetic",
            "client_secret": "synthetic-introspection-secret",
        },
    )
    with storage[0]() as db:
        server = db.get(models.MCPServer, storage[1][2])
        assert "synthetic-introspection-secret" not in str(server.auth_config)
        assert (
            store.prepare(db, server.id).config["introspection"]["client_secret"]
            == "synthetic-introspection-secret"
        )


def test_name_edit_does_not_revive_rejected_refresh(storage):
    store = grant(storage)
    with storage[0]() as db:
        lease = store.prepare(db, storage[1][2])
        store.fail(db, lease)
        server = db.get(models.MCPServer, storage[1][2])
        crud_mcp_server.update(
            db, db_obj=server, obj_in={"name": "Renamed", "url": RESOURCE}
        )
        assert store.state(db, server) == "reconnect_required"
        with pytest.raises(OAuthContextError):
            store.prepare(db, server.id)


def test_oauth_json_writes_never_contain_plaintext_even_during_flush(storage):
    from sqlalchemy import event

    factory, (account, _, server_id) = storage
    writes = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith(("UPDATE mcp_server ", "INSERT INTO mcp_server ")):
            writes.append(str(parameters))

    engine = factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with factory() as db:
            server = db.get(models.MCPServer, server_id)
            crud_mcp_server.update(
                db,
                db_obj=server,
                obj_in={
                    "auth_config": {
                        **CONFIG,
                        "client_secret": "synthetic-replacement-secret",
                    }
                },
            )
            extra = crud_mcp_server.create(
                db,
                obj_in={
                    "account_id": account,
                    "name": "Other",
                    "url": RESOURCE,
                    "auth_type": "oauth",
                    "auth_config": {**CONFIG, "access_token": "synthetic-new-secret"},
                },
            )
            crud_mcp_server.remove(db, id=extra.id, account_id=str(account))
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert writes
    assert all(
        "synthetic-replacement-secret" not in write
        and "synthetic-new-secret" not in write
        and "synthetic-client-secret" not in write
        for write in writes
    )
