"""Direct runner session starts never bypass the runner owner's consent."""

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from preloop.api.endpoints import runner_sessions
from preloop.models import models
from preloop.services import owner_consent
from tests.endpoints.test_runner_sessions_api import (
    FakeService,
    _client,
    _member,
    _runner,
    _start_body,
    _tracker,
)


@pytest.fixture
def service():
    fake = FakeService()
    runner_sessions.register_runner_session_service(fake)
    yield fake
    runner_sessions.register_runner_session_service(None)


@pytest.fixture
def admin(db_session, test_user):
    user = _member(db_session, test_user, "Jane Doe")
    user.is_superuser = True
    db_session.flush()
    return user


@pytest.fixture(autouse=True)
def no_owner_notifications(monkeypatch):
    monkeypatch.setattr(owner_consent, "notify_owner_consent", AsyncMock())


def _resolve(db_session, approval_id: str, state: str) -> models.ApprovalRequest:
    approval = db_session.get(models.ApprovalRequest, UUID(approval_id))
    approval.status = state
    db_session.commit()
    return approval


def test_owner_start_needs_no_consent_token(db_session, test_user, service):
    runner = _runner(db_session, test_user)
    response = _client(db_session, test_user).post(
        f"/api/v1/runners/{runner.id}/sessions", json=_start_body()
    )
    assert response.status_code == 202, response.text
    assert len(service.plans) == 1
    assert db_session.query(models.OwnerConsent).count() == 0


def test_admin_needs_explicit_request_token(db_session, test_user, admin, service):
    runner = _runner(db_session, test_user)
    response = _client(db_session, admin).post(
        f"/api/v1/runners/{runner.id}/sessions", json=_start_body()
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "consent_request_id_required"
    assert not service.plans


def test_admin_start_waits_then_uses_approval_once(
    db_session, test_user, admin, service
):
    runner = _runner(db_session, test_user)
    path = f"/api/v1/runners/{runner.id}/sessions"
    body = _start_body(consent_request_id=str(uuid4()))
    client = _client(db_session, admin)
    pending = client.post(path, json=body)
    assert pending.status_code == 409, pending.text
    detail = pending.json()["detail"]
    assert detail["status"] == "pending"
    assert detail["message"] == "Waiting for runner owner"
    assert not service.plans
    again = client.post(path, json=body)
    assert again.json()["detail"]["approval_id"] == detail["approval_id"]
    approval = _resolve(db_session, detail["approval_id"], "approved")
    assert approval.managed_agent_id is None
    assert approval.approval_workflow.approver_user_ids == [test_user.id]
    assert client.post(path, json=body).status_code == 202
    assert len(service.plans) == 1
    replay = client.post(path, json=body)
    assert replay.status_code == 403
    assert replay.json()["detail"]["status"] == "consumed"
    assert len(service.plans) == 1


@pytest.mark.parametrize("state", ["declined", "expired"])
def test_admin_cannot_start_after_owner_refusal(
    db_session, test_user, admin, service, state
):
    runner = _runner(db_session, test_user)
    path = f"/api/v1/runners/{runner.id}/sessions"
    body = _start_body(consent_request_id=str(uuid4()))
    client = _client(db_session, admin)
    pending = client.post(path, json=body)
    _resolve(db_session, pending.json()["detail"]["approval_id"], state)
    response = client.post(path, json=body)
    assert response.status_code == 403
    assert response.json()["detail"]["status"] == state
    assert not service.plans


def test_approved_start_does_not_cover_changed_request(
    db_session, test_user, admin, service
):
    runner = _runner(db_session, test_user)
    path = f"/api/v1/runners/{runner.id}/sessions"
    body = _start_body(consent_request_id=str(uuid4()))
    client = _client(db_session, admin)
    pending = client.post(path, json=body)
    approval_id = pending.json()["detail"]["approval_id"]
    _resolve(db_session, approval_id, "approved")
    changed = client.post(path, json={**body, "first_prompt": "Another task"})
    assert changed.status_code == 409
    assert changed.json()["detail"]["approval_id"] != approval_id
    assert not service.plans


def test_consent_does_not_grant_runner_access(
    db_session, test_user, service, monkeypatch
):
    """With the RBAC plugin, a member without manage_account is refused."""
    from preloop.utils import permissions

    monkeypatch.setattr(permissions, "_plugin_require_permission", object())
    runner = _runner(db_session, test_user)
    member = _member(db_session, test_user, "Jane Doe")
    response = _client(db_session, member).post(
        f"/api/v1/runners/{runner.id}/sessions",
        json=_start_body(consent_request_id=str(uuid4())),
    )
    assert response.status_code == 403
    assert not service.plans
    assert db_session.query(models.OwnerConsent).count() == 0


def test_core_member_still_needs_owner_consent(db_session, test_user, service):
    """Core has no roles, but a member still waits for the owner's consent."""
    runner = _runner(db_session, test_user)
    member = _member(db_session, test_user, "Jane Doe")
    response = _client(db_session, member).post(
        f"/api/v1/runners/{runner.id}/sessions",
        json=_start_body(consent_request_id=str(uuid4())),
    )
    assert response.status_code == 409, response.text
    assert not service.plans


def test_admin_cannot_start_runner_without_owner(db_session, test_user, admin, service):
    runner = _runner(db_session, test_user)
    runner.registered_by_user_id = None
    db_session.flush()
    response = _client(db_session, admin).post(
        f"/api/v1/runners/{runner.id}/sessions",
        json=_start_body(consent_request_id=str(uuid4())),
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "runner_owner_required"
    assert not service.plans


def test_pending_consent_never_mints_checkout_credentials(
    db_session, test_user, admin, service, monkeypatch
):
    runner = _runner(db_session, test_user)
    tracker = _tracker(db_session, test_user, "github")

    def forbidden_mint(*args, **kwargs):
        pytest.fail("credential was minted before owner approval")

    monkeypatch.setattr(runner_sessions, "mint_checkout_credential", forbidden_mint)
    response = _client(db_session, admin).post(
        f"/api/v1/runners/{runner.id}/sessions",
        json=_start_body(
            consent_request_id=str(uuid4()),
            workspace={
                "kind": "tracker_checkout",
                "tracker_id": str(tracker.id),
                "repository": "acme/api",
            },
        ),
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["status"] == "pending"
    assert not service.plans


def test_invalid_request_does_not_request_consent(
    db_session, test_user, admin, service
):
    runner = _runner(db_session, test_user)
    response = _client(db_session, admin).post(
        f"/api/v1/runners/{runner.id}/sessions",
        json=_start_body(
            consent_request_id=str(uuid4()),
            workspace={"kind": "authorized_directory", "id": "missing-directory"},
        ),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "workspace_not_authorized"
    assert db_session.query(models.OwnerConsent).count() == 0
    assert not service.plans


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision,notification_failure",
    [("pending", False), ("approved", False), ("pending", True)],
)
async def test_consent_does_not_require_a_second_worker_token(
    monkeypatch: pytest.MonkeyPatch, decision: str, notification_failure: bool
) -> None:
    """A saturated sync-route pool can still finish consent DB work."""
    import asyncio
    import threading
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import anyio
    from fastapi import HTTPException

    from preloop.models.crud import owner_consent as consent_crud

    consent = owner_consent.OwnerConsentResult(
        status=decision, approval_id=uuid4(), created=decision == "pending"
    )
    worker_ids: list[int] = []
    database_ids: list[int] = []

    def check(*args: object, **kwargs: object) -> owner_consent.OwnerConsentResult:
        database_ids.append(threading.get_ident())
        return consent

    def consume(*args: object, **kwargs: object) -> bool:
        database_ids.append(threading.get_ident())
        return True

    monkeypatch.setattr(owner_consent, "check_owner_consent", check)
    monkeypatch.setattr(consent_crud, "consume", consume)
    notifications = AsyncMock(
        side_effect=OSError("notification unavailable")
        if notification_failure
        else None
    )
    monkeypatch.setattr(owner_consent, "notify_owner_consent", notifications)
    runner = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        registered_by_user_id=uuid4(),
        name="Example runner",
    )
    user = SimpleNamespace(id=uuid4(), account_id=runner.account_id)
    body = runner_sessions.RunnerSessionStartRequest(
        consent_request_id=uuid4(),
        harness="claude_code",
        workspace={"kind": "authorized_directory", "id": "example-workspace"},
    )

    def route_worker() -> int:
        worker_ids.append(threading.get_ident())
        try:
            runner_sessions._require_start_consent(
                MagicMock(), user, runner, body, MagicMock()
            )
        except HTTPException as exc:
            return exc.status_code
        return 202

    limiter = anyio.to_thread.current_default_thread_limiter()
    previous_tokens = limiter.total_tokens
    limiter.total_tokens = 1
    worker = asyncio.create_task(anyio.to_thread.run_sync(route_worker))
    timed_out = False
    try:
        try:
            result = await asyncio.wait_for(asyncio.shield(worker), timeout=2)
        except TimeoutError:
            timed_out = True
    finally:
        # Let a regressed nested offload drain rather than strand a thread
        # after the assertion. The route is required to finish with ONE token.
        if not worker.done():
            limiter.total_tokens = 2
        await worker
        limiter.total_tokens = previous_tokens
    assert not timed_out, "Consent waited for a second worker token"
    assert result == (409 if decision == "pending" else 202)
    assert database_ids == worker_ids * (2 if decision == "approved" else 1)
    assert notifications.await_count == (1 if decision == "pending" else 0)
