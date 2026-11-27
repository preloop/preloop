"""Flow ingress credentials are write-only and can be rotated independently."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pytest_mock import MockerFixture

from preloop.api.auth import get_current_active_user
from preloop.api.auth.ci import get_current_actor
from preloop.api.endpoints import flows
from preloop.models import schemas

BASE = "/api/v1/flows"
SECRET = "synthetic-existing-webhook-secret"


@pytest.fixture
def hook(mocker: MockerFixture) -> Iterator[SimpleNamespace]:
    """Serve the real flow routes with an account-scoped CRUD boundary."""
    account_id = uuid4()
    user = SimpleNamespace(id=uuid4(), account_id=account_id, is_superuser=False)
    row = SimpleNamespace(
        **schemas.FlowCreate(
            name="Inbound events",
            prompt_template="Handle {{trigger_event.payload}}",
            agent_config={},
            trigger_event_source="webhook",
            trigger_event_types=["webhook"],
            webhook_config={
                "webhook_secret": SECRET,
                "employee_secret": "synthetic-employee-secret-at-least-32-chars",
                "dedupe_path": "event.id",
                "supersede_on_update": True,
            },
        ).model_dump(),
        id=uuid4(),
        webhook_secret_rotated_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    row.account_id = account_id
    crud = mocker.patch.object(flows, "crud_flow")

    def get(*args: Any, **kwargs: Any) -> SimpleNamespace | None:
        if kwargs.get("account_id", account_id) != account_id:
            return None
        return row if kwargs.get("id") == row.id else None

    def update(*args: Any, **kwargs: Any) -> SimpleNamespace:
        for key, value in kwargs["flow_in"].model_dump(exclude_unset=True).items():
            setattr(row, key, value)
        return row

    def rotate(*args: Any, **kwargs: Any) -> SimpleNamespace:
        row.webhook_config = {**row.webhook_config, "webhook_secret": kwargs["secret"]}
        row.webhook_secret_rotated_at = datetime.now(UTC)
        return row

    crud.rotate_webhook_secret.side_effect = rotate
    crud.get.side_effect = get
    crud.get_multi.return_value = [row]
    crud.update.side_effect = update
    crud.get_by_name_and_account.return_value = None
    mocker.patch.object(flows, "_attach_flow_stats")
    mocker.patch.object(
        flows.crud_flow_execution, "get_execution_stats_for_flows", return_value=[]
    )
    audit = mocker.patch.object(flows, "log_config_change")
    permission = mocker.patch.object(flows, "ensure_permission_in_oss")
    service = mocker.patch(
        "preloop.services.flow_trigger_service.FlowTriggerService"
    ).return_value
    service.matches_trigger_config.return_value = True
    service.find_duplicate_execution.return_value = None
    service.trigger_flow = AsyncMock(
        return_value={"id": str(uuid4()), "status": "PENDING"}
    )
    app = FastAPI()
    app.include_router(flows.router, prefix="/api/v1")
    app.dependency_overrides[flows.get_db] = lambda: MagicMock()
    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[get_current_actor] = lambda: user
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client,
            row=row,
            user=user,
            crud=crud,
            audit=audit,
            permission=permission,
            service=service,
        )


@pytest.mark.parametrize("role", ["viewer", "editor", "owner", "superuser"])
def test_read_and_list_never_contain_secret(hook: SimpleNamespace, role: str) -> None:
    """Every reader gets metadata, including existing rows with no timestamp."""
    hook.user.is_superuser = role == "superuser"
    for path in (f"{BASE}/{hook.row.id}", BASE):
        response = hook.client.get(path)
        assert response.status_code == 200, response.text
        assert SECRET not in response.text
        data = response.json()
        flow = data[0] if isinstance(data, list) else data
        assert "webhook_secret" not in flow["webhook_config"]
        assert flow["webhook_secret"] is None
        assert flow["webhook_secret_set"] is True
        assert flow["webhook_secret_hint"] == SECRET[-4:]
        assert flow["webhook_secret_rotated_at"] is None
    assert hook.row.webhook_config["webhook_secret"] == SECRET


def test_rotate_invalidates_old_secret_and_audits_without_values(
    hook: SimpleNamespace,
) -> None:
    """Rotation changes only the ingress credential and its timestamp."""
    before = dict(hook.row.__dict__)
    before_config = dict(hook.row.webhook_config)
    url = f"/api/v1/webhooks/flows/{hook.row.id}"
    assert hook.client.post(f"{url}/{SECRET}", json={}).status_code == 200
    response = hook.client.post(f"{BASE}/{hook.row.id}/webhook-secret/rotate")
    assert response.status_code == 200, response.text
    data = response.json()
    new_secret = data["webhook_secret"]
    assert new_secret and new_secret != SECRET and len(new_secret) >= 32
    assert data["webhook_secret_hint"] == new_secret[-4:]
    assert data["webhook_secret_rotated_at"] is not None
    assert data["webhook_secret_set"] is True
    assert "webhook_secret" not in data["webhook_config"]
    assert hook.client.post(f"{url}/{SECRET}", json={}).status_code == 403
    assert hook.client.post(f"{url}/{new_secret}", json={}).status_code == 200
    for key, value in before.items():
        if key not in {"webhook_config", "webhook_secret_rotated_at"}:
            assert getattr(hook.row, key) == value
    for key, value in before_config.items():
        if key != "webhook_secret":
            assert hook.row.webhook_config[key] == value
    for path in (f"{BASE}/{hook.row.id}", BASE):
        read = hook.client.get(path)
        assert read.status_code == 200, read.text
        assert new_secret not in read.text and SECRET not in read.text
    hook.audit.assert_called_once()
    audit = hook.audit.call_args.kwargs
    assert audit["user"] == hook.user
    assert audit["action"] == "webhook_secret_rotated"
    assert audit["new_value"]["id"] == str(hook.row.id)
    assert SECRET not in str(audit) and new_secret not in str(audit)


def test_round_trip_settings_preserves_existing_secret(hook: SimpleNamespace) -> None:
    """An older flow remains callable after saving its redacted config."""
    fetched = hook.client.get(f"{BASE}/{hook.row.id}").json()
    config = fetched["webhook_config"]
    config["dedupe_path"] = "delivery.id"
    response = hook.client.put(f"{BASE}/{hook.row.id}", json={"webhook_config": config})
    assert response.status_code == 200, response.text
    assert SECRET not in response.text
    assert hook.row.webhook_config["webhook_secret"] == SECRET
    assert hook.row.webhook_config["employee_secret"]
    assert (
        hook.client.post(
            f"/api/v1/webhooks/flows/{hook.row.id}/{SECRET}", json={}
        ).status_code
        == 200
    )


def test_rotate_is_account_scoped(hook: SimpleNamespace) -> None:
    """A foreign flow cannot be rotated or disclosed."""
    hook.user.account_id = uuid4()
    response = hook.client.post(f"{BASE}/{hook.row.id}/webhook-secret/rotate")
    assert response.status_code == 404
    hook.crud.rotate_webhook_secret.assert_not_called()


def test_rotate_requires_edit_permission(hook: SimpleNamespace) -> None:
    """Reading a flow does not grant the rotation action."""
    hook.permission.side_effect = HTTPException(403, "edit_flows required")
    response = hook.client.post(f"{BASE}/{hook.row.id}/webhook-secret/rotate")
    assert response.status_code == 403
    hook.crud.rotate_webhook_secret.assert_not_called()


def test_rotate_requires_webhook_trigger(hook: SimpleNamespace) -> None:
    """A non-webhook flow has no rotatable ingress endpoint."""
    hook.row.trigger_event_source = "github"
    response = hook.client.post(f"{BASE}/{hook.row.id}/webhook-secret/rotate")
    assert response.status_code == 400
    hook.crud.rotate_webhook_secret.assert_not_called()


def test_short_legacy_secret_is_not_exposed_as_hint(hook: SimpleNamespace) -> None:
    """A short legacy token must not be returned in full as a hint."""
    hook.row.webhook_config["webhook_secret"] = "abc"
    fetched = hook.client.get(f"{BASE}/{hook.row.id}")
    assert fetched.json()["webhook_secret_hint"] is None
    assert fetched.json()["webhook_secret_set"] is True
