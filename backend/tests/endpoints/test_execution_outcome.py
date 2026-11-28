"""Historical launch attribution and outcome evidence over the execution API."""

import json
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.models.crud import execution_outcome
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator


@pytest.fixture
def flow(db_session: Session, test_user: models.User) -> models.Flow:
    """Create an account-owned flow with redaction canaries."""
    account = crud.crud_account.get(db_session, id=test_user.account_id)
    account.primary_user_id = test_user.id
    db_session.flush()
    return crud.crud_flow.create(
        db_session,
        account_id=account.id,
        flow_in=FlowCreate(
            name="Review changes",
            prompt_template="Revision A",
            agent_type="codex",
            agent_config={
                "limits": {"max_turns": 10},
                "env": {"CUSTOM": "opaque-env-value"},
                "arbitrary": {"password": "opaque-password"},
            },
        ),
    )


def _run(
    db: Session, flow: models.Flow, *, retry_of: UUID | None = None
) -> models.FlowExecution:
    """Create a queued execution like trigger entry points do."""
    return crud.crud_flow_execution.create(
        db,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id,
            status="PENDING",
            retry_of_execution_id=retry_of,
            trigger_event_id="event-17",
            trigger_event_details={
                "source": "github",
                "payload": {
                    "repository": {"full_name": "example/project"},
                    "pull_request": {"number": 17, "head": {"sha": "a" * 40}},
                },
            },
        ),
    )


def _url(run: models.FlowExecution) -> str:
    """Return the account-scoped outcome route."""
    return f"/api/v1/flows/executions/{run.id}/outcome"


def _select(
    db: Session, flow: models.Flow, run: models.FlowExecution | None
) -> FlowExecutionOrchestrator:
    """Use the worker's launch path, stopping before any agent or upstream call."""
    orch = FlowExecutionOrchestrator(db, flow.id, {}, None)
    orch.execution_log = run
    orch._get_flow_details()
    orch._create_execution_log()
    return orch


def test_selection_history_and_retry(
    db_session: Session,
    flow: models.Flow,
    client: TestClient,
) -> None:
    """Direct launch, queued dispatch and a retry retain independently selected revisions."""
    original = _select(db_session, flow, None)
    run = original.execution_log
    queued = _run(db_session, flow)
    assert client.get(_url(queued)).json()["configurations"] == []
    path = f"/api/v1/flows/{flow.id}"
    changed = client.put(
        path, headers={"If-Match": '"1"'}, json={"prompt_template": "Revision B"}
    )
    assert changed.status_code == 200
    assert original.flow.prompt_template == "Revision A"
    worker = _select(db_session, flow, queued)
    assert worker.flow.prompt_template == "Revision B"
    retry = _run(db_session, flow, retry_of=run.id)
    _select(db_session, flow, retry)
    for execution, created, selected in ((run, 1, 1), (queued, 1, 2), (retry, 2, 2)):
        outcome = client.get(_url(execution)).json()
        assert outcome["creation_revision"] == created
        assert outcome["configurations"] == [{"revision": selected, "stage": "launch"}]
        body = client.get(f"{_url(execution)}/configurations/{selected}").json()[
            "definition_json"
        ]
        assert json.loads(body)["prompt_template"] == (
            "Revision A" if selected == 1 else "Revision B"
        )
        assert "opaque-env-value" not in body and "opaque-password" not in body
    assert client.get(_url(retry)).json()["retry_of_execution_id"] == str(run.id)
    assert client.get(f"{_url(run)}/configurations/2").status_code == 404


def test_review_delivery_matches_work(
    db_session: Session,
    flow: models.Flow,
    client: TestClient,
) -> None:
    """Require this execution's accepted review on the intended target and head."""
    run = _run(db_session, flow)
    crud.crud_flow_execution.update(
        db_session,
        db_obj=run,
        obj_in={
            "status": "SUCCEEDED",
            "result": {"status": "ok", "review_posted": True},
            "result_schema_validation": {"valid": True},
            "startup_receipt": {"_completion": {"capture_state": "captured"}},
        },
    )

    def read() -> dict:
        response = client.get(_url(run))
        assert response.status_code == 200
        return response.json()

    first = read()
    assert first["runtime"]["execution_status"] == "SUCCEEDED"
    assert first["work"]["pull_request"] == "17"
    assert first["output"] == {
        "validation": "valid",
        "capture": "captured",
        "evidence_artifact_id": None,
    }
    assert first["delivery"] == {"state": "unconfirmed", "destination": None}
    for number, head in ((18, "a"), (17, "b"), (17, "a")):
        receipt = crud.crud_review_publication_receipt.record(
            db_session,
            account_id=flow.account_id,
            execution_id=run.id,
            provider="github",
            repository="example/project",
            pull_request=number,
            review_action="comment",
            head_sha=head * 40,
            provider_review_id=1234,
        )
        if (number, head) != (17, "a"):
            assert read()["delivery"]["state"] == "unconfirmed"
    delivered = read()["delivery"]
    assert delivered["state"] == "delivered"
    assert delivered["destination"]["record_id"] == str(receipt.id)
    assert delivered["destination"]["provider_review_id"] == "1234"


def test_validation_and_legacy(
    db_session: Session, flow: models.Flow, client: TestClient
) -> None:
    """Legacy missing facts stay unknown, independently of validation and runtime."""
    run = _run(db_session, flow)
    crud.crud_flow_execution.update(
        db_session, db_obj=run, obj_in={"status": "FAILED", "flow_revision": None}
    )
    legacy = client.get(_url(run)).json()
    assert legacy["creation_revision"] is None and legacy["configurations"] == []
    assert legacy["output"]["capture"] == legacy["output"]["validation"] == "unknown"
    assert client.get(f"{_url(run)}/configurations/1").status_code == 404
    crud.crud_flow_execution.update(
        db_session, db_obj=run, obj_in={"result_schema_validation": {"valid": False}}
    )
    failed = client.get(_url(run)).json()
    assert failed["runtime"]["execution_status"] == "FAILED"
    assert failed["output"]["validation"] == "failed"
    assert failed["delivery"]["state"] == "unconfirmed"


def test_owned_pages_and_oversized_definition(
    db_session: Session,
    flow: models.Flow,
    client: TestClient,
    test_editor_user: models.User,
) -> None:
    """Reassemble a large definition and enforce the same scope on every page."""
    flow.prompt_template = "Long definition 📝 " * 2000
    db_session.commit()
    run = _select(db_session, flow, None).execution_log
    url = f"{_url(run)}/configurations/{flow.config_revision}"
    chunk = client.get(url).json()
    document = chunk["definition_json"]
    while chunk["next_offset"] is not None:
        chunk = client.get(url, params={"offset": chunk["next_offset"]}).json()
        assert len(chunk["definition_json"]) <= execution_outcome.DEFINITION_PAGE_SIZE
        document += chunk["definition_json"]
    assert json.loads(document)["prompt_template"] == flow.prompt_template
    foreign = crud.crud_flow.create(
        db_session,
        account_id=test_editor_user.account_id,
        flow_in=FlowCreate(
            name="Other flow",
            prompt_template="other",
            agent_type="codex",
            agent_config={},
        ),
    )
    other = _select(db_session, foreign, None).execution_log
    for suffix in ("", "/configurations/1"):
        assert client.get(_url(other) + suffix).status_code == 404
    for endpoint in (_url(run), url):
        assert (
            client.get(endpoint, params={"project_id": str(uuid4())}).status_code == 404
        )


def test_project_and_flow_visibility(
    db_session: Session,
    flow: models.Flow,
    test_tracker: models.Tracker,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope both reads even when the caller omits the optional project filter."""
    org = crud.crud_organization.create(
        db_session,
        obj_in={
            "name": "Example",
            "identifier": "example-org",
            "tracker_id": test_tracker.id,
        },
    )
    project = crud.crud_project.create(
        db_session,
        obj_in={
            "name": "Example project",
            "identifier": "17",
            "slug": "example/project",
            "organization_id": org.id,
        },
    )
    run = _run(db_session, flow)
    crud.crud_flow_execution.update(
        db_session,
        db_obj=run,
        obj_in={
            "trigger_event_details": {
                **run.trigger_event_details,
                "project_id": str(project.id),
            },
        },
    )
    _select(db_session, flow, run)
    endpoints = [_url(run), f"{_url(run)}/configurations/1"]
    assert [client.get(url).status_code for url in endpoints] == [404, 404]
    crud.crud_tracker_scope_rule.create(
        db_session,
        obj_in={
            "tracker_id": test_tracker.id,
            "scope_type": "ORGANIZATION",
            "rule_type": "INCLUDE",
            "identifier": org.identifier,
        },
    )
    assert [
        client.get(url, params={"project_id": str(project.id)}).status_code
        for url in endpoints
    ] == [200, 200]
    monkeypatch.setattr("preloop.api.endpoints.flows.get_authorizer", lambda: object())
    monkeypatch.setattr("preloop.api.endpoints.flows.filter_viewable", lambda *args: [])
    assert [client.get(url).status_code for url in endpoints] == [404, 404]


def test_controller_publication_and_forged_result(
    db_session: Session,
    flow: models.Flow,
    client: TestClient,
) -> None:
    """Only the controller's accepted destination or protected protocol record counts."""
    from preloop.models.crud.base import CRUDBase

    run = _run(db_session, flow)
    url = _url(run)
    pr = {
        "provider": "github",
        "repository_url": "https://github.com/example/project.git",
        "url": "https://github.com/example/project/pull/18",
        "status": "published",
        "head_sha": "a" * 40,
        "number": 18,
    }
    receipt = {"complete": True, "status": "complete", "repositories": [pr]}
    crud.crud_flow_execution.update(
        db_session, db_obj=run, obj_in={"result": {"trusted_publication": receipt}}
    )
    assert client.get(url).json()["delivery"]["state"] == "unconfirmed"
    # The protected completion belongs to the private runner protocol writer.
    CRUDBase(models.FlowExecution).update(
        db_session,
        db_obj=run,
        obj_in={
            "result": {
                "trusted_publication": receipt,
                "_private_publication": {"phase": "complete", "receipt": receipt},
            }
        },
    )
    legacy = client.get(url).json()["delivery"]["destination"]
    assert legacy["source"] == "private_publication_receipt"
    assert legacy["pull_request"] == "18" and legacy["destination_count"] == 1
    other = _select(db_session, flow, None)
    other._record_published_destination(pr)
    destination = client.get(_url(other.execution_log)).json()["delivery"][
        "destination"
    ]
    assert destination == {
        "kind": "pull_request",
        "provider": "github",
        "repository": "example/project",
        "pull_request": "18",
        "destination_count": 1,
        "source": "controller_publication",
    }


def test_platform_review_readback(
    db_session: Session, flow: models.Flow, client: TestClient
) -> None:
    """A status-only or unverified review does not prove delivery of the review."""
    run = _run(db_session, flow)
    publisher = crud.crud_platform_review_publication
    row = publisher.claim(
        db_session,
        account_id=flow.account_id,
        execution_id=run.id,
        repository="example/project",
        pull_request="17",
        head_sha="a" * 40,
    )
    assert row is not None
    for state, review, expected in (
        ("status_only", "skipped", "unconfirmed"),
        ("failed", "posted", "unconfirmed"),
        ("posted", "posted", "delivered"),
    ):
        publisher.record(
            db_session, row=row, state=state, review_state=review, review_id="23"
        )
        delivery = client.get(_url(run)).json()["delivery"]
        assert delivery["state"] == expected
    assert delivery["destination"]["source"] == "platform_review_publication"


@pytest.mark.asyncio
async def test_native_runner_reselects_after_launch(
    db_session: Session,
    flow: models.Flow,
    client: TestClient,
) -> None:
    """Redelivery can use a later flow revision and keeps both historical definitions."""
    from preloop.services.host_exec_delivery import hydrate_host_exec_job

    run = _select(db_session, flow, None).execution_log
    flow.prompt_template = "Revision B"
    db_session.commit()
    job = {"execution_id": str(run.id), "agent_type": "codex"}
    for _ in range(2):
        await hydrate_host_exec_job(db_session, job)
    selections = client.get(_url(run)).json()["configurations"]
    assert selections == [
        {"stage": "launch", "revision": 1},
        {"stage": "runner_delivery", "revision": 2},
    ]
    assert (
        json.loads(
            client.get(f"{_url(run)}/configurations/1").json()["definition_json"]
        )["prompt_template"]
        == "Revision A"
    )
