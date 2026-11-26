"""Gateway evidence and offboard metadata must agree with actual access."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_api_key, crud_managed_agent_enrollment
from tests.endpoints.test_account_agents import (
    _post_anthropic_message,
    _seed_anthropic_gateway_model,
    _seed_pauseable_agent,
)


def _failed_enrollment(db: Session, user: models.User, agent_id: str) -> Any:
    """Persist the CLI's failed probe without a working model route."""
    return crud_managed_agent_enrollment.create_for_agent(
        db,
        account_id=user.account_id,
        agent_id=agent_id,
        created_by_user_id=user.id,
        enrollment_type="cli_managed_config",
        validation_result={
            "preloop_server_present": True,
            "mcp_server_url_ok": True,
            "mcp_server_auth_ok": True,
            "live_validation_supported": True,
            "live_validation_status": "failed",
            "live_validation_attempted": True,
            "gateway_model_configured": False,
        },
    )


def test_gateway_success_reconciles_console_state(
    client: Any, db_session: Session, test_user: models.User
) -> None:
    """An authenticated request with mocked upstream supersedes a failed probe."""
    agent_id, token = _seed_pauseable_agent(
        client, db_session, test_user, source_id="gateway-evidence"
    )
    _seed_anthropic_gateway_model(db_session, test_user.account_id)
    _failed_enrollment(db_session, test_user, agent_id)
    before = client.get(f"/api/v1/agents/{agent_id}").json()["agent"]
    assert before["model_gateway_configured"] is False
    assert _post_anthropic_message(client, token).status_code == 200
    after = client.get(f"/api/v1/agents/{agent_id}").json()["agent"]
    assert after["model_gateway_configured"] is True
    assert after["onboarding_state"] == "fully_onboarded"
    # Retain the probe's historical result, but do not let it mislabel routing.
    assert after["live_validation_status"] == "failed"


@pytest.mark.parametrize(
    "kind", ["failed", "imported", "older", "reapplied", "sibling"]
)
def test_only_current_server_gateway_success_proves_route(
    client: Any, db_session: Session, test_user: models.User, kind: str
) -> None:
    """Telemetry, old requests, other agents and errors cannot bless a route."""
    agent_id, token = _seed_pauseable_agent(
        client, db_session, test_user, source_id="gateway-evidence-negative"
    )
    enrollment = _failed_enrollment(db_session, test_user, agent_id)
    if kind == "reapplied":
        enrollment.last_applied_at = datetime.now(UTC) + timedelta(seconds=1)
        db_session.flush()
    if kind == "sibling":
        _, token = _seed_pauseable_agent(
            client, db_session, test_user, source_id="gateway-evidence-sibling"
        )
    key = crud_api_key.get_by_key(db_session, key=token)
    db_session.add(
        models.ApiUsage(
            account_id=test_user.account_id,
            api_key_id=key.id,
            endpoint="/anthropic/v1/messages",
            method="POST",
            status_code=500 if kind == "failed" else 200,
            duration=0.01,
            action_type="imported_usage" if kind == "imported" else "model_gateway",
            timestamp=(enrollment.created_at - timedelta(seconds=1))
            if kind == "older"
            else datetime.now(UTC),
        )
    )
    db_session.commit()
    after = client.get(f"/api/v1/agents/{agent_id}").json()["agent"]
    assert after["model_gateway_configured"] is False


@pytest.mark.parametrize("operation", ["issue", "decommission"])
def test_credential_lifecycle_uses_account_then_agent_lock(
    client: Any, db_session: Session, test_user: models.User, operation: str
) -> None:
    """Issuance must serialize with decommission before inserting an active key."""
    agent_id, _ = _seed_pauseable_agent(
        client, db_session, test_user, source_id="credential-serialization"
    )
    statements: list[str] = []
    connection = db_session.connection()

    def record_statement(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        """Record SQL structure without retaining parameter values."""
        statements.append(statement.lower())

    event.listen(connection, "before_cursor_execute", record_statement)
    try:
        if operation == "issue":
            response = client.post(
                f"/api/v1/agents/{agent_id}/credentials",
                json={"name": "serialized-credential", "scopes": ["mcp:read"]},
            )
        else:
            response = client.patch(
                f"/api/v1/agents/{agent_id}",
                json={"lifecycle_action": "decommission"},
            )
    finally:
        event.remove(connection, "before_cursor_execute", record_statement)
    assert response.status_code == (201 if operation == "issue" else 200)
    lock_indexes = [
        index
        for index, statement in enumerate(statements)
        if "from managed_agent " in statement and "for update" in statement
    ]
    assert lock_indexes, "credential creation did not lock the agent lifecycle row"
    account_lock = next(
        index
        for index, statement in enumerate(statements)
        if "from account " in statement and "for update" in statement
    )
    assert account_lock < lock_indexes[0], (
        "lock account before agent, as decommission does"
    )
    write_statement = (
        "insert into api_key "
        if operation == "issue"
        else "update managed_agent_credential "
    )
    credential_write = next(
        index
        for index, statement in enumerate(statements)
        if write_statement in statement
    )
    assert lock_indexes[0] < credential_write


def test_offboard_revokes_credential_metadata_and_audits(
    client: Any, db_session: Session, test_user: models.User
) -> None:
    """Revocation, credential summaries and the operator audit commit together."""
    agent_id, token = _seed_pauseable_agent(
        client, db_session, test_user, source_id="offboard-metadata"
    )
    assert (
        client.patch(
            f"/api/v1/agents/{agent_id}",
            json={"lifecycle_action": "decommission", "reason": "offboarded"},
        ).status_code
        == 200
    )
    credentials = client.get(f"/api/v1/agents/{agent_id}/credentials").json()
    assert credentials
    assert all(row["status"] == "revoked" for row in credentials)
    assert all(row["revoked_at"] for row in credentials)
    assert all(row["revoked_reason"] for row in credentials)
    assert _post_anthropic_message(client, token).status_code == 401
    audit = (
        db_session.query(models.AuditLog)
        .filter_by(resource_id=agent_id, action="managed_agent_updated")
        .one()
    )
    assert audit.details["lifecycle_action"] == "decommission"
    assert audit.details["revoked_count"] == len(credentials)
    timestamps = [row["revoked_at"] for row in credentials]
    assert (
        client.patch(
            f"/api/v1/agents/{agent_id}",
            json={"lifecycle_action": "decommission"},
        ).status_code
        == 200
    )
    assert [
        row["revoked_at"]
        for row in client.get(f"/api/v1/agents/{agent_id}/credentials").json()
    ] == timestamps
