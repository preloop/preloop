"""Conditional flow writes and triggers (#2375).

A flow carries a configuration revision, exposed as ``revision`` and as the
strong ``ETag`` ``"<revision>"``. Writes and triggers that send ``If-Match``
apply only at that revision; a stale one is refused with 412 and changes
nothing. Requests without ``If-Match`` behave as before.
"""

import importlib
import threading
import uuid
from typing import Any, Dict, Iterator
from unittest.mock import AsyncMock

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from preloop.api.endpoints import flows as flow_endpoints
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_flow,
    crud_flow_execution,
    crud_user,
)
from preloop.models.schemas.flow import FlowCreate, FlowResponse, FlowUpdate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.models.schemas.flow_revision import (
    FlowRevisionMismatchError,
    MalformedIfMatchError,
    parse_if_match,
)
from preloop.services.flow_trigger_service import FlowTriggerService

FLOWS = "/api/v1/flows"


@pytest.fixture(autouse=True)
def account_owner(db_session: Session, test_user: Any) -> Any:
    """Make the caller the account's primary user, whom the OSS check admits."""
    account = crud_account.get(db_session, id=test_user.account_id)
    account.primary_user_id = test_user.id
    db_session.flush()
    return account


def _flow(db: Session, account_id: Any, name: str = "Nightly review") -> Any:
    flow = crud_flow.create(
        db,
        account_id=account_id,
        flow_in=FlowCreate(
            name=name,
            description="Reviews the day's changes",
            prompt_template="Review {{trigger_event.payload}}",
            agent_type="codex",
            agent_config={"limits": {"max_turns": 40}},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
        ),
    )
    db.flush()
    return flow


@pytest.fixture
def flow(db_session: Session, test_user: Any) -> Any:
    """An ordinary flow in the caller's account, at revision 1."""
    return _flow(db_session, test_user.account_id)


def _stored(db: Session, flow_id: Any) -> Dict[str, Any]:
    """Every field of the stored flow, as the API serializes it."""
    db.expire_all()
    row = crud_flow.get(db, id=flow_id)
    return FlowResponse.model_validate(row).model_dump(mode="json")


def _executions(db: Session, flow_id: Any) -> list[Any]:
    return list(crud_flow_execution.get_by_flow(db, flow_id=flow_id))


# --- If-Match parsing ---------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "revision", "matches"),
    [
        ('"3"', 3, True),
        ('"3"', 4, False),
        ('"2", "3"', 3, True),
        ('W/"3"', 3, False),  # If-Match uses the strong comparison
        ("*", 99, True),
        (' "3" ', 3, True),
        ('"a,b", "3"', 3, True),  # a comma inside a tag is not a separator
    ],
)
def test_if_match_strong_comparison(header: str, revision: int, matches: bool) -> None:
    condition = parse_if_match(header)
    assert condition is not None
    assert condition.matches(revision) is matches


@pytest.mark.parametrize("header", ["", "3", '"3', '"3",', 'abc"3"', '"3" "4"'])
def test_malformed_if_match_is_rejected(header: str) -> None:
    with pytest.raises(MalformedIfMatchError):
        parse_if_match(header)


def test_no_header_is_no_condition() -> None:
    assert parse_if_match(None) is None


# --- read ---------------------------------------------------------------------


def test_read_carries_revision_and_etag(client: TestClient, flow: Any) -> None:
    response = client.get(f"{FLOWS}/{flow.id}")
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 1
    assert response.headers["ETag"] == '"1"'


# --- update -------------------------------------------------------------------


def test_conditional_update_applies_and_bumps(
    client: TestClient, db_session: Session, flow: Any
) -> None:
    response = client.put(
        f"{FLOWS}/{flow.id}",
        json={"prompt_template": "Review only the changed files"},
        headers={"If-Match": '"1"'},
    )
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 2
    assert response.json()["prompt_template"] == "Review only the changed files"
    assert response.headers["ETag"] == '"2"'
    stored = _stored(db_session, flow.id)
    assert stored["revision"] == 2
    assert stored["prompt_template"] == "Review only the changed files"
    reread = client.get(f"{FLOWS}/{flow.id}")
    assert reread.headers["ETag"] == '"2"'


def test_stale_update_is_412_and_changes_nothing(
    client: TestClient, db_session: Session, flow: Any
) -> None:
    """Client A read revision 1; client B wrote since; A's write is refused."""
    b = client.put(f"{FLOWS}/{flow.id}", json={"description": "Edited by B"})
    assert b.status_code == 200, b.text
    assert b.headers["ETag"] == '"2"'
    before = _stored(db_session, flow.id)

    a = client.put(
        f"{FLOWS}/{flow.id}",
        json={
            "description": "Edited by A",
            "prompt_template": "A's prompt",
            "agent_config": {"limits": {"max_turns": 5}},
        },
        headers={"If-Match": '"1"'},
    )

    assert a.status_code == 412, a.text
    detail = a.json()["detail"]
    assert detail["code"] == "flow_revision_mismatch"
    assert detail["current_revision"] == 2
    assert a.headers["ETag"] == '"2"'
    after = _stored(db_session, flow.id)
    assert after == before  # every field, including updated_at
    assert after["description"] == "Edited by B"
    assert after["agent_config"]["limits"] == {"max_turns": 40}


def test_unconditional_update_still_works_and_returns_etag(
    client: TestClient, db_session: Session, flow: Any
) -> None:
    client.put(f"{FLOWS}/{flow.id}", json={"description": "first"})
    response = client.put(f"{FLOWS}/{flow.id}", json={"description": "second"})
    assert response.status_code == 200, response.text
    assert response.json()["description"] == "second"
    assert response.json()["revision"] == 3
    assert response.headers["ETag"] == '"3"'


def test_if_match_star_requires_only_existence(
    client: TestClient, db_session: Session, flow: Any
) -> None:
    client.put(f"{FLOWS}/{flow.id}", json={"description": "moved on"})
    response = client.put(
        f"{FLOWS}/{flow.id}", json={"description": "any"}, headers={"If-Match": "*"}
    )
    assert response.status_code == 200, response.text
    assert response.headers["ETag"] == '"3"'
    missing = client.put(
        f"{FLOWS}/{uuid.uuid4()}", json={"description": "x"}, headers={"If-Match": "*"}
    )
    assert missing.status_code == 404


@pytest.mark.parametrize("header", ["1", '"1', ""])
def test_malformed_if_match_is_400_and_changes_nothing(
    client: TestClient, db_session: Session, flow: Any, header: str
) -> None:
    before = _stored(db_session, flow.id)
    response = client.put(
        f"{FLOWS}/{flow.id}", json={"description": "x"}, headers={"If-Match": header}
    )
    assert response.status_code == 400, response.text
    assert _stored(db_session, flow.id) == before


def test_weak_etag_never_matches(
    client: TestClient, db_session: Session, flow: Any
) -> None:
    response = client.put(
        f"{FLOWS}/{flow.id}", json={"description": "x"}, headers={"If-Match": 'W/"1"'}
    )
    assert response.status_code == 412, response.text
    assert _stored(db_session, flow.id)["description"] == "Reviews the day's changes"


def test_resending_the_stored_values_keeps_the_revision(
    client: TestClient, db_session: Session, flow: Any
) -> None:
    current = client.get(f"{FLOWS}/{flow.id}").json()
    response = client.put(
        f"{FLOWS}/{flow.id}",
        json={
            "name": current["name"],
            "description": current["description"],
            "prompt_template": current["prompt_template"],
        },
        headers={"If-Match": '"1"'},
    )
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 1
    assert response.headers["ETag"] == '"1"'


def test_enable_disable_is_a_configuration_change(
    client: TestClient, db_session: Session, flow: Any
) -> None:
    off = client.put(
        f"{FLOWS}/{flow.id}", json={"is_enabled": False}, headers={"If-Match": '"1"'}
    )
    assert off.status_code == 200, off.text
    assert off.json()["is_enabled"] is False
    assert off.headers["ETag"] == '"2"'
    stale = client.put(
        f"{FLOWS}/{flow.id}", json={"is_enabled": True}, headers={"If-Match": '"1"'}
    )
    assert stale.status_code == 412
    assert _stored(db_session, flow.id)["is_enabled"] is False


# --- what bumps and what does not --------------------------------------------


def test_non_configuration_writes_keep_the_revision(
    db_session: Session, flow: Any
) -> None:
    """Preset notices, executions and their counters never move it."""
    flow.preset_update_available = True
    flow.source_prompt_hash = "0" * 32
    flow.prompt_customized = True
    db_session.flush()
    execution = crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(flow_id=flow.id, status="PENDING"),
    )
    execution.status = "SUCCEEDED"
    execution.tool_calls_count = 12
    execution.total_tokens = 3400
    db_session.flush()
    assert _stored(db_session, flow.id)["revision"] == 1


def test_every_orm_configuration_write_bumps_once_per_flush(
    db_session: Session, flow: Any
) -> None:
    """Writers outside the PUT route (preset sync, model delete) are covered."""
    flow.prompt_template = "Synced from the preset"
    flow.allowed_mcp_tools = [{"name": "get_issue"}]
    db_session.flush()
    db_session.refresh(flow)
    assert flow.config_revision == 2
    crud_flow.update(
        db_session,
        db_obj=flow,
        flow_in=FlowUpdate(agent_config={"limits": {"max_turns": 9}}),
        account_id=flow.account_id,
    )
    assert flow.config_revision == 3


# --- other write endpoints ----------------------------------------------------


def _webhook_flow(client: TestClient, flow: Any) -> int:
    response = client.put(
        f"{FLOWS}/{flow.id}",
        json={"trigger_event_source": "webhook", "trigger_event_types": ["webhook"]},
    )
    assert response.status_code == 200, response.text
    return int(response.json()["revision"])


def test_secret_rotation_is_conditional(
    client: TestClient, db_session: Session, flow: Any
) -> None:
    revision = _webhook_flow(client, flow)
    before = _stored(db_session, flow.id)
    stale = client.post(
        f"{FLOWS}/{flow.id}/webhook-secret/rotate",
        headers={"If-Match": f'"{revision - 1}"'},
    )
    assert stale.status_code == 412, stale.text
    assert stale.json()["detail"]["current_revision"] == revision
    assert _stored(db_session, flow.id) == before

    fresh = client.post(
        f"{FLOWS}/{flow.id}/webhook-secret/rotate",
        headers={"If-Match": f'"{revision}"'},
    )
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["webhook_secret"]
    assert fresh.json()["revision"] == revision + 1
    assert fresh.headers["ETag"] == f'"{revision + 1}"'


def _linked_to_preset(db: Session, flow: Any) -> Any:
    preset = _flow(db, flow.account_id, name="Review preset")
    preset.is_preset = True
    preset.prompt_template = "Preset prompt v2"
    flow.source_preset_id = preset.id
    flow.preset_update_available = True
    db.flush()
    db.refresh(flow)
    return preset


def test_apply_preset_update_is_conditional(
    client: TestClient, db_session: Session, flow: Any
) -> None:
    _linked_to_preset(db_session, flow)
    revision = flow.config_revision
    before = _stored(db_session, flow.id)
    stale = client.post(
        f"{FLOWS}/{flow.id}/apply-preset-update",
        headers={"If-Match": f'"{revision - 1}"'},
    )
    assert stale.status_code == 412, stale.text
    assert _stored(db_session, flow.id) == before

    applied = client.post(
        f"{FLOWS}/{flow.id}/apply-preset-update",
        headers={"If-Match": f'"{revision}"'},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["prompt_template"] == "Preset prompt v2"
    assert applied.json()["revision"] == revision + 1
    assert applied.headers["ETag"] == f'"{revision + 1}"'


def test_dismiss_preset_update_is_conditional_and_keeps_the_revision(
    client: TestClient, db_session: Session, flow: Any
) -> None:
    _linked_to_preset(db_session, flow)
    revision = flow.config_revision
    stale = client.post(
        f"{FLOWS}/{flow.id}/dismiss-preset-update",
        headers={"If-Match": f'"{revision - 1}"'},
    )
    assert stale.status_code == 412, stale.text
    assert _stored(db_session, flow.id)["preset_update_available"] is True

    dismissed = client.post(
        f"{FLOWS}/{flow.id}/dismiss-preset-update",
        headers={"If-Match": f'"{revision}"'},
    )
    assert dismissed.status_code == 200, dismissed.text
    assert dismissed.json()["preset_update_available"] is False
    assert dismissed.json()["revision"] == revision
    assert dismissed.headers["ETag"] == f'"{revision}"'


# --- dispatch -----------------------------------------------------------------


@pytest.fixture
def no_dispatch(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stop after the execution row exists: no NATS, no orchestrator."""
    start = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "_start_flow_execution", start)
    monkeypatch.setattr(
        "preloop.services.flow_trigger_service.get_nats_client",
        AsyncMock(return_value=None),
    )
    return start


def test_execution_records_the_revision_it_was_created_with(
    client: TestClient, db_session: Session, flow: Any, no_dispatch: AsyncMock
) -> None:
    first = client.post(f"{FLOWS}/{flow.id}/trigger", json={})
    assert first.status_code == 200, first.text
    client.put(f"{FLOWS}/{flow.id}", json={"description": "changed"})
    second = client.post(f"{FLOWS}/{flow.id}/trigger", json={})
    assert second.status_code == 200, second.text

    for body, revision in ((first.json(), 1), (second.json(), 2)):
        detail = client.get(f"{FLOWS}/executions/{body['id']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["flow_revision"] == revision


def test_trigger_with_current_revision_runs_it(
    client: TestClient, db_session: Session, flow: Any, no_dispatch: AsyncMock
) -> None:
    response = client.post(
        f"{FLOWS}/{flow.id}/trigger", json={}, headers={"If-Match": '"1"'}
    )
    assert response.status_code == 200, response.text
    [execution] = _executions(db_session, flow.id)
    assert execution.flow_revision == 1
    no_dispatch.assert_awaited_once()


@pytest.mark.parametrize("body", [{}, {"matrix": [{}, {}]}])
def test_trigger_with_stale_revision_creates_nothing(
    client: TestClient,
    db_session: Session,
    flow: Any,
    no_dispatch: AsyncMock,
    body: Dict[str, Any],
) -> None:
    client.put(f"{FLOWS}/{flow.id}", json={"description": "reviewed version moved"})
    response = client.post(
        f"{FLOWS}/{flow.id}/trigger", json=body, headers={"If-Match": '"1"'}
    )
    assert response.status_code == 412, response.text
    assert response.json()["detail"]["current_revision"] == 2
    assert response.headers["ETag"] == '"2"'
    assert _executions(db_session, flow.id) == []
    no_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_checks_the_revision_under_lock_before_insert(
    db_session: Session, flow: Any, no_dispatch: AsyncMock
) -> None:
    """A configuration change between the endpoint's read and the insert."""
    stale = parse_if_match('"1"')
    flow.description = "changed after the caller's read"
    db_session.flush()
    with pytest.raises(FlowRevisionMismatchError) as raised:
        await FlowTriggerService(db_session).trigger_flow(
            flow_id=flow.id, test_mode=True, expected_revision=stale
        )
    assert raised.value.current_revision == 2
    assert _executions(db_session, flow.id) == []


def test_direct_call_without_headers_is_unconditional(
    db_session: Session, flow: Any, test_user: Any
) -> None:
    """Code that calls the route function directly keeps today's behaviour."""
    updated = flow_endpoints.update_flow(
        db=db_session,
        flow_id=flow.id,
        flow_in=FlowUpdate(description="direct"),
        current_user=test_user,
    )
    assert updated.config_revision == 2
    with pytest.raises(HTTPException) as raised:
        flow_endpoints.update_flow(
            db=db_session,
            flow_id=flow.id,
            flow_in=FlowUpdate(description="stale"),
            current_user=test_user,
            if_match='"1"',
        )
    assert raised.value.status_code == 412


# --- concurrent writers against PostgreSQL -------------------------------------


@pytest.fixture
def committed_flow(db_engine: Any) -> Iterator[tuple[Any, Any]]:
    """A flow and its owner committed for real, so two sessions can race."""
    with Session(db_engine) as db:
        account = crud_account.create(
            db, obj_in={"organization_name": f"Race {uuid.uuid4().hex[:8]}"}
        )
        user = crud_user.create(
            db,
            obj_in={
                "account_id": account.id,
                "email": f"race-{uuid.uuid4().hex[:8]}@example.com",
                "username": f"race-{uuid.uuid4().hex[:8]}",
                "full_name": "Jane Doe",
                "is_active": True,
                "email_verified": True,
                "hashed_password": "not-a-real-hash",
            },
        )
        account.primary_user_id = user.id
        flow = _flow(db, account.id, name="Race flow")
        db.commit()
        ids = (account.id, user.id, flow.id)
    yield ids[2], ids[1]
    with Session(db_engine) as db:
        db.execute(text("DELETE FROM flow WHERE id = :id"), {"id": ids[2]})
        db.execute(
            text("UPDATE account SET primary_user_id = NULL WHERE id = :id"),
            {"id": ids[0]},
        )
        db.execute(text('DELETE FROM "user" WHERE id = :id'), {"id": ids[1]})
        db.execute(text("DELETE FROM account WHERE id = :id"), {"id": ids[0]})
        db.commit()


def test_two_conditional_writers_exactly_one_wins(
    db_engine: Any, committed_flow: tuple[Any, Any]
) -> None:
    assert db_engine.dialect.name == "postgresql"  # row locks are the point
    flow_id, user_id = committed_flow
    barrier = threading.Barrier(2)
    outcomes: Dict[str, Any] = {}

    def writer(label: str) -> None:
        with Session(db_engine) as db:
            user = db.get(models.User, user_id)
            barrier.wait()
            try:
                flow_endpoints.update_flow(
                    db=db,
                    flow_id=flow_id,
                    flow_in=FlowUpdate(description=f"written by {label}"),
                    current_user=user,
                    if_match='"1"',
                )
                outcomes[label] = 200
            except HTTPException as exc:
                db.rollback()
                outcomes[label] = (exc.status_code, exc.detail["current_revision"])

    threads = [threading.Thread(target=writer, args=(name,)) for name in "AB"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes.values(), key=str) == [(412, 2), 200]
    winner = next(label for label, value in outcomes.items() if value == 200)
    with Session(db_engine) as db:
        stored = crud_flow.get(db, id=flow_id)
        assert stored.config_revision == 2
        assert stored.description == f"written by {winner}"


# --- migration ----------------------------------------------------------------


def test_migration_adds_and_drops_both_columns(db_session: Session, flow: Any) -> None:
    migration = importlib.import_module(
        "preloop.models.alembic.versions.20261222_flow_config_revision"
    )
    connection = db_session.connection()
    context = MigrationContext.configure(connection)

    def columns(table: str) -> Dict[str, Any]:
        return {c["name"]: c for c in inspect(connection).get_columns(table)}

    with Operations.context(context):
        migration.downgrade()
        assert "config_revision" not in columns("flow")
        assert "flow_revision" not in columns("flow_execution")
        migration.upgrade()
    flow_column = columns("flow")["config_revision"]
    assert flow_column["nullable"] is False
    assert "1" in str(flow_column["default"])
    assert columns("flow_execution")["flow_revision"]["nullable"] is True
    # The existing row took the default without a rewrite of its values.
    assert (
        connection.execute(
            text("SELECT config_revision FROM flow WHERE id = :id"), {"id": flow.id}
        ).scalar_one()
        == 1
    )
