"""Account flow-artifact usage against the quota (#1339).

The endpoint mirrors ``/account/session-artifacts/usage``: same account
resolution, same role behaviour, a different storage pool.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_user, flow_artifact

USAGE = "/api/v1/account/flow-artifacts/usage"
SESSION_USAGE = "/api/v1/account/session-artifacts/usage"


def seed(db: Session, account_id: Any, **overrides: Any) -> models.FlowArtifact:
    flow = models.Flow(
        account_id=account_id,
        name=f"usage-{uuid4().hex[:8]}",
        prompt_template="test",
        agent_type="codex",
        agent_config={},
    )
    db.add(flow)
    db.flush()
    execution = models.FlowExecution(flow_id=flow.id, status="RUNNING")
    db.add(execution)
    db.flush()
    values = {
        "account_id": account_id,
        "flow_id": flow.id,
        "execution_id": execution.id,
        "thread_id": str(uuid4()),
        "kind": "workspace",
        "manifest": {},
        "manifest_sha256": "0" * 64,
        "ciphertext": b"w" * 100,
        "availability": "available",
        "expires_at": datetime.now(UTC) + timedelta(hours=24),
        **overrides,
    }
    row = models.FlowArtifact(**values)
    db.add(row)
    db.flush()
    return row


def test_usage_reports_retained_bytes_against_the_quota(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "flow_artifact_account_quota_bytes", 1000)
    now = datetime.now(UTC)
    first_expiry = now + timedelta(hours=3)
    seed(db_session, test_user.account_id, expires_at=first_expiry)
    seed(db_session, test_user.account_id, ciphertext=b"w" * 50)
    seed(
        db_session,
        test_user.account_id,
        kind="evidence",
        ciphertext=b"e" * 7,
        expires_at=now - timedelta(minutes=5),
    )
    # Another account's bytes never show up here.
    other = models.Account(organization_name="other")
    db_session.add(other)
    db_session.flush()
    seed(db_session, other.id, ciphertext=b"o" * 999)

    response = client.get(USAGE)
    assert response.status_code == 200
    body = response.json()
    assert body["retained_bytes"] == 157
    assert body["quota_bytes"] == 1000
    assert body["by_kind"] == {
        "workspace": {"bytes": 150, "count": 2},
        "evidence": {"bytes": 7, "count": 1},
    }
    assert body["expired_pending_cleanup"] == 1
    # The expired-but-uncleared evidence row is earliest among available rows.
    assert datetime.fromisoformat(body["next_expiry_at"]) == now - timedelta(minutes=5)
    # The same number admission compares.
    assert (
        body["retained_bytes"]
        == flow_artifact.usage(db_session, account_id=test_user.account_id)[
            "retained_bytes"
        ]
    )


def test_usage_is_empty_for_a_fresh_account(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "flow_artifact_account_quota_bytes", 4096)
    response = client.get(USAGE)
    assert response.status_code == 200
    assert response.json() == {
        "retained_bytes": 0,
        "quota_bytes": 4096,
        "by_kind": {},
        "expired_pending_cleanup": 0,
        "next_expiry_at": None,
    }


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_role_behaviour_mirrors_session_artifact_usage(
    app: Any,
    db_session: Session,
    request: pytest.FixtureRequest,
    role: str,
) -> None:
    """Whatever a role gets on the session usage read, it gets here too."""
    user = request.getfixturevalue(f"test_{role}_user")
    app.dependency_overrides[get_current_active_user] = lambda: crud_user.get(
        db_session, id=user.id
    )
    with TestClient(app) as client:
        session_status = client.get(SESSION_USAGE).status_code
        flow_status = client.get(USAGE).status_code
    assert flow_status == session_status


def test_unauthenticated_request_is_refused_like_session_usage(app: Any) -> None:
    app.dependency_overrides.pop(get_current_active_user, None)
    with TestClient(app) as client:
        session_status = client.get(SESSION_USAGE).status_code
        flow_status = client.get(USAGE).status_code
    assert session_status == 401
    assert flow_status == session_status
