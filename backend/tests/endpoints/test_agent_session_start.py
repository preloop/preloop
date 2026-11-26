"""SessionStart registration as the list endpoint and the hook see it (#1045)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from preloop.api.endpoints import agent_permission as endpoint
from preloop.models import models
from preloop.services.agent_session_lineage import register_session_start


def _start(db: Session, account_id: Any, external: str, prompt: str, **kw: Any):
    return register_session_start(
        db,
        account_id=account_id,
        principal_type="claude_code",
        principal_id="claude_machine_ep",
        principal_name=None,
        external_session_id=external,
        agent_kind="claude_code",
        cwd="/work/repo",
        first_prompt=prompt,
        **kw,
    )


@pytest.fixture
def account_id(test_user: models.User) -> Any:
    return test_user.account_id


def test_same_second_sessions_show_distinct_titles_in_the_list(
    client: Any, db_session: Session, account_id: Any
) -> None:
    first = _start(db_session, account_id, "ext-a", "Fix the login bug in api/")
    second = _start(
        db_session, account_id, "ext-b", "Write docs for token=sk-abcdef123456789"
    )
    response = client.get("/api/v1/runtime-sessions?limit=50")
    assert response.status_code == 200, response.text
    titles = {
        item["id"]: item["title"]
        for item in response.json()["items"]
        if item["id"] in {first.runtime_session_id, second.runtime_session_id}
    }
    assert len(titles) == 2
    assert len(set(titles.values())) == 2
    assert titles[first.runtime_session_id] == "Fix the login bug in api/"
    assert "sk-abcdef123456789" not in titles[second.runtime_session_id]


def test_child_lineage_is_visible_on_the_session_detail(
    client: Any, db_session: Session, account_id: Any
) -> None:
    parent = _start(db_session, account_id, "ext-p", "conduct")
    child = _start(
        db_session,
        account_id,
        "ext-c",
        "work",
        parent_session_id=parent.runtime_session_id,
    )
    response = client.get(f"/api/v1/runtime-sessions/{child.runtime_session_id}")
    assert response.status_code == 200, response.text
    assert response.json()["session"]["parent_session_id"] == parent.runtime_session_id


def test_endpoint_requires_a_runtime_principal(db_session: Session) -> None:
    identity = endpoint.PermissionIdentity(
        account_id="00000000-0000-0000-0000-000000000000",
        user_id=None,  # type: ignore[arg-type]
        api_key_id=None,  # type: ignore[arg-type]
        managed_agent_id=None,
        runtime_session_id=None,
        managed_agent_name="Agent",
    )
    payload = endpoint.AgentSessionStartRequest(session_id="abc")
    with patch.object(endpoint, "get_session_factory") as factory:
        assert endpoint._register_session_start(identity, payload) is None
        factory.assert_not_called()


def test_alias_list_filter_and_note_target(client, db_session, account_id):
    parent = _start(db_session, account_id, "alias-parent", "conduct")
    workers = [
        _start(
            db_session,
            account_id,
            f"alias-worker-{i}",
            "",
            alias=f"brief-{i}",
            parent_session_id=parent.runtime_session_id,
        )
        for i in range(4)
    ]
    response = client.get(
        f"/api/v1/runtime-sessions?alias=brief-2&parent_session_id={parent.runtime_session_id}"
    )
    assert response.status_code == 200, response.text
    assert [(r["alias"], r["parent_session_id"]) for r in response.json()["items"]] == [
        ("brief-2", parent.runtime_session_id)
    ]
    for i, worker in enumerate(workers):
        response = client.post(
            "/api/v1/operator-notes",
            json={"alias": f"brief-{i}", "text": "Report progress"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["runtime_session_id"] == worker.runtime_session_id


def test_cli_children_and_external_targets(client, db_session, account_id):
    parent = _start(db_session, account_id, "cli-parent", "conduct")
    workers = [
        _start(
            db_session,
            account_id,
            f"cli-worker-{i}",
            "work",
            parent_session_id=parent.runtime_session_id,
        )
        for i in range(23)
    ]
    response = client.post(
        "/api/v1/operator-notes",
        json={
            "children": "all",
            "parent_session_id": parent.runtime_session_id,
            "text": "Report progress",
        },
    )
    assert response.status_code == 201, response.text
    notes = response.json()["notes"]
    assert len(notes) == 20
    assert {n["runtime_session_id"] for n in notes} == {
        w.runtime_session_id for w in workers[-20:]
    }
    response = client.post(
        "/api/v1/operator-notes",
        json={
            "children": "latest",
            "parent_session_id": parent.runtime_session_id,
            "text": "Report next step",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["runtime_session_id"] == workers[-1].runtime_session_id
    response = client.post(
        "/api/v1/operator-notes",
        json={"external_session_id": "cli-worker-0", "text": "Report"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["runtime_session_id"] == workers[0].runtime_session_id


def test_alias_rest_ambiguity_and_target_exclusivity(client, db_session, account_id):
    _start(db_session, account_id, "ambiguous-one", "", alias="brief")
    register_session_start(
        db_session,
        account_id=account_id,
        principal_type="codex_cli",
        principal_id="another-principal",
        principal_name="Worker",
        external_session_id="ambiguous-two",
        alias="brief",
    )
    response = client.post(
        "/api/v1/operator-notes", json={"alias": "brief", "text": "Report"}
    )
    assert response.status_code == 409, response.text
    assert "target_ambiguous" in response.json()["detail"]
    response = client.post(
        "/api/v1/operator-notes",
        json={
            "alias": "brief",
            "external_session_id": "ambiguous-one",
            "text": "Report",
        },
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("owned", [True, False])
def test_session_start_forwards_process_ownership(owned: bool) -> None:
    """Terminal resumes must replace stale sidecar ownership metadata."""
    identity = endpoint.PermissionIdentity(
        account_id="00000000-0000-0000-0000-000000000001",
        user_id=None,
        api_key_id=None,
        managed_agent_id=None,
        runtime_session_id=None,
        managed_agent_name="Agent",
        runtime_principal_type="claude_code",
        runtime_principal_id="example-host",
    )
    payload = endpoint.AgentSessionStartRequest(
        session_id="example-native", control_owned=owned
    )
    with (
        patch.object(endpoint, "get_session_factory"),
        patch(
            "preloop.services.agent_session_lineage.register_session_start",
            return_value=None,
        ) as register,
    ):
        endpoint._register_session_start(identity, payload)
    assert register.call_args.kwargs["control_owned"] is owned
