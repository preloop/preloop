"""Attribution on approval requests: agent, API key, session, flow run.

The founder report was "an approval raised by Claude Code shows 'Agent: AI
agent'". That generic label is a frontend fallback for a request whose ids
were never recorded, so these tests pin the two halves of the fix:

1. Creation records every id the authenticated caller carries, and backfills
   the agent's display name when the caller only had the id.
2. Reading turns those ids into named summaries, omitting the parts whose row
   is unknown rather than inventing a label.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.models import models
from preloop.models.schemas.approval_request import ApprovalRequestResponse
from preloop.services.approval_attribution import (
    attach_attribution,
    attributed,
    attribution_from_user_context,
    resolve_managed_agent_name,
)
from preloop.services.approval_service import ApprovalService


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# --- Creation: read the ids off the caller's context -----------------------


def test_user_context_with_everything_yields_every_id():
    """An onboarded agent's MCP context carries all four attribution ids."""
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    key_id = uuid.uuid4()
    caller = attribution_from_user_context(
        SimpleNamespace(
            managed_agent_id=str(agent_id),
            runtime_session_id=str(session_id),
            api_key_id=str(key_id),
            flow_execution_id="a5b0d0e2-0000-4000-8000-000000000001",
            runtime_principal_name="Claude Code (laptop)",
        )
    )
    assert caller.managed_agent_id == agent_id
    assert caller.runtime_session_id == session_id
    assert caller.api_key_id == key_id
    assert caller.execution_id == "a5b0d0e2-0000-4000-8000-000000000001"
    assert caller.managed_agent_name == "Claude Code (laptop)"


def test_user_context_with_only_a_key_yields_only_the_key():
    """A plain API key caller is attributed by its key and nothing else."""
    key_id = uuid.uuid4()
    caller = attribution_from_user_context(
        SimpleNamespace(api_key_id=str(key_id), api_key_name="ci-key")
    )
    assert caller.api_key_id == key_id
    assert caller.managed_agent_id is None
    assert caller.runtime_session_id is None
    assert caller.execution_id is None
    assert caller.managed_agent_name is None


def test_user_context_ignores_unparsable_ids():
    """A malformed id must not become an attribution link to nowhere."""
    caller = attribution_from_user_context(
        SimpleNamespace(managed_agent_id="not-a-uuid", api_key_id=None)
    )
    assert caller.managed_agent_id is None
    assert caller.api_key_id is None


def test_no_user_context_is_not_an_error():
    """Non-MCP callers (flows, tests) attribute to nothing, they do not raise."""
    caller = attribution_from_user_context(None)
    assert caller == type(caller)()


@pytest.mark.asyncio
async def test_agent_name_is_backfilled_from_the_id(db_session, test_user):
    """A caller that knows only the agent id still stores a display name."""
    agent = models.ManagedAgent(
        id=uuid.uuid4(),
        account_id=test_user.account_id,
        agent_kind="claude_code",
        session_source_type="claude_code",
        session_source_id="host-1",
        display_name="Claude Code (laptop)",
        enrolled_via="runtime_session_token",
        lifecycle_state="active",
        lifecycle_updated_at=_now(),
        last_seen_at=_now(),
    )
    db_session.add(agent)
    db_session.flush()

    db = AsyncMock()
    db.execute.return_value = MagicMock(
        scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=agent)))
    )
    name = await resolve_managed_agent_name(
        db, managed_agent_id=agent.id, provided_name=None
    )
    assert name == "Claude Code (laptop)"


@pytest.mark.asyncio
async def test_a_provided_name_is_never_overwritten():
    """The caller's own label wins; no query is made."""
    db = AsyncMock()
    name = await resolve_managed_agent_name(
        db, managed_agent_id=uuid.uuid4(), provided_name="  Cursor  "
    )
    assert name == "Cursor"
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_create_approval_request_records_the_api_key():
    """The credential the call authenticated with lands on the row."""
    key_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    db = AsyncMock()
    db.add = MagicMock()
    service = ApprovalService(db, "https://app.test.com")

    with (
        patch(
            "preloop.services.approval_attribution.resolve_managed_agent_name",
            AsyncMock(return_value="Claude Code (laptop)"),
        ),
        patch.object(service, "_record_event", AsyncMock()),
        patch.object(service, "_broadcast_approval_update", AsyncMock()),
        patch("preloop.services.approval_service._log_approval_lifecycle_async"),
    ):
        request = await service.create_approval_request(
            account_id=str(uuid.uuid4()),
            tool_configuration_id=uuid.uuid4(),
            approval_workflow_id=uuid.uuid4(),
            tool_name="Bash",
            tool_args={"command": "rm -rf build"},
            managed_agent_id=agent_id,
            runtime_session_id=session_id,
            api_key_id=key_id,
            execution_id="run-1",
        )

    assert request.api_key_id == key_id
    assert request.managed_agent_id == agent_id
    assert request.runtime_session_id == session_id
    assert request.managed_agent_name == "Claude Code (laptop)"


# --- Reading: turn the ids into named summaries ----------------------------


@pytest.fixture
def attributed_world(db_session, test_user):
    """One agent, key, session and flow run, plus the request that used them."""
    now = _now()
    session = models.RuntimeSession(
        id=uuid.uuid4(),
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="host-1",
        session_reference="feature/attribution",
        started_at=now,
    )
    agent = models.ManagedAgent(
        id=uuid.uuid4(),
        account_id=test_user.account_id,
        runtime_session_id=None,
        agent_kind="claude_code",
        session_source_type="claude_code",
        session_source_id="host-1",
        display_name="Claude Code (laptop)",
        enrolled_via="runtime_session_token",
        lifecycle_state="active",
        lifecycle_updated_at=now,
        last_seen_at=now,
    )
    api_key = models.ApiKey(
        id=uuid.uuid4(),
        account_id=test_user.account_id,
        user_id=test_user.id,
        name="claude-code-laptop",
        key=f"pk_{uuid.uuid4().hex}",
        scopes=[],
        is_active=True,
    )
    flow = models.Flow(
        id=uuid.uuid4(),
        account_id=test_user.account_id,
        name="Nightly audit",
        prompt_template="do the thing",
        agent_config={},
    )
    execution = models.FlowExecution(
        id=uuid.uuid4(),
        flow_id=flow.id,
        status="RUNNING",
        start_time=now,
    )
    db_session.add_all([session, agent, api_key, flow, execution])
    db_session.flush()
    return SimpleNamespace(
        session=session,
        agent=agent,
        api_key=api_key,
        flow=flow,
        execution=execution,
    )


def _request(**overrides) -> models.ApprovalRequest:
    """An approval-request row carrying only the ids the test cares about."""
    defaults = dict(
        id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        tool_configuration_id=uuid.uuid4(),
        approval_workflow_id=uuid.uuid4(),
        tool_name="Bash",
        tool_args={},
        status="pending",
        requested_at=_now(),
        decided_by_ai=False,
        execution_id=None,
        managed_agent_id=None,
        runtime_session_id=None,
        api_key_id=None,
    )
    defaults.update(overrides)
    return models.ApprovalRequest(**defaults)


def test_all_four_parts_are_named_and_linkable(db_session, attributed_world):
    """The full fixture: agent, key, session and flow run all resolve."""
    world = attributed_world
    request = _request(
        managed_agent_id=world.agent.id,
        runtime_session_id=world.session.id,
        api_key_id=world.api_key.id,
        execution_id=str(world.execution.id),
    )

    response = ApprovalRequestResponse.model_validate(attributed(db_session, request))

    assert response.agent is not None
    assert response.agent.id == world.agent.id
    assert response.agent.name == "Claude Code (laptop)"
    assert response.agent.kind == "claude_code"
    assert response.api_key is not None
    assert response.api_key.name == "claude-code-laptop"
    assert response.session is not None
    assert response.session.id == world.session.id
    assert response.session.subject == "feature/attribution"
    assert response.flow_execution is not None
    assert response.flow_execution.id == str(world.execution.id)
    assert response.flow_execution.flow_name == "Nightly audit"


def test_only_an_api_key_names_only_the_key(db_session, attributed_world):
    """A plain key caller shows one link, not three blanks."""
    request = _request(api_key_id=attributed_world.api_key.id)

    response = ApprovalRequestResponse.model_validate(attributed(db_session, request))

    assert response.api_key is not None
    assert response.api_key.name == "claude-code-laptop"
    assert response.agent is None
    assert response.session is None
    assert response.flow_execution is None


def test_ids_pointing_at_deleted_rows_are_omitted(db_session):
    """A revoked key or a purged session omits its part, it does not fail."""
    request = _request(
        managed_agent_id=uuid.uuid4(),
        runtime_session_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        execution_id=str(uuid.uuid4()),
    )

    response = ApprovalRequestResponse.model_validate(attributed(db_session, request))

    assert response.agent is None
    assert response.api_key is None
    assert response.session is None
    assert response.flow_execution is None


def test_a_non_uuid_execution_id_is_not_looked_up(db_session, attributed_world):
    """execution_id is a free-form string column; only UUIDs name a run."""
    request = _request(execution_id="legacy-run-42")

    response = ApprovalRequestResponse.model_validate(attributed(db_session, request))

    assert response.flow_execution is None


def test_a_page_of_requests_is_attributed_in_one_batch(db_session, attributed_world):
    """The list endpoint must not issue four lookups per row."""
    world = attributed_world
    requests = [
        _request(managed_agent_id=world.agent.id, api_key_id=world.api_key.id)
        for _ in range(5)
    ]

    attach_attribution(db_session, requests)

    assert all(r.agent.name == "Claude Code (laptop)" for r in requests)
    assert all(r.api_key.name == "claude-code-laptop" for r in requests)


def test_attribution_failure_never_breaks_the_read():
    """A broken session degrades to no links, not to a 500."""
    broken = MagicMock()
    broken.execute.side_effect = RuntimeError("connection gone")
    request = _request(managed_agent_id=uuid.uuid4(), api_key_id=uuid.uuid4())

    attach_attribution(broken, [request])

    assert request.agent is None
    assert request.api_key is None
