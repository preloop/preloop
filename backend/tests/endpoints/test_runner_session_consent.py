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


def test_consent_does_not_grant_runner_access(db_session, test_user, service):
    runner = _runner(db_session, test_user)
    member = _member(db_session, test_user, "Jane Doe")
    response = _client(db_session, member).post(
        f"/api/v1/runners/{runner.id}/sessions",
        json=_start_body(consent_request_id=str(uuid4())),
    )
    assert response.status_code == 403
    assert not service.plans
    assert db_session.query(models.OwnerConsent).count() == 0


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
