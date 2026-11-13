"""Native command rules apply to hosted read-only reviews.

A ``publication_mode: read_only`` review flow runs Codex in the default
``exec`` sandbox, so the Preloop permission hook gates its shell commands.
The run is triggered through the restricted CI route, product code mints the
review key, and the real permission-check route evaluates the account's real
rules: a ``deny`` rule denies a shell read and a ``require_approval`` rule
parks the run. Rule evaluation is not mocked.
"""

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import crud
from preloop.models.models.tool_access_rule import ToolAccessRule
from preloop.models.models.tool_configuration import ToolConfiguration
from tests.services.test_read_only_hook_binding_preflight import (  # noqa: F401
    PERMISSION_CHECK_URL,
    _trigger_and_mint,
    permission_client,
    review_flow,
)

SHELL_READ = {
    "source": "codex_cli",
    "tool_name": "Bash",
    "tool_input": {"command": "cat README.md"},
    "session_id": "synthetic-codex-session",
    "cwd": "/workspace",
}


class _AsyncOverSync:
    """Async session facade over the test transaction's sync session.

    The permission service opens an async session on its own connection,
    which cannot see the rolled-back test transaction. This forwards the
    async calls to the same sync session so the real rule evaluation and
    approval code read the rows this test wrote. It is DB plumbing only:
    no decision is stubbed.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    async def run_sync(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return fn(self._session, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._session, name)
        if name in {
            "execute",
            "scalar",
            "scalars",
            "flush",
            "commit",
            "rollback",
            "refresh",
            "get",
            "delete",
            "merge",
            "close",
        }:

            async def call(*args: Any, **kwargs: Any) -> Any:
                if name == "commit":
                    return self._session.flush()
                if name == "close":
                    return None
                return attr(*args, **kwargs)

            return call
        return attr


@pytest.fixture(autouse=True)
def _shared_async_session(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from preloop.services import agent_permission_service

    @asynccontextmanager
    async def session() -> AsyncIterator[Any]:
        yield _AsyncOverSync(db_session)

    monkeypatch.setattr(agent_permission_service, "get_async_db_session", session)

    from sqlalchemy.orm import sessionmaker

    from preloop.services import native_flow_park

    factory = sessionmaker(
        bind=db_session.connection(), join_transaction_mode="create_savepoint"
    )
    monkeypatch.setattr(native_flow_park, "_session_factory", lambda: factory)


def _shell_rule(db: Session, account_id: Any, action: str) -> None:
    config = ToolConfiguration(
        tool_name="Bash",
        tool_source="agent",
        account_id=account_id,
    )
    db.add(config)
    db.flush()
    db.add(
        ToolAccessRule(
            account_id=account_id,
            tool_configuration_id=config.id,
            condition_expression="true",
            condition_type="simple",
            action=action,
            priority=0,
            is_enabled=True,
        )
    )
    db.commit()


def _check(client: TestClient, token: str) -> Any:
    return client.post(
        PERMISSION_CHECK_URL,
        json=SHELL_READ,
        headers={"Authorization": f"Bearer {token}"},
    )


def test_deny_rule_denies_a_read_only_review_shell_read(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    review_flow: tuple[Any, ...],  # noqa: F811
    permission_client: TestClient,  # noqa: F811
) -> None:
    orch, token = _trigger_and_mint(db_session, monkeypatch, review_flow)
    key = crud.crud_api_key.get_by_key(db_session, key=token)
    assert key.scopes == ["mcp:read"]
    assert (orch.flow.agent_config or {}).get("sandbox_type") != "read-only"
    _shell_rule(db_session, key.account_id, "deny")

    response = _check(permission_client, token)

    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "deny"


def test_require_approval_rule_parks_a_read_only_review(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    review_flow: tuple[Any, ...],  # noqa: F811
    permission_client: TestClient,  # noqa: F811
) -> None:
    orch, token = _trigger_and_mint(db_session, monkeypatch, review_flow)
    key = crud.crud_api_key.get_by_key(db_session, key=token)
    _shell_rule(db_session, key.account_id, "require_approval")
    # The launch marks the run live before Codex starts; the CI trigger here
    # stops at dispatch.
    orch.execution_log.status = "RUNNING"
    db_session.commit()

    response = _check(permission_client, token)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision"] in {"park", "deny"}, body
    assert "paused" in body["reason"], body
    db_session.expire_all()
    execution = crud.crud_flow_execution.get(db_session, id=orch.execution_log.id)
    assert execution.park_request_id is not None, (execution.status, body)
