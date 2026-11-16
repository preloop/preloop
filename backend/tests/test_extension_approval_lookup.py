"""Tenant and permission boundaries for a single extension decision lookup."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_extension_approval
from preloop.schemas.discovered_extension import ExtensionApprovalUpdate

PATH = "/api/v1/agents/extension-approvals/claude_code/plugin/reviewer@community"


def test_approval_lookup_is_tenant_scoped(
    client: TestClient, db_session: Session, test_viewer_user: models.User
) -> None:
    """protects=one-key metadata preservation and isolation; fails_when=another
    account's decision leaks; no_claim=synthetic accounts and local PostgreSQL.
    """
    crud_extension_approval.set_state(
        db_session,
        account_id=test_viewer_user.account_id,
        agent_kind="claude_code",
        extension_kind="plugin",
        extension_id="reviewer@community",
        decided_by_user_id=test_viewer_user.id,
        payload=ExtensionApprovalUpdate(
            approval_state="approved", note="Other account"
        ),
    )
    assert client.get(PATH).status_code == 404
    decision = {
        "approval_state": "unapproved",
        "version_constraint": "1.4.*",
        "note": "Reviewed",
    }
    assert client.put(PATH, json=decision).status_code == 200
    response = client.get(PATH)
    assert response.status_code == 200
    assert {key: response.json()[key] for key in decision} == decision


def test_approval_lookup_requires_view_permission(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """protects=read authorization; fails_when=lookup bypasses view_agents;
    no_claim=mock role provider exercises the existing edition boundary.
    """
    monkeypatch.setattr(
        "preloop.utils.permissions.members_have_full_access", lambda: False
    )
    monkeypatch.setattr(
        "preloop.utils.permissions.user_holds_permission",
        lambda db, user, permission: False,
    )
    assert client.get(PATH).status_code == 403
    monkeypatch.setattr(
        "preloop.utils.permissions.user_holds_permission",
        lambda db, user, permission: permission == "view_agents",
    )
    assert client.get(PATH).status_code == 404


def test_approval_lookup_rejects_oversized_identifier(client: TestClient) -> None:
    """protects=bounded lookup; fails_when=multibyte identifiers bypass limits;
    no_claim=synthetic UTF-8 identifier, not a production account.
    """
    assert client.get(PATH.rsplit("/", 1)[0] + "/" + "é" * 100).status_code == 422
