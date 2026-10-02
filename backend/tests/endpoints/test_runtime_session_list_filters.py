"""Session list filters and row labels for `preloop sessions list` (#1148).

Exercised through the HTTP endpoint against the real query so the filters are
proven server side, account scoped, and not left to the CLI to apply to a
page it happened to receive.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models import models


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _agent(db: Session, account_id: Any, name: str, kind: str) -> models.ManagedAgent:
    now = _now()
    agent = models.ManagedAgent(
        account_id=account_id,
        agent_kind=kind,
        session_source_type="managed_agent",
        session_source_id=f"{kind}_{uuid4().hex[:8]}",
        display_name=name,
        enrolled_via="cli",
        lifecycle_state="active",
        lifecycle_updated_at=now,
        last_seen_at=now,
    )
    db.add(agent)
    db.flush()
    return agent


def _session(
    db: Session,
    account_id: Any,
    source_id: str,
    *,
    source_type: str = "claude_code",
    agent: models.ManagedAgent | None = None,
    principal_suffix: str = "",
    started_ago: timedelta = timedelta(hours=1),
    active_ago: timedelta | None = timedelta(minutes=1),
    ended: bool = False,
    parent: models.RuntimeSession | None = None,
    cwd: str | None = None,
) -> models.RuntimeSession:
    now = _now()
    row = models.RuntimeSession(
        account_id=account_id,
        session_source_type=source_type,
        session_source_id=source_id,
        runtime_principal_type=agent.session_source_type if agent else None,
        runtime_principal_id=(
            agent.session_source_id + principal_suffix if agent else None
        ),
        started_at=now - started_ago,
        last_activity_at=now - active_ago if active_ago is not None else None,
        ended_at=now - timedelta(seconds=30) if ended else None,
        parent_session_id=parent.id if parent else None,
        cwd=cwd,
    )
    db.add(row)
    db.flush()
    return row


def _ids(response: Any) -> set[str]:
    assert response.status_code == 200, response.text
    return {item["id"] for item in response.json()["items"]}


@pytest.fixture
def account_id(test_user: models.User) -> Any:
    return test_user.account_id


def test_parent_filter_returns_only_children(
    client: Any, db_session: Session, account_id: Any
) -> None:
    parent = _session(db_session, account_id, "parent")
    child_a = _session(db_session, account_id, "child-a", parent=parent)
    child_b = _session(db_session, account_id, "child-b", parent=parent)
    _session(db_session, account_id, "unrelated")

    ids = _ids(client.get(f"/api/v1/runtime-sessions?parent_session_id={parent.id}"))

    assert ids == {str(child_a.id), str(child_b.id)}


def test_active_window_excludes_quiet_and_ended_sessions(
    client: Any, db_session: Session, account_id: Any
) -> None:
    recent = _session(db_session, account_id, "recent", active_ago=timedelta(minutes=2))
    just_started = _session(
        db_session,
        account_id,
        "just-started",
        started_ago=timedelta(minutes=1),
        active_ago=None,
    )
    _session(db_session, account_id, "quiet", active_ago=timedelta(minutes=11))
    _session(db_session, account_id, "ended", ended=True)

    ids = _ids(client.get("/api/v1/runtime-sessions?active_within_minutes=10"))

    assert ids == {str(recent.id), str(just_started.id)}


def test_agent_filter_accepts_id_or_name_and_includes_per_run_sessions(
    client: Any, db_session: Session, account_id: Any
) -> None:
    worker = _agent(db_session, account_id, "Worker One", "claude_code")
    other = _agent(db_session, account_id, "Worker Two", "claude_code")
    base = _session(db_session, account_id, "w1-base", agent=worker)
    per_run = _session(
        db_session, account_id, "w1-run", agent=worker, principal_suffix=":run-7"
    )
    _session(db_session, account_id, "w2", agent=other)
    expected = {str(base.id), str(per_run.id)}

    assert _ids(client.get(f"/api/v1/runtime-sessions?agent={worker.id}")) == expected
    assert _ids(client.get("/api/v1/runtime-sessions?agent=worker%20one")) == expected


def test_agent_filter_refuses_unknown_and_ambiguous_names(
    client: Any, db_session: Session, account_id: Any
) -> None:
    _agent(db_session, account_id, "Twin", "codex")
    _agent(db_session, account_id, "Twin", "codex")

    missing = client.get("/api/v1/runtime-sessions?agent=nobody")
    ambiguous = client.get("/api/v1/runtime-sessions?agent=Twin")

    assert missing.status_code == 404
    assert ambiguous.status_code == 409
    assert "agent id" in ambiguous.json()["detail"]


def test_kind_filter_matches_managed_agent_kind_and_source(
    client: Any, db_session: Session, account_id: Any
) -> None:
    hermes = _agent(db_session, account_id, "Hermes", "hermes")
    governed = _session(
        db_session, account_id, "h1", source_type="managed_agent", agent=hermes
    )
    pushed = _session(db_session, account_id, "pushed", source_type="hermes")
    _session(db_session, account_id, "cc", source_type="claude_code")

    assert _ids(client.get("/api/v1/runtime-sessions?agent_kind=hermes")) == {
        str(governed.id),
        str(pushed.id),
    }


def test_kind_filter_with_no_matching_agent_matches_nothing_rather_than_all(
    client: Any, db_session: Session, account_id: Any
) -> None:
    _session(db_session, account_id, "cc", source_type="claude_code")
    cursor_agent = _agent(db_session, account_id, "Cursor", "cursor")

    response = client.get(
        f"/api/v1/runtime-sessions?agent={cursor_agent.id}&agent_kind=codex"
    )

    assert _ids(response) == set()


def test_execution_filter_matches_legacy_row_and_usage_link(
    client: Any, db_session: Session, account_id: Any
) -> None:
    flow = models.Flow(
        account_id=account_id, name="Example", prompt_template="x", agent_config={}
    )
    db_session.add(flow)
    db_session.flush()
    execution = models.FlowExecution(flow_id=flow.id)
    db_session.add(execution)
    db_session.flush()
    legacy = _session(
        db_session, account_id, str(execution.id), source_type="flow_execution"
    )
    linked = _session(db_session, account_id, "linked")
    _session(db_session, account_id, "unlinked")
    db_session.add(
        models.ApiUsage(
            account_id=account_id,
            runtime_session_id=linked.id,
            flow_execution_id=execution.id,
            endpoint="/v1/messages",
            method="POST",
            status_code=200,
            duration=0.1,
            action_type="model_gateway",
            timestamp=_now() - timedelta(minutes=1),
        )
    )
    db_session.flush()

    ids = _ids(client.get(f"/api/v1/runtime-sessions?flow_execution_id={execution.id}"))

    assert ids == {str(legacy.id), str(linked.id)}


@pytest.mark.parametrize("param", ["parent_session_id", "flow_execution_id"])
def test_uuid_filters_reject_malformed_values(client: Any, param: str) -> None:
    assert client.get(f"/api/v1/runtime-sessions?{param}=nope").status_code == 422


def test_rows_carry_agent_cwd_tool_and_approval_labels(
    client: Any, db_session: Session, account_id: Any
) -> None:
    worker = _agent(db_session, account_id, "Builder", "claude_code")
    recorded = _session(
        db_session,
        account_id,
        "recorded",
        agent=worker,
        principal_suffix=":run-1",
        cwd="/work/alpha",
    )
    reported = _session(db_session, account_id, "reported")
    for _ in range(3):
        db_session.add(
            models.RuntimeSessionActivity(
                account_id=account_id,
                runtime_session_id=recorded.id,
                activity_type="tool_call",
                tool_name="Bash",
                status="allow",
                timestamp=_now(),
            )
        )
    for count in (2, 9, 5):
        db_session.add(
            models.ApiUsage(
                account_id=account_id,
                runtime_session_id=reported.id,
                endpoint="/usage/ingest",
                method="POST",
                status_code=200,
                duration=0.0,
                action_type="usage_import",
                tool_call_count=count,
                timestamp=_now(),
            )
        )
    workflow = models.ApprovalWorkflow(account_id=account_id, name="labels")
    tool = models.ToolConfiguration(
        account_id=account_id, tool_name="Bash", tool_source="builtin"
    )
    db_session.add_all([workflow, tool])
    db_session.flush()
    for status in ("pending", "pending", "approved"):
        db_session.add(
            models.ApprovalRequest(
                account_id=account_id,
                tool_configuration_id=tool.id,
                approval_workflow_id=workflow.id,
                runtime_session_id=recorded.id,
                tool_name="Bash",
                tool_args={},
                status=status,
            )
        )
    db_session.flush()

    response = client.get("/api/v1/runtime-sessions?limit=100")
    assert response.status_code == 200, response.text
    rows = {item["id"]: item for item in response.json()["items"]}

    first = rows[str(recorded.id)]
    assert first["managed_agent_id"] == str(worker.id)
    assert first["managed_agent_name"] == "Builder"
    assert first["agent_kind"] == "claude_code"
    assert first["cwd"] == "/work/alpha"
    assert first["tool_call_count"] == 3
    assert first["pending_approval_count"] == 2

    second = rows[str(reported.id)]
    assert second["tool_call_count"] == 9
    assert second["managed_agent_id"] is None
    assert second["cwd"] is None
    assert second["pending_approval_count"] == 0
