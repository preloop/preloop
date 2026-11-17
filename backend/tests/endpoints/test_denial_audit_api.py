"""Audit list and timeline expose reason filters with tenant isolation."""

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.services.denial_audit import write_denial


def test_denial_reason_filter_applies_to_list_total_and_grouped_timeline(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """Reason filtering must affect the rows and totals in both audit views."""
    other = crud.crud_account.create(db_session, obj_in={"organization_name": "Other"})
    for account_id, code in [
        (test_user.account_id, "key_expired"),
        (test_user.account_id, "tool_blocked"),
        (other.id, "key_expired"),
    ]:
        write_denial(
            db_session,
            account_id=account_id,
            principal_id=uuid4(),
            principal_type="managed_agent",
            action="agent_permission_denied",
            target="Bash",
            reason_code=code,
        )
    response = client.get("/api/v1/audit-logs", params={"reason_code": "key_expired"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 1
    assert len(data["logs"]) == 1
    assert data["logs"][0]["details"]["reason_code"] == "key_expired"
    response = client.get(
        "/api/v1/audit-logs/grouped", params={"reason_code": "key_expired"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
