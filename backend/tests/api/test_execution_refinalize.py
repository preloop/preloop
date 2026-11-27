"""Human-only HTTP recovery and its execution-detail audit projection."""

# ruff: noqa: F401, F811
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from preloop.models import crud, models
from preloop.models.crud.base import CRUDBase
from tests.services.test_execution_refinalize import provider_read, saved
from tests.services.test_platform_review import (
    client,
    github,
    human_client,
    identity,
    world,
)


def test_preview_apply_and_visible_mark(
    world: SimpleNamespace, human_client: TestClient
) -> None:
    execution = saved(world)
    path = f"/api/v1/flows/executions/{execution.id}"
    preview = human_client.post(path + "/refinalize")
    assert preview.status_code == 200, preview.text
    assert preview.json()["eligible"] is True
    assert execution.status == "FAILED"
    denied = human_client.post(
        path + "/refinalize", json={"dry_run": False, "expected_digest": "0" * 64}
    )
    assert denied.status_code == 412
    applied = human_client.post(
        path + "/refinalize",
        json={"dry_run": False, "expected_digest": preview.json()["digest"]},
    )
    assert applied.status_code == 200, applied.text
    read = human_client.get(path)
    assert read.status_code == 200, read.text
    assert read.json()["refinalization"]["user_id"] == str(world.owner.id)
    assert read.json()["refinalization"]["previous_status"] == "FAILED"


def test_ci_identity_cannot_preview(world: SimpleNamespace, client: TestClient) -> None:
    who = identity(world, world.projects[0])
    execution = saved(world)
    response = client.post(
        f"/api/v1/flows/executions/{execution.id}/refinalize",
        headers={"Authorization": f"Bearer {who.token}"},
    )
    assert response.status_code == 403


def test_member_cannot_recover(
    world: SimpleNamespace, human_client: TestClient
) -> None:
    execution = saved(world)
    CRUDBase(models.Account).update(
        world.db, db_obj=world.account, obj_in={"primary_user_id": None}
    )
    response = human_client.post(f"/api/v1/flows/executions/{execution.id}/refinalize")
    assert response.status_code == 403


def test_agent_credential_cannot_inherit_owner(
    world: SimpleNamespace, human_client: TestClient
) -> None:
    execution = saved(world)
    world.owner._auth_api_key = SimpleNamespace(
        requires_machine_authorization=False,
        context_data={"flow_execution_id": str(execution.id)},
    )
    try:
        response = human_client.post(
            f"/api/v1/flows/executions/{execution.id}/refinalize"
        )
        assert response.status_code == 403
    finally:
        del world.owner._auth_api_key


def test_admin_can_preview(world: SimpleNamespace, human_client: TestClient) -> None:
    execution = saved(world)
    CRUDBase(models.Account).update(
        world.db, db_obj=world.account, obj_in={"primary_user_id": None}
    )
    role = crud.crud_role.get_by_name(world.db, name="admin")
    if role is None:
        role = CRUDBase(models.Role).create(
            world.db,
            obj_in={
                "name": "admin",
                "is_system_role": True,
                "description": "Account administrator",
            },
        )
    crud.crud_user_role.assign_role(world.db, user_id=world.owner.id, role_id=role.id)
    response = human_client.post(f"/api/v1/flows/executions/{execution.id}/refinalize")
    assert response.status_code == 200, response.text
