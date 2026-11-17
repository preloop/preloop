"""Credential masking at persistence and provider boundaries."""

from pathlib import Path
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from preloop.models.crud.flow_execution_log import crud_flow_execution_log
from preloop.utils.execution_credentials import CredentialMasker

TOKEN = "synthetic-unprefixed-execution-credential-123456789"


@pytest.mark.asyncio
async def test_streamed_line_reuses_fresh_masking_snapshot_for_delivery() -> None:
    """One inventory read per line also sees credentials registered mid-run."""
    import json

    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    db = MagicMock()
    nats = AsyncMock()
    subject = FlowExecutionOrchestrator(
        db=db, flow_id=uuid4(), trigger_event_data={}, nats_client=nats
    )
    subject.flow = SimpleNamespace(account_id=uuid4())
    subject.execution_log = SimpleNamespace(id=uuid4())
    subject.execution_logger = MagicMock()
    later = "synthetic-later-credential-987654321"

    async def run_db(operation: Callable[[Any], Any]) -> Any:
        return operation(db)

    with (
        patch(
            "preloop.models.crud.execution_credentials.for_execution",
            side_effect=[
                CredentialMasker.from_values([TOKEN]),
                CredentialMasker.from_values([TOKEN, later]),
            ],
        ) as lookup,
        patch("preloop.services.execution_realtime.run_db_async", side_effect=run_db),
        patch(
            "preloop.services.execution_realtime.crud_realtime.allocate_sequence",
            return_value=(1, 0),
        ),
    ):
        for index, line in enumerate((TOKEN, later)):
            await subject._process_agent_log_line(
                line,
                session_reference="example-session",
                previous_line="",
                log_count=index,
            )
    assert lookup.call_count == 2
    assert nats.publish.await_count == 2
    for call in nats.publish.await_args_list:
        event = json.loads(call.args[1])
        assert event["payload"]["line"] == "[REDACTED]"
    for call in subject.execution_logger.log_agent_output.call_args_list:
        assert call.args[0] == "[REDACTED]"


def test_log_persistence_masks_messages_and_metadata() -> None:
    """Log writers sanitize before native evidence parsing or DB insertion."""
    db = MagicMock()
    with patch(
        "preloop.models.crud.execution_credentials.for_execution",
        return_value=CredentialMasker.from_values([TOKEN]),
    ):
        row = crud_flow_execution_log.append_log(
            db,
            str(uuid4()),
            {"type": "log", "message": TOKEN, "metadata": {"output": TOKEN}},
            commit=False,
        )
    assert row.message == "[REDACTED]"
    assert row.metadata_ == {"output": "[REDACTED]"}


def test_gateway_masks_before_provider_entry_and_does_not_trust_body() -> None:
    """The authenticated key binds masking; a request field cannot select it."""
    from preloop.services.openai_gateway import gateway_database_scope

    bound = str(uuid4())
    service = SimpleNamespace(
        auth_context=SimpleNamespace(
            api_key=SimpleNamespace(context_data={"flow_execution_id": bound})
        ),
        db=MagicMock(),
        release_db_for_wait=MagicMock(),
        _close_owned_db=MagicMock(),
    )
    seen = []

    @gateway_database_scope
    def upstream(self: object, payload: dict) -> dict:
        seen.append(payload)
        return payload

    with patch(
        "preloop.models.crud.execution_credentials.for_execution",
        return_value=CredentialMasker.from_values([TOKEN]),
    ) as lookup:
        upstream(
            service,
            {
                "input": [{"type": "function_call_output", "output": TOKEN}],
                "flow_execution_id": str(uuid4()),
            },
        )
    assert seen[0]["input"][0]["output"] == "[REDACTED]"
    lookup.assert_called_once_with(service.db, bound)


def test_artifact_read_capability_ends_with_execution() -> None:
    """Read and write artifact capabilities obey the same execution lifetime."""
    from preloop.api.endpoints.flow_artifacts import authorize

    execution_id, flow_id, account_id = uuid4(), uuid4(), uuid4()
    execution = SimpleNamespace(
        id=execution_id, flow_id=flow_id, status="SUCCEEDED", trigger_event_details={}
    )
    claims = {
        "execution_id": execution_id,
        "flow_id": flow_id,
        "account_id": account_id,
        "operation": "get",
        "thread_id": str(execution_id),
    }
    with (
        patch(
            "preloop.api.endpoints.flow_artifacts.crud_flow_execution.get_without_workspace",
            return_value=execution,
        ),
        patch(
            "preloop.api.endpoints.flow_artifacts.crud_flow.get",
            return_value=SimpleNamespace(id=flow_id, account_id=account_id),
        ),
    ):
        with pytest.raises(HTTPException) as error:
            authorize(MagicMock(), claims, execution_id, "get")
    assert error.value.status_code == 409


@pytest.mark.parametrize("token", [TOKEN, 'synthetic-quoted-"credential\\value'])
def test_codex_uses_private_config_for_model_and_mcp_credentials(
    tmp_path: Path, token: str
) -> None:
    """Codex can authenticate without inheriting model/MCP secrets in tools."""
    import subprocess
    import json
    import os
    import tomllib
    from preloop.agents.codex import CodexAgent

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    block = CodexAgent({})._build_codex_auth_config(
        model="example-model",
        model_provider="example",
        model_endpoint="https://example.com/v1",
    )
    result = subprocess.run(
        ["bash", "-c", block + '\nprintf "KEY=%s" "${OPENAI_API_KEY-unset}"'],
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "OPENAI_API_KEY": token,
            "EXAMPLE_API_KEY": token,
            "PRELOOP_API_TOKEN": token,
            "PRELOOP_MCP_URL": "https://example.com/mcp/v1",
            "MCP_TOOL_TIMEOUT_SEC": "600",
            "PRELOOP_DISABLE_TELEMETRY": "true",
        },
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.endswith("KEY=unset")
    config = (home / ".codex/config.toml").read_text()
    assert 'env_key = "EXAMPLE_API_KEY"' not in config
    assert "bearer_token_env_var" not in config
    assert "Authorization" in config
    assert (home / ".codex/config.toml").stat().st_mode & 0o077 == 0
    parsed = tomllib.loads(config)
    assert (
        parsed["model_providers"]["example"]["http_headers"]["Authorization"]
        == "Bearer " + token
    )
    assert (
        parsed["mcp_servers"]["preloop"]["http_headers"]["Authorization"]
        == "Bearer " + token
    )
    assert (
        json.loads((home / ".codex/auth.json").read_text())["OPENAI_API_KEY"] == token
    )


def test_live_event_masks_before_delivery() -> None:
    """Live delivery uses the same execution scope as persisted output."""
    from preloop.services.execution_realtime import stamp_execution_event

    event = {
        "execution_id": str(uuid4()),
        "type": "agent_log_line",
        "payload": {"line": TOKEN},
    }
    with (
        patch(
            "preloop.services.execution_realtime.run_db_sync",
            side_effect=lambda operation: operation(MagicMock()),
        ),
        patch(
            "preloop.services.execution_realtime.crud_realtime.allocate_sequence",
            return_value=(1, 0),
        ),
        patch(
            "preloop.models.crud.execution_credentials.for_execution",
            return_value=CredentialMasker.from_values([TOKEN]),
        ),
    ):
        safe = stamp_execution_event(event)
    assert safe["payload"]["line"] == "[REDACTED]"


def test_gateway_masks_error_details() -> None:
    """Rendered errors and exception strings use the execution's masker."""
    from preloop.services.openai_gateway import gateway_database_scope
    from preloop.services.model_gateway_errors import ModelGatewayAPIError

    service = SimpleNamespace(
        auth_context=SimpleNamespace(
            api_key=SimpleNamespace(context_data={"flow_execution_id": str(uuid4())})
        ),
        db=MagicMock(),
        release_db_for_wait=MagicMock(),
        _close_owned_db=MagicMock(),
    )

    @gateway_database_scope
    def upstream(self: object, payload: dict) -> dict:
        raise ModelGatewayAPIError(
            provider="openai",
            status_code=400,
            message=TOKEN,
            provider_detail=TOKEN,
            cause={"message": TOKEN},
        )

    with patch(
        "preloop.models.crud.execution_credentials.for_execution",
        return_value=CredentialMasker.from_values([TOKEN]),
    ):
        with pytest.raises(ModelGatewayAPIError) as error:
            upstream(service, {})
    assert str(error.value) == "[REDACTED]"
    assert error.value.message == "[REDACTED]"
    assert error.value.provider_detail == "[REDACTED]"
    assert error.value.cause == {"message": "[REDACTED]"}


def test_gateway_does_not_forward_when_inventory_read_fails() -> None:
    """An unavailable masking registry cannot turn into an unmasked request."""
    from preloop.services.openai_gateway import gateway_database_scope

    service = SimpleNamespace(
        auth_context=SimpleNamespace(
            api_key=SimpleNamespace(context_data={"flow_execution_id": str(uuid4())})
        ),
        db=MagicMock(),
        release_db_for_wait=MagicMock(),
        _close_owned_db=MagicMock(),
    )
    seen = []

    @gateway_database_scope
    def upstream(self: object, payload: dict) -> dict:
        seen.append(payload)
        return payload

    with patch(
        "preloop.models.crud.execution_credentials.for_execution",
        side_effect=RuntimeError("unavailable"),
    ):
        with pytest.raises(RuntimeError, match="unavailable"):
            upstream(service, {"output": TOKEN})
    assert not seen
    service._close_owned_db.assert_called_once()


@pytest.mark.parametrize(
    "channel", ["allow_stale_runtime_session", "allow_ended_runtime_session"]
)
def test_runtime_channels_bound_terminal_execution_grace(channel: str) -> None:
    """Durable-agent exceptions do not extend execution credentials indefinitely."""
    from datetime import UTC, datetime, timedelta
    from preloop.api.auth import jwt

    key = SimpleNamespace(
        context_data={"flow_execution_id": str(uuid4())},
        account_id=uuid4(),
        user_id=uuid4(),
        scopes=["mcp:read"],
        is_active=True,
        is_expired=False,
    )
    user = SimpleNamespace(account_id=key.account_id, is_active=True)
    with (
        patch.object(jwt, "_runtime_session_id_from_api_key", return_value=None),
        patch.object(jwt, "_managed_agent_for_api_key", return_value=None),
        patch.object(jwt.crud_user, "get", return_value=user),
        patch.object(jwt.crud_flow_execution, "get_status", return_value="FAILED"),
        patch.object(
            jwt.crud_flow_execution,
            "get_end_time",
            return_value=datetime.now(UTC) - timedelta(minutes=10),
        ),
    ):
        with pytest.raises(HTTPException) as error:
            jwt._authenticate_with_api_key(MagicMock(), key, **{channel: True})
    assert error.value.status_code == 401


def test_browser_flush_keeps_short_terminal_grace() -> None:
    """Final output delivery still works inside its five-minute window."""
    from datetime import UTC, datetime, timedelta
    from preloop.api.auth import jwt

    key = SimpleNamespace(
        context_data={"flow_execution_id": str(uuid4())}, account_id=uuid4()
    )
    with (
        patch.object(jwt.crud_flow_execution, "get_status", return_value="SUCCEEDED"),
        patch.object(
            jwt.crud_flow_execution,
            "get_end_time",
            return_value=datetime.now(UTC) - timedelta(minutes=2),
        ),
    ):
        jwt._reject_finished_flow_execution_key(MagicMock(), key, allow_flush=True)


def test_session_summary_masks_before_truncation() -> None:
    """Summary limits cannot retain the beginning of a credential."""
    from preloop.models.crud.runtime_session_activity import (
        crud_runtime_session_activity,
    )

    prefix = "x" * 1990
    with (
        patch(
            "preloop.models.crud.execution_credentials.for_session",
            return_value=CredentialMasker.from_values([TOKEN]),
        ),
        patch.object(crud_runtime_session_activity, "_touch_runtime_session_and_agent"),
    ):
        row = crud_runtime_session_activity.log_agent_control_message(
            MagicMock(),
            account_id=uuid4(),
            runtime_session_id=uuid4(),
            message=prefix + TOKEN,
            status="sent",
            commit=False,
        )
    assert row.summary == prefix + "[REDACTED]"


def test_session_artifact_masks_payload_and_metadata() -> None:
    """Encrypted output and descriptive metadata use the same execution scope."""
    from preloop.models.crud import runtime_session_artifact

    with (
        patch(
            "preloop.models.crud.execution_credentials.for_session",
            return_value=CredentialMasker.from_values([TOKEN]),
        ),
        patch(
            "preloop.services.session_artifact_budget.enforce_account_budget",
            return_value=[],
        ),
        patch.object(
            runtime_session_artifact, "_existing_source_row", return_value=None
        ),
    ):
        row = runtime_session_artifact.store(
            MagicMock(),
            account_id=uuid4(),
            runtime_session_id=uuid4(),
            kind="screenshot",
            source="runner",
            source_ref=TOKEN,
            content_type="image/png",
            plaintext=b"\x89PNG\r\n\x1a\n" + TOKEN.encode(),
            manifest={"output": TOKEN},
            tool_name=TOKEN,
            commit=False,
        )
    assert runtime_session_artifact.decrypt(row) == b"\x89PNG\r\n\x1a\n[REDACTED]"
    assert row.manifest == {"output": "[REDACTED]"}
    assert row.tool_name == "[REDACTED]"
    assert row.source_ref == "[REDACTED]"


def test_existing_control_connection_rechecks_execution_lifetime() -> None:
    """An already-connected socket cannot extend a finished execution key."""
    from preloop.models.crud import agent_control_connection, crud_flow_execution

    account, agent_id, key_id, user_id, connection_id = (str(uuid4()) for _ in range(5))
    connection = SimpleNamespace(
        account_id=account,
        managed_agent_id=agent_id,
        api_key_id=key_id,
        user_id=user_id,
        connection_id=connection_id,
    )
    agent = SimpleNamespace(
        lifecycle_state="active", control_connection_id=connection_id
    )
    key = SimpleNamespace(
        is_active=True,
        is_expired=False,
        user_id=user_id,
        context_data={"managed_agent_id": agent_id, "flow_execution_id": str(uuid4())},
    )
    user = SimpleNamespace(is_active=True)
    db = MagicMock()
    query = db.query.return_value
    query.filter.return_value = query
    query.with_for_update.return_value = query
    query.populate_existing.return_value = query
    query.first.side_effect = [object(), agent, key, user]
    with patch.object(crud_flow_execution, "get_status", return_value="FAILED"):
        assert agent_control_connection.authorize(db, connection) is None
