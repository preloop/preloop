"""A person cannot start a read-only review flow; only its CI identity can.

Before this, a console or session trigger created the execution, which then
failed at startup with "Read-only review requires a restricted CI
execution", leaving a dead row. Now the trigger is refused with 409 before
any execution exists, and the message names the CI route and its fields.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models.crud import crud_account, crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate
from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.services.read_only_review import ReadOnlyHumanTriggerError


@pytest.fixture(autouse=True)
def account_owner(db_session: Session, test_user: Any) -> Any:
    """Make the caller the account's primary user, whom the OSS check admits."""
    account = crud_account.get(db_session, id=test_user.account_id)
    account.primary_user_id = test_user.id
    db_session.flush()
    return account


def _flow(db_session: Session, test_user: Any, *, read_only: bool) -> Any:
    git_clone_config: dict[str, Any] = {"enabled": True}
    if read_only:
        git_clone_config["publication_mode"] = "read_only"
    flow = crud_flow.create(
        db_session,
        account_id=test_user.account_id,
        flow_in=FlowCreate(
            account_id=test_user.account_id,
            name="Read-only review" if read_only else "Ordinary flow",
            prompt_template="Review",
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
        ),
    )
    # Set directly: the read-only shape needs a bound repository to pass
    # schema validation, which is beside the point of this test.
    flow.git_clone_config = git_clone_config
    db_session.flush()
    return flow


def _executions(db_session: Session, flow: Any) -> int:
    return len(crud_flow_execution.get_by_flow(db_session, flow_id=flow.id))


@pytest.mark.parametrize("body", [None, {"matrix": [{}]}])
def test_human_trigger_of_read_only_flow_is_409_and_creates_nothing(
    client: TestClient, db_session: Session, test_user: Any, body: Any
) -> None:
    flow = _flow(db_session, test_user, read_only=True)

    response = client.post(f"/api/v1/flows/{flow.id}/trigger", json=body)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert f"POST /api/v1/flows/{flow.id}/trigger" in detail
    assert "pr_number" in detail and "head_sha" in detail
    assert "no execution was created" in detail
    assert _executions(db_session, flow) == 0


@pytest.mark.asyncio
async def test_service_refuses_before_insert(
    db_session: Session, test_user: Any
) -> None:
    flow = _flow(db_session, test_user, read_only=True)
    with pytest.raises(ReadOnlyHumanTriggerError):
        await FlowTriggerService(db_session).trigger_flow(flow_id=flow.id)
    assert _executions(db_session, flow) == 0


def test_ordinary_flow_trigger_is_not_refused(
    client: TestClient,
    db_session: Session,
    test_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.services import read_only_review

    flow = _flow(db_session, test_user, read_only=False)
    calls: list[Any] = []
    original = read_only_review.reject_human_read_only_trigger
    monkeypatch.setattr(
        read_only_review,
        "reject_human_read_only_trigger",
        lambda f: calls.append(f) or original(f),
    )

    async def fake_start(self: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(FlowTriggerService, "_start_flow_execution", fake_start)
    response = client.post(f"/api/v1/flows/{flow.id}/trigger", json={})
    assert response.status_code != 409, response.text
    assert calls, "the read-only guard must run on every human trigger"
