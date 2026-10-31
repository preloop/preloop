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


@pytest.mark.asyncio
async def test_pr_preset_run_maps_read_only_refusal_to_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from preloop.services import preset_runner
    from preloop.services.read_only_review import read_only_trigger_message

    flow = SimpleNamespace(id="flow-1", name="Pull Request Reviewer")
    monkeypatch.setattr(
        preset_runner, "_load_visible_project", lambda *a, **k: (None, None, None)
    )
    monkeypatch.setattr(
        preset_runner, "resolve_or_create_flow", lambda *a, **k: (flow, False)
    )
    monkeypatch.setattr(
        preset_runner, "_fetch_pull_request_detail", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        preset_runner, "build_pull_request_trigger_payload", lambda *a: {}
    )
    monkeypatch.setattr(preset_runner, "_active_run_for_target", lambda *a, **k: None)

    async def refuse(self: Any, **kwargs: Any) -> Any:
        raise ReadOnlyHumanTriggerError(read_only_trigger_message("flow-1"))

    monkeypatch.setattr(FlowTriggerService, "trigger_flow", refuse)
    with pytest.raises(preset_runner.PresetRunnerError) as raised:
        await preset_runner._run_preset_on_pull_request(
            MagicMock(),
            current_user=SimpleNamespace(account_id="acct"),
            preset_slug="pull-request-reviewer",
            target={"project_id": "p", "number": 7},
            confirm_create=True,
            triggered_by="Jane Doe",
        )
    assert raised.value.status_code == 409
    assert "head_sha" in str(raised.value.detail)


def test_flow_summary_marks_read_only_flows_ci_only(
    client: TestClient, db_session: Session, test_user: Any
) -> None:
    ro = _flow(db_session, test_user, read_only=True)
    plain = _flow(db_session, test_user, read_only=False)
    response = client.get("/api/v1/flows/summary")
    assert response.status_code == 200, response.text
    by_id = {row["id"]: row for row in response.json()}
    assert by_id[str(ro.id)]["ci_only"] is True
    assert by_id[str(plain.id)]["ci_only"] is False
    # Derived from the SQL-extracted publication_mode, not the clone config.
    assert "git_clone_config" not in by_id[str(ro.id)]
    from preloop.schemas.flow_summary import FlowSummaryResponse

    derived = {k: v for k, v in by_id[str(ro.id)].items() if k != "ci_only"}
    assert FlowSummaryResponse.model_validate(
        {**derived, "publication_mode": "read_only"}
    ).ci_only
