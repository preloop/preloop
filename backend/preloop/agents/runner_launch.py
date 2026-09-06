"""Transient private Docker launch specs built by the hosted agent adapters.

Launch contents are never persisted. Scripts and environment are rebuilt at
lease delivery so model, MCP and git credentials never enter pending_job JSONB.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models.crud import crud_flow_execution
from preloop.services.mcp_config_service import MCPConfigService

LAUNCH_VERSION = 1
MAX_RESULT_BYTES = 256 * 1024
logger = logging.getLogger(__name__)


class RunnerLaunchConfigurationChangedError(ValueError):
    """Leased configuration no longer matches the flow definition."""


def flow_launch_fingerprint(flow: Any) -> str | None:
    """Detect configuration edits between leasing and redelivery without secrets."""
    if flow is None:
        return None
    fields = (
        "agent_type",
        "agent_config",
        "ai_model_id",
        "git_clone_config",
        "custom_commands",
        "allowed_mcp_servers",
        "allowed_mcp_tools",
    )
    snapshot = {key: getattr(flow, key, None) for key in fields}
    model = getattr(flow, "ai_model", None)
    if model is not None:
        snapshot["model"] = {
            key: getattr(model, key, None)
            for key in (
                "id",
                "model_identifier",
                "provider_name",
                "api_endpoint",
                "meta_data",
            )
        }
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, default=str).encode()
    ).hexdigest()


async def prepare_runner_delivery(
    db: Session, job: dict[str, Any], context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Always deliver either a runnable launch or an explicit failing lease.

    Adapter/credential errors must not strand the already committed lease. Do
    not serialize exception text: upstream client errors can contain secrets.
    """
    try:
        if context is None:
            return await hydrate_runner_job(db, job)
        return {**job, "launch": await build_runner_launch(context)}
    except RunnerLaunchConfigurationChangedError:
        return {
            **job,
            "launch_error": "Flow configuration changed after leasing; retry the execution",
        }
    except Exception:
        logger.warning(
            "Could not prepare private runner launch for %s", job.get("execution_id")
        )
        return {
            **job,
            "launch_error": "Could not prepare private runner launch; verify the flow configuration and supported harness",
        }


async def build_runner_launch(context: dict[str, Any]) -> dict[str, Any]:
    """Build the same bootstrap and credentials as a hosted Codex/OpenCode run."""
    from .codex import CodexAgent
    from .opencode import OpenCodeAgent

    context = dict(context)
    agent_type = context.get("agent_type")
    config = context.get("agent_config") or {}
    model = (
        (
            context.get("model_gateway_model_alias")
            if context.get("model_gateway_enabled")
            else None
        )
        or context.get("model_identifier")
        or config.get("model")
    )
    if agent_type == "codex":
        agent = CodexAgent(config)
        context["codex_model"] = model or "gpt-5.4"
        build_script = agent._build_codex_script
    elif agent_type == "opencode":
        agent = OpenCodeAgent(config)
        context["opencode_model"] = model
        build_script = agent._build_opencode_script
    else:
        raise ValueError("Private Docker launch supports only codex and opencode")

    context["prompt"] = (context.get("prompt") or "") + (
        "\nBefore exiting, write /workspace/result.json with a recognized completion "
        'status (for example {"status":"success"}) or the verdict required by the '
        "task. Preserve all richer report fields. An exit code alone is insufficient."
    )
    if context.get("model_gateway_enabled"):
        # Private containers cannot resolve the hosted service DNS name. The
        # runner supplies its authenticated control-plane origin at execution.
        context["model_gateway_url"] = "${PRELOOP_URL}/openai/v1"
    env = await agent._prepare_environment(context)
    env["PRELOOP_API_TOKEN"] = context.get("account_api_token") or ""
    env["MCP_TOOL_TIMEOUT_SEC"] = str(context.get("_mcp_tool_timeout", 600))
    servers = context.get("allowed_mcp_servers") or []
    tools = context.get("allowed_mcp_tools") or []
    env.update(MCPConfigService.generate_mcp_environment_vars(servers, tools))
    mcp_config = MCPConfigService.generate_mcp_config(servers, tools)
    # Do not pass credentials to the config generator: it logs its output.
    server = mcp_config["mcpServers"].get("preloop-mcp")
    if server is not None:
        server["headers"] = {"Authorization": "Bearer " + env["PRELOOP_API_TOKEN"]}
    env["MCP_CONFIG_JSON"] = json.dumps(mcp_config)
    # The script builder resolves repository credentials into transient env refs.
    script = build_script(context)
    agent._apply_git_credential_env(env, context)
    return {"version": LAUNCH_VERSION, "script": script, "env": env}


async def hydrate_runner_job(db: Session, job: dict[str, Any]) -> dict[str, Any]:
    """Regenerate a persisted lease, including fresh credentials and trigger data."""
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    if job.get("launch_version") != LAUNCH_VERSION:
        return job
    execution = crud_flow_execution.get(db, id=UUID(job["execution_id"]))
    if execution is None:
        raise ValueError("Runner execution no longer exists")
    orchestrator = FlowExecutionOrchestrator(
        db, execution.flow_id, execution.trigger_event_details or {}, None
    )
    orchestrator.execution_log = execution
    orchestrator._get_flow_details()
    expected = job.get("flow_launch_fingerprint")
    if expected and expected != flow_launch_fingerprint(orchestrator.flow):
        raise RunnerLaunchConfigurationChangedError()
    context = await orchestrator._prepare_execution_context(
        resolved_prompt=job.get("prompt") or execution.resolved_input_prompt or ""
    )
    # Keep the leased harness/configuration stable across reconnects.
    for key in (
        "agent_type",
        "agent_config",
        "git_clone_config",
        "custom_commands",
        "allowed_mcp_servers",
        "allowed_mcp_tools",
    ):
        if key in job:
            context[key] = job[key]
    hydrated = dict(job)
    hydrated["account_api_token"] = context.get("account_api_token")
    hydrated["launch"] = await build_runner_launch(context)
    return hydrated


def validate_runner_completion(
    message: dict[str, Any],
) -> tuple[str, str | None, dict[str, Any] | None]:
    """Exit zero and an explicit structured verdict are both required for success."""
    from preloop.services.flow_orchestrator import _result_artifact_confirmation

    status = str(message.get("status") or "FAILED").upper()
    error = str(message["error"]) if message.get("error") else None
    result = message.get("result")
    if (
        not isinstance(result, dict)
        or len(json.dumps(result).encode()) > MAX_RESULT_BYTES
    ):
        result = None
    if status not in {"SUCCEEDED", "FAILED", "STOPPED"}:
        return "FAILED", "Invalid runner completion status", result
    if status == "SUCCEEDED" and (
        message.get("launch_version") != LAUNCH_VERSION
        or type(message.get("exit_code")) is not int
        or message["exit_code"] != 0
        or _result_artifact_confirmation(result) != "success"
    ):
        return (
            "FAILED",
            "Runner exited without a valid structured completion result",
            result,
        )
    return status, error, result
