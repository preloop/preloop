"""Content-asserting tests for routes that previously lacked a real-DB test.

Each test creates rows through ``preloop.models.crud`` and asserts the body
the route returns, not only its status code.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.flow_presets import PRESET_SLUGS
from preloop.models import crud, models
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate


def _flow(
    db: Session,
    account_id: UUID | None,
    *,
    name: str | None = None,
    is_preset: bool = False,
    source_preset_id: UUID | None = None,
) -> models.Flow:
    """Create a minimal flow row.

    Args:
        db: Database session.
        account_id: Owning account, or None for a global row.
        name: Flow name; a random one is used when omitted.
        is_preset: Whether the row is a preset.
        source_preset_id: Catalog preset an account copy was cloned from.

    Returns:
        The created flow.
    """
    return crud.crud_flow.create(
        db=db,
        flow_in=FlowCreate(
            name=name or f"flow-{uuid4().hex[:8]}",
            prompt_template="Summarise the change",
            trigger_event_source="schedule",
            trigger_event_types=["schedule"],
            agent_type="openhands",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            is_enabled=True,
            is_preset=is_preset,
            source_preset_id=source_preset_id,
            account_id=account_id,
        ),
        account_id=account_id,
    )


def _execution(db: Session, flow: models.Flow) -> models.FlowExecution:
    """Create a running execution for ``flow``.

    Args:
        db: Database session.
        flow: Flow the execution belongs to.

    Returns:
        The created execution.
    """
    return crud.crud_flow_execution.create(
        db,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id, status="RUNNING", trigger_event_details={}
        ),
    )


def test_approval_bypass_status_reports_only_callers_active_bypasses(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """The banner status lists the caller's live bypass and flags auto-approve."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    live = crud.crud_approval_bypass.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "user_id": test_user.id,
            "created_by_user_id": test_user.id,
            "mode": "auto_approve",
            "reason": "Release freeze drill",
            "expires_at": now + timedelta(hours=1),
        },
    )
    crud.crud_approval_bypass.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "user_id": test_user.id,
            "created_by_user_id": test_user.id,
            "mode": "mute_notifications",
            "expires_at": now - timedelta(hours=1),
        },
    )
    assert isinstance(live.id, UUID)

    response = client.get("/api/v1/approval-bypasses/status")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["active"] is True
    assert body["auto_approve_active"] is True
    assert [b["id"] for b in body["bypasses"]] == [str(live.id)]
    assert body["bypasses"][0]["reason"] == "Release freeze drill"


def test_approval_bypass_status_is_inactive_without_bypasses(
    client: TestClient,
) -> None:
    """With no bypass rows the banner status is fully inactive."""
    response = client.get("/api/v1/approval-bypasses/status")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["active"] is False
    assert body["auto_approve_active"] is False
    assert body["bypasses"] == []


def test_list_organizations_returns_tracker_rows(
    client: TestClient, db_session: Session, test_tracker: models.Tracker
) -> None:
    """Organizations linked to the caller's trackers are listed with fields."""
    organization = crud.crud_organization.create(
        db_session,
        obj_in={
            "name": "Example Org",
            "identifier": "example-org",
            "description": "Synthetic organization",
            "tracker_id": test_tracker.id,
        },
    )

    response = client.get("/api/v1/organizations")

    assert response.status_code == 200, response.text
    body = response.json()
    rows = {row["id"]: row for row in body["items"]}
    assert str(organization.id) in rows
    row = rows[str(organization.id)]
    assert row["name"] == "Example Org"
    assert row["identifier"] == "example-org"
    assert row["description"] == "Synthetic organization"
    assert row["tracker_id"] == str(test_tracker.id)
    assert body["total"] == len(body["items"])
    assert body["limit"] == 100 and body["offset"] == 0


def test_evidence_status_reports_missing_pack(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """An execution that captured no evidence reports ``missing``, unsigned."""
    execution = _execution(db_session, _flow(db_session, test_user.account_id))

    response = client.get(f"/api/v1/flows/executions/{execution.id}/evidence-status")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "missing"
    assert body["object_lock"] is False
    assert body["signed"] is False


def test_evidence_status_unknown_execution_is_404(client: TestClient) -> None:
    """An execution id outside the account is not found."""
    response = client.get(f"/api/v1/flows/executions/{uuid4()}/evidence-status")

    assert response.status_code == 404


def test_execution_metrics_count_logged_tool_calls(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """Metrics count tool-call log rows and report zero limit usage."""
    execution = _execution(db_session, _flow(db_session, test_user.account_id))
    for index in range(2):
        crud.crud_flow_execution_log.append_log(
            db_session,
            str(execution.id),
            {"type": "tool_call", "payload": {"call_id": f"call-{index}"}},
        )
    crud.crud_flow_execution_log.append_log(
        db_session, str(execution.id), {"type": "log", "message": "hello"}
    )

    response = client.get(f"/api/v1/flows/executions/{execution.id}/metrics")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tool_calls"] == 2
    assert body["token_usage"]["total_tokens"] == 0
    assert body["api_requests"] == 0
    assert body["limit_status"]["total_tokens"] == 0
    assert isinstance(body["limits"], dict)
    assert isinstance(body["limit_sources"], dict)


def test_flow_presets_returns_global_and_account_rows_with_slugs(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """Presets list includes catalog and account copies, slugged correctly."""
    slug, catalog_name = next(iter(PRESET_SLUGS.items()))
    existing = {
        flow.name: flow
        for flow in crud.crud_flow.get_presets_for_account(
            db_session, account_id=test_user.account_id, limit=1000
        )
        if flow.account_id is None
    }
    catalog = existing.get(catalog_name) or _flow(
        db_session, None, name=catalog_name, is_preset=True
    )
    copy = _flow(
        db_session,
        test_user.account_id,
        name="Jane Doe's copy",
        is_preset=True,
        source_preset_id=catalog.id,
    )
    other_account_flow = _flow(db_session, test_user.account_id, name="not-a-preset")

    response = client.get("/api/v1/flows/presets")

    assert response.status_code == 200, response.text
    rows = {row["id"]: row for row in response.json()}
    assert str(other_account_flow.id) not in rows
    assert rows[str(catalog.id)]["slug"] == slug
    assert rows[str(catalog.id)]["name"] == catalog_name
    assert rows[str(copy.id)]["name"] == "Jane Doe's copy"
    assert rows[str(copy.id)].get("slug") is None


def test_execution_gateway_event_returns_payload(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """A single gateway event is returned with its metadata and message."""
    execution = _execution(db_session, _flow(db_session, test_user.account_id))
    entry: Any = crud.crud_flow_execution_log.append_log(
        db_session,
        str(execution.id),
        {
            "type": "model_gateway_call",
            "message": "completion",
            "payload": {"model": "example-model", "status_code": 200},
        },
    )

    response = client.get(
        f"/api/v1/flows/executions/{execution.id}/gateway-events/{entry.id}"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(entry.id)
    assert body["execution_id"] == str(execution.id)
    assert body["type"] == "model_gateway_call"
    assert body["payload"]["model"] == "example-model"
    assert body["payload"]["status_code"] == 200
    assert body["timestamp"]


def test_execution_gateway_event_other_execution_is_404(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """An event id belonging to another execution is not served."""
    flow = _flow(db_session, test_user.account_id)
    owner, other = _execution(db_session, flow), _execution(db_session, flow)
    entry: Any = crud.crud_flow_execution_log.append_log(
        db_session, str(owner.id), {"type": "log", "message": "x"}
    )

    response = client.get(
        f"/api/v1/flows/executions/{other.id}/gateway-events/{entry.id}"
    )

    assert response.status_code == 404
