"""Extension decision history and state counts behind Settings > Compliance."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.models import models
from tests.test_extension_inventory import (
    APPROVAL,
    EXTENSIONS,
    REPORTS,
    extension,
    report,
)

DECISIONS = "/api/v1/agents/extension-decisions"
COUNTS = EXTENSIONS + "/state-counts"
HASH = "sha256:" + "a" * 64


def _decision_audits(db: Session, account_id: Any) -> list[dict[str, Any]]:
    """Audit rows written for extension decisions, oldest first."""
    rows = db.scalars(
        select(models.AuditLog)
        .where(
            models.AuditLog.account_id == account_id,
            models.AuditLog.resource_id == "extension_approval",
        )
        .order_by(models.AuditLog.timestamp, models.AuditLog.id)
    ).all()
    return [row.details for row in rows]


def test_decisions_chain_previous_state_and_audit(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """protects=a decision trail with the replaced state, decider and an audit
    row per change; fails_when=a decision overwrites in place with no record or
    the previous state is lost; no_claim=history starts when this ships.
    """
    assert client.post(REPORTS, json=report()).status_code == 202
    assert client.put(APPROVAL, json={"approval_state": "approved"}).status_code == 200
    response = client.put(
        APPROVAL, json={"approval_state": "unapproved", "note": "Not reviewed"}
    )
    assert response.status_code == 200

    items = client.get(DECISIONS).json()["items"]
    assert [(i["previous_state"], i["new_state"]) for i in items] == [
        ("approved", "unapproved"),
        (None, "approved"),
    ]
    assert items[0]["note"] == "Not reviewed"
    assert items[0]["decided_by_user_id"] == str(test_user.id)
    assert items[0]["decided_by"]
    assert items[0]["extension_id"] == "reviewer@community"

    audits = _decision_audits(db_session, test_user.account_id)
    assert [a["action"] for a in audits] == ["updated", "updated"]
    assert audits[1]["old_value"]["approval_state"] == "approved"
    assert audits[1]["new_value"]["approval_state"] == "unapproved"


def test_pin_and_unpin_are_recorded(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """protects=pin changes in history and audit; fails_when=a pin is added or
    removed without a row; no_claim=none beyond protects.
    """
    assert client.put(APPROVAL, json={"approval_state": "approved"}).status_code == 200
    pin = client.post(APPROVAL + "/pins", json={"content_hash": HASH})
    assert pin.status_code == 201, pin.text
    assert client.delete(f"{APPROVAL}/pins/{pin.json()['id']}").status_code == 204

    items = client.get(DECISIONS).json()["items"]
    assert [i["action"] for i in items] == [
        "pin_removed",
        "pin_added",
        "state_changed",
    ]
    assert items[0]["content_hash"] == HASH
    assert items[0]["decided_by_user_id"] == str(test_user.id)
    actions = [a["action"] for a in _decision_audits(db_session, test_user.account_id)]
    assert actions == ["updated", "pin_added", "pin_removed"]


def test_bulk_decisions_record_each_identity(client: TestClient) -> None:
    """protects=one history row per bulk identity with its own prior state;
    fails_when=bulk changes skip history or share one previous state;
    no_claim=none beyond protects.
    """
    assert client.put(APPROVAL, json={"approval_state": "approved"}).status_code == 200
    identities = [
        {
            "agent_kind": "claude_code",
            "extension_kind": "plugin",
            "extension_id": "reviewer@community",
        },
        {"agent_kind": "claude_code", "extension_kind": "skill", "extension_id": "new"},
    ]
    response = client.post(
        "/api/v1/agents/extension-approvals/bulk",
        json={"approval_state": "unapproved", "extensions": identities},
    )
    assert response.status_code == 200, response.text
    items = client.get(DECISIONS, params={"limit": 2}).json()["items"]
    previous = {i["extension_id"]: i["previous_state"] for i in items}
    assert previous == {"reviewer@community": "approved", "new": None}
    assert {i["new_state"] for i in items} == {"unapproved"}


def test_state_counts_follow_decisions(client: TestClient) -> None:
    """protects=the approved / unapproved / unknown summary on the page;
    fails_when=undecided identities are dropped or a decision is not counted;
    no_claim=counts are per identity, not per device copy.
    """
    payload = report(
        [
            extension(),
            extension(kind="skill", id="writer"),
            extension(kind="skill", id="reader"),
        ]
    )
    assert client.post(REPORTS, json=payload).status_code == 202, payload
    assert client.get(COUNTS).json() == {
        "approved": 0,
        "unapproved": 0,
        "unknown": 3,
        "total": 3,
    }
    client.put(APPROVAL, json={"approval_state": "approved"})
    client.put(
        "/api/v1/agents/extension-approvals/claude_code/skill/writer",
        json={"approval_state": "unapproved"},
    )
    assert client.get(COUNTS).json() == {
        "approved": 1,
        "unapproved": 1,
        "unknown": 1,
        "total": 3,
    }


def test_history_is_account_scoped(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """protects=tenant isolation of the history; fails_when=another account's
    decisions are listed; no_claim=synthetic second account.
    """
    other = models.Account(organization_name="Other Org")
    db_session.add(other)
    db_session.flush()
    db_session.add(
        models.ExtensionDecisionEvent(
            account_id=other.id,
            agent_kind="claude_code",
            extension_kind="plugin",
            extension_id="foreign",
            action="state_changed",
            new_state="approved",
        )
    )
    db_session.flush()
    assert client.get(DECISIONS).json()["items"] == []


def test_routes_need_view_agents(
    client: TestClient, test_viewer_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """protects=the view_agents boundary; fails_when=a user without it reads
    decisions or counts; no_claim=permission provider is patched.
    """
    monkeypatch.setattr(
        "preloop.utils.permissions.members_have_full_access", lambda: False
    )
    monkeypatch.setattr(
        "preloop.utils.permissions.user_holds_permission",
        lambda db, user, permission: False,
    )
    client.app.dependency_overrides[get_current_active_user] = lambda: test_viewer_user
    assert client.get(DECISIONS).status_code == 403
    assert client.get(COUNTS).status_code == 403
