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
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import event

from preloop.models import models
from preloop.models.schemas.approval_request import ApprovalRequestResponse
from preloop.services.approval_attribution import (
    attach_attribution,
    attributed,
    attributed_async,
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


def test_a_flow_run_is_not_filed_as_an_agent():
    """A flow runtime token names the flow, not an agent.

    ``flow_runtime_token`` sets ``runtime_principal = {type: flow_execution,
    name: <flow name>}`` and no managed agent id. Taking that name as the
    agent's would label the run "Agent Nightly audit" beside "Flow run
    Nightly audit", and put a flow name in the approval email subject.
    """
    caller = attribution_from_user_context(
        SimpleNamespace(
            managed_agent_id=None,
            api_key_id=str(uuid.uuid4()),
            flow_execution_id="a5b0d0e2-0000-4000-8000-000000000001",
            runtime_principal_type="flow_execution",
            runtime_principal_name="Nightly audit",
        )
    )
    assert caller.managed_agent_id is None
    assert caller.managed_agent_name is None
    assert caller.execution_id == "a5b0d0e2-0000-4000-8000-000000000001"


def test_a_name_without_a_parsable_agent_id_is_dropped():
    """A name is only an agent's name when there is an agent id to link it to."""
    caller = attribution_from_user_context(
        SimpleNamespace(
            managed_agent_id="not-a-uuid",
            runtime_principal_name="Claude Code (laptop)",
        )
    )
    assert caller.managed_agent_id is None
    assert caller.managed_agent_name is None


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
        account_id=test_user.account_id,
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
        account_id=world.account_id,
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
    request = _request(
        account_id=attributed_world.account_id,
        api_key_id=attributed_world.api_key.id,
    )

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


def test_an_id_from_another_account_is_never_resolved(db_session, attributed_world):
    """A mis-stamped id must not name another account's agent or key.

    The ids come off a row the reader already owns, so this is defence in
    depth rather than a live leak, but the lookup is scoped to the request's
    account so a wrong id resolves to nothing instead of to a stranger.
    """
    world = attributed_world
    request = _request(
        account_id=uuid.uuid4(),
        managed_agent_id=world.agent.id,
        runtime_session_id=world.session.id,
        api_key_id=world.api_key.id,
        execution_id=str(world.execution.id),
    )

    response = ApprovalRequestResponse.model_validate(attributed(db_session, request))

    assert response.agent is None
    assert response.api_key is None
    assert response.session is None
    assert response.flow_execution is None


def _scalar_result(rows):
    """A stand-in for ``Result`` as the attribution loader consumes it."""
    return MagicMock(
        scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
    )


@pytest.mark.asyncio
async def test_the_decide_endpoints_attribute_without_blocking(
    db_session, attributed_world
):
    """approve/decline/cancel are async, so attribution runs on their session.

    Four awaited statements, not four blocking ones on the event loop: the
    agent, the key, the session, and the run joined to its flow.
    """
    world = attributed_world
    request = _request(
        account_id=world.account_id,
        managed_agent_id=world.agent.id,
        runtime_session_id=world.session.id,
        api_key_id=world.api_key.id,
        execution_id=str(world.execution.id),
    )
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _scalar_result([world.agent]),
            _scalar_result([world.api_key]),
            _scalar_result([world.session]),
            MagicMock(all=MagicMock(return_value=[(world.execution, world.flow)])),
        ]
    )

    await attributed_async(db, request)

    assert db.execute.await_count == 4
    assert request.agent.name == "Claude Code (laptop)"
    assert request.api_key.name == "claude-code-laptop"
    assert request.session.subject == "feature/attribution"
    assert request.flow_execution.flow_name == "Nightly audit"


@contextmanager
def _counting_statements(session):
    """Collect every SQL statement issued inside the block."""
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = session.connection().engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_a_page_of_requests_is_attributed_in_one_batch(db_session, attributed_world):
    """The list endpoint must not issue four lookups per row.

    Counted, not assumed: asserting only the resolved names would pass just as
    happily if this degenerated into four queries per row, which is the whole
    property the batching exists for. A page costs one statement per kind of
    id present, four at most.
    """
    world = attributed_world
    requests = [
        _request(
            account_id=world.account_id,
            managed_agent_id=world.agent.id,
            runtime_session_id=world.session.id,
            api_key_id=world.api_key.id,
            execution_id=str(world.execution.id),
        )
        for _ in range(5)
    ]

    with _counting_statements(db_session) as statements:
        attach_attribution(db_session, requests)

    assert len(statements) == 4, statements
    assert all(r.agent.name == "Claude Code (laptop)" for r in requests)
    assert all(r.api_key.name == "claude-code-laptop" for r in requests)
    assert all(r.session.subject == "feature/attribution" for r in requests)
    assert all(r.flow_execution.flow_name == "Nightly audit" for r in requests)


def test_a_page_with_only_agents_costs_one_statement(db_session, attributed_world):
    """A kind of id that no row carries is not queried at all."""
    world = attributed_world
    requests = [
        _request(account_id=world.account_id, managed_agent_id=world.agent.id)
        for _ in range(5)
    ]

    with _counting_statements(db_session) as statements:
        attach_attribution(db_session, requests)

    assert len(statements) == 1, statements
    assert all(r.api_key is None for r in requests)


def test_attribution_failure_never_breaks_the_read():
    """A broken session degrades to no links, not to a 500."""
    broken = MagicMock()
    broken.execute.side_effect = RuntimeError("connection gone")
    request = _request(managed_agent_id=uuid.uuid4(), api_key_id=uuid.uuid4())

    attach_attribution(broken, [request])

    assert request.agent is None
    assert request.api_key is None


# --- The gates forward what they know -------------------------------------
#
# Each gate reads the caller's ids and hands them to create_and_notify.
# Dropping one kwarg at one of these sites re-creates the founder's bug with
# every unit test still green, so each gate gets a test that walks the real
# code path and inspects the kwargs that arrive.


def _caller_context():
    """An authenticated MCP caller carrying all four attribution ids."""
    from preloop.services.dynamic_mcp_server import UserContext

    ids = SimpleNamespace(
        account_id=uuid.uuid4(),
        agent=uuid.uuid4(),
        session=uuid.uuid4(),
        key=uuid.uuid4(),
        run="a5b0d0e2-0000-4000-8000-000000000001",
    )
    context = UserContext(
        user_id=str(uuid.uuid4()),
        account_id=str(ids.account_id),
        username="agent@example.com",
        has_tracker=True,
        flow_execution_id=ids.run,
        runtime_session_id=str(ids.session),
        api_key_id=str(ids.key),
        managed_agent_id=str(ids.agent),
        runtime_principal_name="Claude Code (laptop)",
    )
    return context, ids


def _capturing_approval_service(captured: dict):
    """An ApprovalService stand-in that records the creation kwargs.

    It raises straight after capturing: every gate handles a creation failure
    (the helper and the wrapper return an error, the MCP server propagates),
    so the test stops at the point it cares about instead of driving the
    whole wait-for-decision loop.
    """

    class _Service:
        def __init__(self, *args, **kwargs):
            pass

        async def create_and_notify(self, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("captured")

    return _Service


def _assert_forwarded(captured: dict, ids: SimpleNamespace) -> None:
    assert captured["managed_agent_id"] == ids.agent
    assert captured["runtime_session_id"] == ids.session
    assert captured["api_key_id"] == ids.key
    assert captured["execution_id"] == ids.run
    assert captured["managed_agent_name"] == "Claude Code (laptop)"


@pytest.mark.asyncio
async def test_the_builtin_gate_forwards_the_callers_ids():
    """approval_helper.require_approval: ask_user, request_approval, proxied tools."""
    from preloop.services.approval_helper import require_approval

    context, ids = _caller_context()
    captured: dict = {}

    config = MagicMock(id=uuid.uuid4(), approval_workflow_id=uuid.uuid4())
    workflow = MagicMock(id=config.approval_workflow_id, timeout_seconds=300)
    db = AsyncMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=None)
    db.execute = AsyncMock(
        side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=config)),
            MagicMock(scalars=MagicMock(return_value=iter([]))),
            MagicMock(scalar_one_or_none=MagicMock(return_value=workflow)),
        ]
    )

    with (
        patch("preloop.models.db.session.get_async_db_session", return_value=db),
        patch(
            "preloop.services.dynamic_fastmcp_http.get_current_user_context",
            return_value=context,
        ),
        patch(
            "preloop.services.approval_service.ApprovalService",
            _capturing_approval_service(captured),
        ),
    ):
        approved, error = await require_approval(
            tool_name="Bash",
            tool_source="builtin",
            account_id=str(ids.account_id),
            arguments={"command": "rm -rf build"},
            ctx=None,
        )

    assert approved is False  # the capture raised; the gate fails closed
    _assert_forwarded(captured, ids)


@pytest.mark.asyncio
async def test_the_decorator_gate_forwards_the_callers_ids():
    """approval_wrapper.with_approval: the @requires_approval decorator."""
    from preloop.services.approval_wrapper import with_approval

    context, ids = _caller_context()
    captured: dict = {}
    workflow_id = uuid.uuid4()

    db = AsyncMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=None)

    async def tool_func(**kwargs):
        return "tool_result"

    with (
        patch("preloop.models.db.session.get_async_db_session", return_value=db),
        patch(
            "preloop.services.dynamic_fastmcp_http.get_current_user_context",
            return_value=context,
        ),
        patch(
            "preloop.models.crud.tool_configuration.get_tool_config_by_name_and_source_async",
            AsyncMock(
                return_value=MagicMock(
                    id=uuid.uuid4(), approval_workflow_id=workflow_id
                )
            ),
        ),
        patch(
            "preloop.services.policy_evaluator.evaluate_policy_async",
            AsyncMock(return_value=("require_approval", workflow_id, "Needs a human")),
        ),
        patch(
            "preloop.models.crud.approval_workflow.get_approval_workflow_async",
            AsyncMock(return_value=MagicMock(id=workflow_id, timeout_seconds=300)),
        ),
        patch(
            "preloop.services.approval_service.ApprovalService",
            _capturing_approval_service(captured),
        ),
    ):
        result = await with_approval(tool_func)(arg="value")

    assert "Approval error" in result  # the capture raised
    _assert_forwarded(captured, ids)


@pytest.mark.asyncio
async def test_the_mcp_gate_forwards_the_callers_ids():
    """dynamic_mcp_server: ask_user and every dynamically registered tool."""
    from preloop.services.dynamic_mcp_server import DynamicMCPServer

    context, ids = _caller_context()
    captured: dict = {}

    config = MagicMock(id=uuid.uuid4(), approval_workflow_id=uuid.uuid4())
    workflow = MagicMock(id=config.approval_workflow_id, timeout_seconds=300)
    db = AsyncMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=None)
    db.execute = AsyncMock(
        side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=config)),
            MagicMock(scalar_one_or_none=MagicMock(return_value=workflow)),
        ]
    )

    with (
        patch("preloop.models.db.session.get_async_db_session", return_value=db),
        patch(
            "preloop.services.approval_service.ApprovalService",
            _capturing_approval_service(captured),
        ),
        pytest.raises(RuntimeError),
    ):
        await DynamicMCPServer()._request_and_wait_for_approval(
            context, "Bash", {"command": "rm -rf build"}
        )

    _assert_forwarded(captured, ids)
