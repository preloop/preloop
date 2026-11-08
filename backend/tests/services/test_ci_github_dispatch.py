"""GitHub repository_dispatch completion sink for restricted CI identities."""

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.auth.ci import get_current_actor
from preloop.api.endpoints import ci_identities, event_webhooks
from preloop.api.middleware import ci_auth
from preloop.models import crud, models
from preloop.models.crud.base import CRUDBase
from preloop.models.crud.ci_subscription import (
    _binding,
    github_api_base,
    github_dispatch_url,
)
from preloop.models.db.session import get_db_session
from preloop.plugins.ci_authorization import register_ci_administrator
from preloop.schemas.ci_subscription import CiSubscriptionUpdate
from preloop.services.event_webhooks import github_dispatch, outbox, worker
from preloop.services.event_webhooks.signing import verify_signature
from tests.api.test_ci_execution import review_binding
from tests.api.test_ci_principal import ci_resources as make_resources

BASE = "/api/v1/ci-identities"
TOKEN = "ghs_" + "S" * 36
FINISHED = "flow.execution.finished"
DISPATCH_URL = "https://api.github.com/repos/example/repository/dispatches"


@pytest.fixture(autouse=True)
def authority() -> Any:
    register_ci_administrator(None)
    yield
    register_ci_administrator(None)


@pytest.fixture
def ci_resources(db_session: Session) -> tuple[Any, ...]:
    return make_resources.__wrapped__(db_session)


@pytest.fixture
def client(
    db_session: Session, ci_resources: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    def sessions() -> Any:
        yield db_session

    app = FastAPI()
    app.include_router(ci_identities.router, prefix="/api/v1")
    app.include_router(event_webhooks.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = sessions
    app.dependency_overrides[get_current_active_user] = lambda: ci_resources[0]
    app.dependency_overrides[get_current_actor] = lambda: ci_resources[0]
    monkeypatch.setattr(ci_auth, "get_auth_db_session", sessions)
    app.add_middleware(ci_auth.RestrictedCiAuthMiddleware)
    return TestClient(app)


def identity(client: TestClient, resources: tuple[Any, ...]) -> dict[str, Any]:
    from datetime import datetime, timedelta, timezone

    response = client.post(
        BASE,
        json={
            "name": "Synthetic review",
            "grant": resources[3].model_dump(mode="json"),
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def dispatch_subscription(
    client: TestClient, resources: tuple[Any, ...]
) -> tuple[dict[str, Any], dict[str, Any]]:
    own = identity(client, resources)
    response = client.post(
        f"{BASE}/{own['identity']['id']}/github-dispatch-subscriptions",
        json={
            "key_id": own["key_id"],
            "event_type": "preloop-review-completed",
            "token": TOKEN,
        },
    )
    assert response.status_code == 201, response.text
    return own, response.json()


def finished_execution(db: Session, token: str, result: Any) -> models.FlowExecution:
    context = crud.crud_ci_principal.authenticate(db, token=token)
    binding = review_binding(context).model_copy(update={"base_sha": "b" * 40})
    execution = crud.crud_ci_execution.create(
        db, context=context, binding=binding, event={}
    )
    CRUDBase(models.FlowExecution).update(
        db, db_obj=execution, obj_in={"status": "SUCCEEDED", "result": result}
    )
    return execution


# --- target derivation -------------------------------------------------------


@pytest.mark.parametrize(
    "tracker_url,expected",
    [
        (None, "https://api.github.com"),
        ("https://github.com", "https://api.github.com"),
        ("https://api.github.com/", "https://api.github.com"),
        ("https://git.example.com", "https://git.example.com/api/v3"),
        ("https://git.example.com/api/v3", "https://git.example.com/api/v3"),
    ],
)
def test_api_base_follows_bound_integration(
    tracker_url: str | None, expected: str
) -> None:
    assert github_api_base(tracker_url) == expected


@pytest.mark.parametrize(
    "tracker_url",
    [
        "http://git.example.com",
        "https://user:pw@git.example.com",
        "https://git.example.com?x=1",
    ],
)
def test_api_base_rejects_unsafe_integration_urls(tracker_url: str) -> None:
    with pytest.raises(ValueError):
        github_api_base(tracker_url)


# --- human-only configuration ------------------------------------------------


def test_human_creates_dispatch_sink_without_exposing_token(
    db_session: Session, ci_resources: tuple[Any, ...], client: TestClient
) -> None:
    own, created = dispatch_subscription(client, ci_resources)
    assert created["delivery_kind"] == "github_dispatch"
    assert created["dispatch_event_type"] == "preloop-review-completed"
    assert created["dispatch_credential_configured"] is True
    assert created["url"] == DISPATCH_URL
    assert created["secret"].startswith("whsec_")
    assert TOKEN not in json.dumps(created)
    endpoint = CRUDBase(models.WebhookEndpoint).get(db_session, id=created["id"])
    assert endpoint.dispatch_token_encrypted and TOKEN not in (
        endpoint.dispatch_token_encrypted
    )
    listing = client.get("/api/v1/event-webhooks/endpoints")
    assert listing.status_code == 200, listing.text
    assert TOKEN not in listing.text
    audit = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "ci_identity_dispatch_subscription_create")
        .all()
    )
    assert len(audit) == 1 and TOKEN not in json.dumps(audit[0].details)
    assert str(endpoint.ci_principal_id) == own["identity"]["id"]


@pytest.mark.parametrize(
    "override",
    [
        {"token": "short"},
        {"token": "ghp_" + "x" * 30 + " spaces"},
        {"event_type": ""},
        {"event_type": "x" * 101},
        {"url": "https://attacker.example.com/"},
    ],
)
def test_dispatch_settings_are_validated(
    ci_resources: tuple[Any, ...], client: TestClient, override: dict[str, Any]
) -> None:
    own = identity(client, ci_resources)
    body = {"key_id": own["key_id"], "token": TOKEN, **override}
    response = client.post(
        f"{BASE}/{own['identity']['id']}/github-dispatch-subscriptions", json=body
    )
    assert response.status_code == 422
    assert TOKEN not in response.text


def test_machine_key_cannot_configure_dispatch(
    ci_resources: tuple[Any, ...], client: TestClient
) -> None:
    own = identity(client, ci_resources)
    response = client.post(
        f"{BASE}/{own['identity']['id']}/github-dispatch-subscriptions",
        headers={"Authorization": f"Bearer {own['token']}"},
        json={"key_id": own["key_id"], "token": TOKEN},
    )
    assert response.status_code in (401, 403)


def test_dispatch_target_cannot_be_redirected(
    db_session: Session, ci_resources: tuple[Any, ...], client: TestClient
) -> None:
    own, created = dispatch_subscription(client, ci_resources)
    human = client.patch(
        f"/api/v1/event-webhooks/endpoints/{created['id']}",
        json={"url": "https://attacker.example.com/hook"},
    )
    assert human.status_code == 422
    context = crud.crud_ci_principal.authenticate(db_session, token=own["token"])
    with pytest.raises(ValueError):
        crud.crud_ci_subscription.update_owned(
            db_session,
            context=context,
            endpoint_id=created["id"],
            payload=CiSubscriptionUpdate(url="https://attacker.example.com/hook"),
        )
    db_session.rollback()
    endpoint = CRUDBase(models.WebhookEndpoint).get(db_session, id=created["id"])
    # Even a row edited behind the API is sent to the bound repository.
    endpoint.url = "https://attacker.example.com/hook"
    db_session.commit()
    assert github_dispatch_url(_binding(endpoint)) == DISPATCH_URL


def test_human_replaces_dispatch_token(
    db_session: Session, ci_resources: tuple[Any, ...], client: TestClient
) -> None:
    from preloop.utils.encryption import decrypt_value

    own, created = dispatch_subscription(client, ci_resources)
    replacement = "github_pat_" + "R" * 40
    response = client.put(
        f"{BASE}/{own['identity']['id']}/github-dispatch-subscriptions/"
        f"{created['id']}/token",
        json={"token": replacement},
    )
    assert response.status_code == 200, response.text
    assert replacement not in response.text
    endpoint = CRUDBase(models.WebhookEndpoint).get(db_session, id=created["id"])
    db_session.refresh(endpoint)
    assert decrypt_value(endpoint.dispatch_token_encrypted) == replacement
    missing = client.put(
        f"{BASE}/{own['identity']['id']}/github-dispatch-subscriptions/{uuid4()}/token",
        json={"token": replacement},
    )
    assert missing.status_code == 404


# --- delivery ----------------------------------------------------------------


def test_dispatch_delivery_carries_signed_binding(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    own, created = dispatch_subscription(client, ci_resources)
    execution = finished_execution(
        db_session, own["token"], {"review": "persisted review"}
    )
    enqueued = outbox.enqueue_event(
        db_session,
        account_id=ci_resources[0].account_id,
        event_type=FINISHED,
        data={"ignored": True},
        natural_key=f"{FINISHED}:{execution.id}:SUCCEEDED",
        subject_id=execution.id,
    )
    db_session.commit()
    assert len(enqueued.delivery_ids) == 1
    rows = crud.crud_ci_subscription.prepare_delivery(
        db_session, delivery_id=enqueued.delivery_ids[0]
    )
    assert rows is not None
    prepared = worker.prepare_claimed(*rows)
    assert prepared is not None
    assert prepared.url == DISPATCH_URL
    assert TOKEN not in repr(prepared)

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(204)

    async def send() -> worker.AttemptOutcome:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await worker.post_delivery(http, prepared)

    outcome = asyncio.run(send())
    assert outcome.success and outcome.response_status == 204
    request = captured["request"]
    assert str(request.url) == DISPATCH_URL
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
    body = json.loads(request.content)
    assert body["event_type"] == "preloop-review-completed"
    payload = body["client_payload"]
    assert set(payload) == {"event", "signature", "event_id", "delivery_id", "attempt"}
    assert len(payload) <= 10
    assert verify_signature(
        created["secret"], payload["signature"], payload["event"].encode()
    )
    envelope = json.loads(payload["event"])
    assert envelope["type"] == FINISHED and envelope["id"] == payload["event_id"]
    data = envelope["data"]
    assert data["execution_id"] == str(execution.id)
    assert data["pr_number"] == 7
    assert data["head_sha"] == "a" * 40
    assert data["base_sha"] == "b" * 40
    assert data["repository_slug"] == "example/repository"
    assert data["result_ready"] is True
    assert re.fullmatch(r"[0-9a-f]{64}", data["result_sha256"])
    assert "persisted review" not in request.content.decode()
    assert TOKEN not in request.content.decode()


@pytest.mark.parametrize("status_code", [401, 404, 422, 500])
def test_rejected_dispatch_is_a_failed_attempt_without_token(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    status_code: int,
) -> None:
    own, _ = dispatch_subscription(client, ci_resources)
    execution = finished_execution(db_session, own["token"], {"review": "x"})
    enqueued = outbox.enqueue_event(
        db_session,
        account_id=ci_resources[0].account_id,
        event_type=FINISHED,
        data={},
        natural_key=f"{FINISHED}:{execution.id}:SUCCEEDED",
        subject_id=execution.id,
    )
    db_session.commit()
    rows = crud.crud_ci_subscription.prepare_delivery(
        db_session, delivery_id=enqueued.delivery_ids[0]
    )
    prepared = worker.prepare_claimed(*rows)

    async def send() -> worker.AttemptOutcome:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(status_code, text=TOKEN)
        )
        async with httpx.AsyncClient(transport=transport) as http:
            return await worker.post_delivery(http, prepared)

    outcome = asyncio.run(send())
    assert not outcome.success and outcome.response_status == status_code
    assert TOKEN not in (outcome.error or "")
    # The outcome goes through the shared retry, dead-letter and breaker path.
    worker.apply_outcomes(db_session, [outcome])
    delivery = db_session.get(models.WebhookDelivery, enqueued.delivery_ids[0])
    db_session.refresh(delivery)
    assert delivery.attempt_count == 1 and delivery.status == "pending"
    assert TOKEN not in (delivery.last_error or "")


def test_dispatch_payload_limits() -> None:
    with pytest.raises(ValueError, match="size"):
        github_dispatch.request_body("e", {"event": "x" * 70_000})
    with pytest.raises(ValueError, match="keys"):
        github_dispatch.request_body("e", {str(i): i for i in range(11)})
    with pytest.raises(ValueError, match="event_type"):
        github_dispatch.request_body("", {})


def test_dispatch_credential_is_read_only_by_the_delivery_path() -> None:
    """No execution, runner or agent code can reach the dispatch credential."""
    root = Path(__file__).resolve().parents[2] / "preloop"
    readers = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "dispatch_token_encrypted" in path.read_text(encoding="utf-8")
    }
    assert readers == {
        "api/endpoints/event_webhooks.py",
        "models/alembic/versions/20261205_ci_dispatch_sink.py",
        "models/crud/ci_subscription.py",
        "models/models/webhook_endpoint.py",
        "services/event_webhooks/worker.py",
    }, readers
