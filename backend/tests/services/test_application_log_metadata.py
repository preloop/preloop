"""Application logging uses metadata across tool and approval paths."""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture

from preloop.services import approval_service, policy_evaluator
from preloop.services.policy.schema import SensitiveDataConfig
from preloop.services.policy_evaluator import evaluate_policy_async
from preloop.services.sensitive_data import storage
from tests.services import test_sensitive_data_reference_only as reference_tests

proxied = reference_tests.proxied
salts = reference_tests.salts
user_context = reference_tests.user_context

MARKER = "application-log-value-marker"


def assert_metadata_only(caplog: pytest.LogCaptureFixture) -> None:
    """Check both formatted output and the original logging record payload."""
    records = [r for r in caplog.records if r.levelno >= logging.INFO]
    assert records
    for record in records:
        assert MARKER not in record.getMessage(), record.name
        assert MARKER not in repr(record.args), record.name
        assert MARKER not in repr(record.__dict__), record.name
    assert MARKER not in "\n".join(logging.Formatter().format(r) for r in records)


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_only", [False, True])
@pytest.mark.parametrize("upstream_error", [False, True])
async def test_firewall_evaluates_rules_without_logging_values(
    proxied: tuple,
    salts: dict,
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
    reference_only: bool,
    upstream_error: bool,
) -> None:
    """Run the actual evaluator and firewall with a mocked upstream transport."""
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="preloop")
    mcp, client, audit, activity = proxied
    config = SensitiveDataConfig.model_validate(
        {
            "reference_only": [
                {"id": "tool-reference", "scope": {"tools": ["get_patient_record"]}}
            ]
        }
        if reference_only
        else {}
    )
    mocker.patch.object(storage, "resolve_config", return_value=config)
    mocker.patch.object(storage, "has_cached_config", return_value=True)
    mocker.patch.object(
        policy_evaluator, "evaluate_policy_async", evaluate_policy_async
    )
    mocker.patch.object(
        policy_evaluator, "get_meta_data_async", AsyncMock(return_value={})
    )
    tool_config = SimpleNamespace(id=uuid.uuid4(), approval_workflow_id=None)
    for name in (
        "get_tool_config_by_id_async",
        "get_tool_config_by_tool_name_async",
        "get_tool_config_for_route_async",
    ):
        mocker.patch.object(policy_evaluator, name, AsyncMock(return_value=tool_config))
    mocker.patch.object(
        policy_evaluator,
        "get_default_approval_workflow_async",
        AsyncMock(return_value=None),
    )
    rule = SimpleNamespace(
        id=uuid.uuid4(),
        priority=1,
        action="allow",
        condition_type="simple",
        condition_expression=f'args.note == "{MARKER}"',
        description=MARKER,
        approval_workflow_id=None,
    )
    mocker.patch.object(
        policy_evaluator, "get_multi_by_config_async", AsyncMock(return_value=[rule])
    )
    mocker.patch.object(
        policy_evaluator, "_get_audit_service", return_value=MagicMock()
    )
    if upstream_error:
        client.call_tool.side_effect = RuntimeError(MARKER)
    else:
        client.call_tool.return_value = [SimpleNamespace(text=MARKER)]
    result = await mcp.call_tool(
        "get_patient_record", {"note": MARKER, "consent_id": "example"}
    )
    client.call_tool.assert_awaited_once()
    assert result.is_error is upstream_error
    assert_metadata_only(caplog)
    evaluated = [
        r for r in caplog.records if r.getMessage().startswith("Evaluating rule ")
    ]
    assert evaluated and all(r.levelno == logging.DEBUG for r in evaluated)
    assert "['consent_id', 'note']" in evaluated[0].getMessage()
    assert MARKER not in evaluated[0].getMessage()


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_only", [False, True])
async def test_approval_event_failure_logs_metadata(
    salts: dict,
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
    reference_only: bool,
) -> None:
    """Approval comments and SQL parameters stay out of failure records."""
    from sqlalchemy.exc import StatementError

    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="preloop")
    config = SensitiveDataConfig.model_validate(
        {
            "reference_only": [
                {"id": "tool-reference", "scope": {"tools": ["example_tool"]}}
            ]
        }
        if reference_only
        else {}
    )
    mocker.patch.object(storage, "resolve_config", return_value=config)
    service = approval_service.ApprovalService.__new__(approval_service.ApprovalService)
    service.db = AsyncMock()
    service.db.add = MagicMock()
    service.db.flush.side_effect = StatementError(
        "write failed", "INSERT", {"comment": MARKER}, RuntimeError(MARKER)
    )
    await service._record_event(
        approval_request_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        event_type="approved",
        detail="Decision recorded",
        comment=MARKER,
    )
    assert_metadata_only(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_only", [False, True])
async def test_approval_notifications_and_comments_log_metadata(
    salts: dict,
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
    reference_only: bool,
) -> None:
    """Exercise the approval gate, progress messages, and decided comment."""
    from preloop.services.approval_helper import require_approval

    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="preloop")
    config = SensitiveDataConfig.model_validate(
        {
            "reference_only": [
                {"id": "tool-reference", "scope": {"tools": ["example_tool"]}}
            ]
        }
        if reference_only
        else {}
    )
    mocker.patch.object(storage, "resolve_config", return_value=config)
    db = AsyncMock()
    db.__aenter__.return_value = db
    tool = SimpleNamespace(id=uuid.uuid4(), approval_workflow_id=uuid.uuid4())
    workflow = SimpleNamespace(
        id=tool.approval_workflow_id,
        name="Example approval",
        approval_config={},
        approver_user_ids=[],
        approval_type="standard",
        channel=None,
        user=None,
        timeout_seconds=60,
        notification_channels=["email"],
        escalation_user_ids=None,
        escalation_team_ids=None,
        async_approval_enabled=False,
    )
    request = MagicMock()
    request.id = uuid.uuid4()
    request.status = "approved"
    request.approver_comment = MARKER
    request.tool_args = {"note": MARKER}
    results = [MagicMock() for _ in range(4)]
    results[0].scalars.return_value.first.return_value = tool
    results[1].scalars.return_value = iter([])
    results[2].scalar_one_or_none.return_value = workflow
    results[3].scalar_one_or_none.return_value = request
    db.execute.side_effect = results
    service = AsyncMock()
    service.create_and_notify.return_value = request
    mocker.patch("preloop.models.db.session.get_async_db_session", return_value=db)
    mocker.patch.object(approval_service, "ApprovalService", return_value=service)
    ctx = AsyncMock()
    ctx.request_context = MagicMock()
    ctx.request_context.meta = {"progressToken": MARKER}
    ctx.report_progress.return_value = None
    approved, error = await require_approval(
        tool_name="example_tool",
        tool_source="mcp",
        account_id=str(uuid.uuid4()),
        arguments={"note": MARKER},
        ctx=ctx,
    )
    assert approved, error
    service.create_and_notify.assert_awaited_once()
    assert_metadata_only(caplog)


def test_audit_writer_failure_logs_only_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SQL parameters, chained errors, and diagnostic strings are omitted."""
    from sqlalchemy.exc import StatementError
    from preloop.services.audit.async_writer import log_best_effort_failure

    caplog.set_level(logging.DEBUG)
    try:
        raise ValueError(MARKER)
    except ValueError as cause:
        try:
            raise StatementError(
                MARKER, "INSERT", {"tool_args": MARKER}, cause
            ) from cause
        except StatementError as error:
            log_best_effort_failure(
                logging.getLogger(__name__),
                error,
                summary="Audit write failed",
                diagnostic=MARKER,
            )
    assert_metadata_only(caplog)
    assert "StatementError" in caplog.text


@pytest.mark.asyncio
async def test_mcp_transport_failure_logs_only_metadata(
    caplog: pytest.LogCaptureFixture, mocker: MockerFixture
) -> None:
    """Upstream errors can echo arguments and headers in exception text."""
    from contextlib import asynccontextmanager
    from collections.abc import AsyncIterator
    from typing import Any
    from preloop.services.mcp_client_pool import MCPClient, MCPToolOutcomeUnknownError

    caplog.set_level(logging.DEBUG)
    session = AsyncMock()
    session.call_tool.side_effect = RuntimeError(MARKER)

    @asynccontextmanager
    async def context(**kwargs: Any) -> AsyncIterator[tuple]:
        yield session, MagicMock()

    client = MCPClient("https://example.com/mcp")
    client._connected = True
    mocker.patch.object(client, "_raw_session_context", context)
    with pytest.raises(MCPToolOutcomeUnknownError):
        await client.call_tool("example_tool", {"note": MARKER})
    session.call_tool.assert_awaited_once()
    assert_metadata_only(caplog)


def test_model_translation_failure_logs_only_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Model request translation failures preserve metadata without content."""
    from tests.services.test_gateway_translation_error import _run_stream

    caplog.set_level(logging.DEBUG)
    _run_stream(TypeError(MARKER))
    assert_metadata_only(caplog)


def test_application_errors_do_not_attach_exception_payloads() -> None:
    """Error logging must opt for metadata instead of body-bearing tracebacks."""
    import ast
    from pathlib import Path

    source_root = Path(policy_evaluator.__file__).parents[1]
    violations = []
    for path in source_root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            if node.func.attr not in {
                "info",
                "warning",
                "error",
                "exception",
                "critical",
            }:
                continue
            if not any(
                name in ast.unparse(node.func.value).lower()
                for name in ("log", "logging")
            ):
                continue
            loguru_options = [
                keyword
                for call in ast.walk(node.func.value)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "opt"
                for keyword in call.keywords
            ]
            if node.func.attr == "exception" or any(
                k.arg in {"exc_info", "exception"}
                and not (isinstance(k.value, ast.Constant) and k.value.value is False)
                for k in [*node.keywords, *loguru_options]
            ):
                violations.append(f"{path.relative_to(source_root)}:{node.lineno}")
    assert not violations


@pytest.mark.asyncio
async def test_async_database_cleanup_logs_metadata() -> None:
    """Loguru cleanup callbacks omit SQL parameters and exception payloads."""
    import asyncio

    from sqlalchemy.exc import StatementError

    from preloop.models.db import session

    messages: list = []
    sink = session.logger.add(messages.append, format="{message}", level="DEBUG")
    try:
        future = asyncio.get_running_loop().create_future()
        future.set_exception(
            StatementError(MARKER, "INSERT", {"note": MARKER}, ValueError(MARKER))
        )
        session._async_close_done(future)
    finally:
        session.logger.remove(sink)
    assert messages
    assert all(MARKER not in str(message) for message in messages)
    assert all(message.record["exception"] is None for message in messages)
    assert "StatementError" in str(messages[-1])
