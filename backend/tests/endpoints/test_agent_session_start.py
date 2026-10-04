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
