"""Every applied policy upload records a policy version (issue #2557)."""

import json
from typing import Any

from fastapi.testclient import TestClient
from pytest_mock import MockerFixture
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.services.policy_version_service import PolicyVersionService


def _policy(action: str, name: str = "Example policy") -> dict[str, Any]:
    """Build a policy with one tool access rule.

    Args:
        action: Condition action for the tool rule.
        name: Policy metadata name.

    Returns:
        The policy document as a dict.
    """
    return {
        "version": "1.0",
        "metadata": {"name": name},
        "tools": [
            {
                "name": "Bash",
                "source": "builtin",
                "conditions": [
                    {"expression": "args.command == 'rm'", "action": action}
                ],
            }
        ],
    }


def _upload(client: TestClient, data: dict[str, Any], dry_run: bool = False) -> Any:
    """Upload a policy file.

    Args:
        client: API test client.
        data: Policy document.
        dry_run: Whether to validate only.

    Returns:
        The HTTP response.
    """
    return client.post(
        "/api/v1/policies/upload",
        data={"dry_run": str(dry_run).lower()},
        files={"file": ("policy.json", json.dumps(data))},
    )


def test_apply_records_version_listed_and_rollbackable(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """An apply that updates tool rules writes a version that rollback restores."""
    first = _upload(client, _policy("deny"))
    assert first.status_code == 200, first.text
    assert first.json()["policy_version"] == 1

    second = _upload(client, _policy("allow"))
    assert second.status_code == 200, second.text
    assert second.json()["policy_version"] == 2

    listed = client.get("/api/v1/policies/versions", params={"limit": 3})
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["total"] == 2
    assert [v["version_number"] for v in body["versions"]] == [2, 1]
    assert body["versions"][0]["is_active"] is True
    assert body["versions"][0]["tools_count"] == 1
    assert "Example policy" in body["versions"][0]["description"]

    rollback = client.post(
        f"/api/v1/policies/versions/{body['versions'][1]['id']}/rollback",
        json={"preview_only": False},
    )
    assert rollback.status_code == 200, rollback.text
    exported = client.get("/api/v1/policies/export", params={"format": "json"})
    tools = exported.json()["tools"]
    assert [c["action"] for c in tools[0]["conditions"]] == ["deny"]

    service = PolicyVersionService(db_session, str(test_user.account_id))
    assert service.get_active_snapshot().version_number == 1


def test_dry_run_records_no_version(client: TestClient) -> None:
    """A dry run changes nothing, so it records no version."""
    response = _upload(client, _policy("deny"), dry_run=True)
    assert response.status_code == 200, response.text
    assert response.json()["policy_version"] is None
    listed = client.get("/api/v1/policies/versions")
    assert listed.json()["total"] == 0


def test_snapshot_failure_keeps_apply_successful(
    client: TestClient, mocker: MockerFixture
) -> None:
    """A failed snapshot reports a warning; the committed apply still succeeds."""
    assert _upload(client, _policy("deny")).json()["policy_version"] == 1
    mocker.patch.object(
        PolicyVersionService, "create_snapshot", side_effect=RuntimeError("boom")
    )
    response = _upload(client, _policy("allow"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert body["policy_version"] is None
    assert any("no policy version was recorded" in w for w in body["warnings"])
    exported = client.get("/api/v1/policies/export", params={"format": "json"})
    assert exported.json()["tools"][0]["conditions"][0]["action"] == "allow"
    mocker.stopall()
    listed = client.get("/api/v1/policies/versions").json()
    assert listed["total"] == 1
    assert listed["versions"][0]["is_active"] is True
