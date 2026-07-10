import uuid
import secrets
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from preloop.models import schemas
from preloop.models.crud import crud_account, crud_runtime_session_activity
from preloop.models.crud.flow import CRUDFlow
from preloop.models.crud.flow_execution import CRUDFlowExecution
from preloop.models.db.session import get_db_session as get_db
from preloop.api.auth import get_current_active_user
from preloop.models.models.user import User
from preloop.schemas.gateway_usage import FlowGatewayUsageSummaryResponse
from preloop.services.model_gateway_usage import ModelGatewayUsageService
from preloop.utils.hashing import compute_content_hash
from preloop.utils.audit import log_config_change
from preloop.utils.permissions import require_permission
from preloop.models.crud.flow_execution_log import crud_flow_execution_log


router = APIRouter()


crud_flow = CRUDFlow()
crud_flow_execution = CRUDFlowExecution()


@router.post("/flows", response_model=schemas.FlowResponse)
@require_permission("create_flows")
def create_flow(
    *,
    db: Session = Depends(get_db),
    flow_in: schemas.FlowCreate,
    current_user: User = Depends(get_current_active_user),
):
    """Create new flow."""
    # Check for name uniqueness within account
    # Note: We intentionally allow flows to have the same name as global presets,
    # as users should be able to customize presets and keep the original name
    existing_in_account = crud_flow.get_by_name_and_account(
        db, name=flow_in.name, account_id=current_user.account_id
    )
    if existing_in_account:
        raise HTTPException(
            status_code=400,
            detail=f"A flow with name '{flow_in.name}' already exists in your account",
        )

    # Security check: Only superusers can configure custom commands
    if flow_in.custom_commands and flow_in.custom_commands.enabled:
        if not current_user.is_superuser:
            raise HTTPException(
                status_code=403,
                detail="Only administrators can configure custom commands for security reasons",
            )

    # If this is a webhook trigger, auto-generate a secure webhook secret
    if flow_in.trigger_event_source == "webhook" or (
        not flow_in.trigger_event_source and not flow_in.trigger_event_types
    ):
        # Generate a secure 32-byte URL-safe token
        webhook_secret = secrets.token_urlsafe(32)
        flow_in.webhook_config = schemas.WebhookConfig(webhook_secret=webhook_secret)
        flow_in.trigger_event_source = "webhook"
        flow_in.trigger_event_types = ["webhook"]

    # If creating from a preset, validate and compute source hashes for template tracking
    if flow_in.source_preset_id:
        preset = crud_flow.get(db=db, id=flow_in.source_preset_id)

        # Security: Validate the source is a valid, accessible preset
        if not preset:
            raise HTTPException(
                status_code=400,
                detail=f"Source preset {flow_in.source_preset_id} not found",
            )

        if not preset.is_preset:
            raise HTTPException(
                status_code=400,
                detail=f"Source flow {flow_in.source_preset_id} is not a preset. "
                "Only preset flows can be used as a source.",
            )

        # Security: Preset must be global (account_id is None) or belong to the user's account
        if (
            preset.account_id is not None
            and preset.account_id != current_user.account_id
        ):
            raise HTTPException(
                status_code=403,
                detail="Cannot create flow from a preset belonging to another account",
            )

        # Compute hashes of the preset's current prompt and tools
        flow_in.source_prompt_hash = compute_content_hash(preset.prompt_template)
        flow_in.source_tools_hash = compute_content_hash(preset.allowed_mcp_tools or [])
        flow_in.prompt_customized = False
        flow_in.tools_customized = False
        flow_in.preset_update_available = False

    flow = crud_flow.create(db=db, flow_in=flow_in, account_id=current_user.account_id)

    log_config_change(
        db,
        user=current_user,
        config_type="flow",
        action="created",
        new_value={"id": str(flow.id), "name": flow.name},
    )

    return flow


@router.get("/flows", response_model=List[schemas.FlowResponse])
@require_permission("view_flows")
def read_flows(
    db: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_active_user),
):
    """Retrieve flows for the account."""
    flows = crud_flow.get_multi(
        db, account_id=current_user.account_id, skip=skip, limit=limit
    )

    if flows:
        flow_ids = [f.id for f in flows]
        stats = crud_flow_execution.get_execution_stats_for_flows(db, flow_ids)

        stats_map = {
            s.flow_id: {
                "total_execs": s.total_execs,
                "running_execs": int(s.running_execs or 0),
                "last_seen_at": s.last_seen_at.isoformat() if s.last_seen_at else None,
                "estimated_cost": float(s.estimated_cost or 0.0),
            }
            for s in stats
        }

        for flow in flows:
            flow.execution_stats = stats_map.get(
                flow.id,
                {
                    "total_execs": 0,
                    "running_execs": 0,
                    "last_seen_at": None,
                    "estimated_cost": 0.0,
                },
            )

    return flows


@router.get("/flows/presets", response_model=List[schemas.FlowResponse])
@require_permission("view_flows")
def read_presets(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Retrieve flow presets available to the account.

    Returns global presets (account_id=None) plus any account-specific presets.
    """
    return crud_flow.get_presets_for_account(db, account_id=current_user.account_id)


@router.post("/flows/presets/{flow_id}/clone", response_model=schemas.FlowResponse)
@require_permission("create_flows")
def clone_preset(
    *,
    db: Session = Depends(get_db),
    flow_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
):
    """Clone a flow preset.

    Creates a copy of the preset for the user's account with template tracking
    enabled. The cloned flow will auto-update when the preset changes, unless
    the user customizes the prompt or tools.
    """
    preset = crud_flow.get(db=db, id=flow_id)
    if not preset or not preset.is_preset:
        raise HTTPException(status_code=404, detail="Preset not found")

    # Build dict excluding fields we want to override or that aren't in FlowCreate
    # Note: is_enabled is excluded so cloned flows start enabled (presets are disabled)
    # Also exclude template tracking fields - we set these explicitly
    preset_dict = {
        k: v
        for k, v in preset.__dict__.items()
        if k
        not in [
            "_sa_instance_state",
            "id",
            "created_at",
            "updated_at",
            "name",
            "is_preset",
            "is_enabled",
            "account_id",
            # Template tracking fields - set explicitly below
            "source_preset_id",
            "source_prompt_hash",
            "source_tools_hash",
            "prompt_customized",
            "tools_customized",
            "preset_update_available",
        ]
    }

    # Generate a unique name for the cloned flow
    base_name = f"Copy of {preset.name}"
    final_name = base_name
    suffix = 1

    while crud_flow.get_by_name_and_account(
        db, name=final_name, account_id=current_user.account_id
    ):
        suffix += 1
        final_name = f"{base_name} ({suffix})"

    # Compute hashes of the preset's current prompt and tools
    # These are used to detect if the user customizes the flow later
    source_prompt_hash = compute_content_hash(preset.prompt_template)
    source_tools_hash = compute_content_hash(preset.allowed_mcp_tools or [])

    cloned_flow_in = schemas.FlowCreate(
        **preset_dict,
        name=final_name,
        is_preset=False,
        is_enabled=True,  # Cloned flows start enabled
        account_id=str(current_user.account_id),
        # Template tracking: link to source preset
        source_preset_id=str(preset.id),
        source_prompt_hash=source_prompt_hash,
        source_tools_hash=source_tools_hash,
        prompt_customized=False,
        tools_customized=False,
        preset_update_available=False,
    )
    cloned_flow = crud_flow.create(
        db=db, flow_in=cloned_flow_in, account_id=current_user.account_id
    )
    return cloned_flow


def _normalize_status_filters(status: Optional[List[str]]) -> Optional[List[str]]:
    """Normalize repeated or comma-separated status query parameters."""
    if not status:
        return None
    if isinstance(status, str):
        status = [status]
    if not isinstance(status, list):
        return None

    values: List[str] = []
    for item in status:
        values.extend(part.strip().upper() for part in item.split(",") if part.strip())
    return values or None


@router.get("/flows/executions", response_model=List[schemas.FlowExecutionListResponse])
@require_permission("view_flows")
def read_flow_executions(
    db: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 25,
    flow_id: Optional[uuid.UUID] = None,
    status: Optional[List[str]] = Query(default=None),
    current_user: User = Depends(get_current_active_user),
):
    """Retrieve lightweight flow execution summaries for the account."""
    statuses = _normalize_status_filters(status)
    limit = max(1, min(limit, 100))
    # Use eager_load=True to load flow relationship in single query (avoids N+1)
    executions = crud_flow_execution.get_multi(
        db,
        account_id=current_user.account_id,
        skip=skip,
        limit=limit,
        flow_id=flow_id,
        statuses=statuses,
        eager_load=True,
        lightweight=True,
    )

    # Flow is already loaded via joinedload - no additional queries needed
    for execution in executions:
        execution.flow_name = execution.flow.name if execution.flow else None

    return executions


@router.get(
    "/flows/executions/{execution_id}", response_model=schemas.FlowExecutionResponse
)
@require_permission("view_flows")
def read_flow_execution(
    *,
    db: Session = Depends(get_db),
    execution_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
):
    """Get flow execution by ID."""
    execution = crud_flow_execution.get(
        db=db, id=execution_id, account_id=current_user.account_id
    )
    if not execution:
        raise HTTPException(status_code=404, detail="Flow execution not found")

    if not execution.mcp_usage_logs:
        rows = crud_runtime_session_activity.list_tool_calls_for_flow_execution(
            db,
            account_id=current_user.account_id,
            flow_execution_id=execution.id,
        )
        if rows:
            execution.mcp_usage_logs = [
                {
                    "timestamp": row.timestamp.isoformat() if row.timestamp else None,
                    "tool_name": row.tool_name,
                    "server_name": row.server_name,
                    "status": row.status,
                    "summary": row.summary,
                    **(row.metadata_ or {}),
                }
                for row in rows
            ]

    return execution


@router.get("/flows/executions/{execution_id}/logs")
@require_permission("view_flows")
async def get_flow_execution_logs(
    *,
    db: Session = Depends(get_db),
    execution_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
    tail: int | None = None,
    skip: int = 0,
    limit: int | None = None,
) -> Dict[str, Any]:
    """Get execution logs from the container (if running) or database (if finished).

    For running executions, fetches logs directly from the Docker/Kubernetes container.
    For finished executions, returns persisted logs from the database.

    Args:
        execution_id: ID of the execution
        tail: Number of recent log lines to retrieve, or None for all logs
        skip: Number of logs to skip (for pagination)
        limit: Number of logs to return (for pagination)

    Returns:
        Dictionary with:
        - logs: List of log lines
        - source: Where logs were fetched from ("container" or "database")
        - has_more: Boolean indicating if there are more logs (if paginated)
    """
    from preloop.agents import create_agent_executor

    # Verify execution exists and user has access
    execution = crud_flow_execution.get(
        db=db, id=execution_id, account_id=current_user.account_id
    )
    if not execution:
        raise HTTPException(status_code=404, detail="Flow execution not found")

    # Check if execution is running
    is_running = execution.status in ["RUNNING", "STARTING", "INITIALIZING", "PENDING"]

    if is_running and execution.agent_session_reference:
        # Fetch logs directly from container
        agent = None
        try:
            # Get the flow to determine agent type
            flow = crud_flow.get(
                db=db, id=execution.flow_id, account_id=current_user.account_id
            )
            if not flow:
                raise HTTPException(status_code=404, detail="Flow not found")

            # Create the concrete agent executor so log retrieval uses the same
            # Docker/Kubernetes detection as execution startup and recovery.
            agent = create_agent_executor(
                flow.agent_type, {"agent_config": flow.agent_config or {}}
            )

            # Fetch logs from container
            container_logs = await agent.get_logs(
                execution.agent_session_reference, tail=tail
            )

            # Format logs as execution updates (matching the WebSocket format)
            formatted_logs = []
            for log_line in container_logs:
                # Use start_time if available, otherwise use current time
                timestamp = (
                    execution.start_time.isoformat()
                    if execution.start_time
                    else execution.created_at.isoformat()
                )
                formatted_logs.append(
                    {
                        "execution_id": str(execution_id),
                        "timestamp": timestamp,
                        "type": "agent_log_line",
                        "payload": {"line": log_line},
                    }
                )

            return {"logs": formatted_logs, "source": "container", "has_more": False}

        except Exception as e:
            import logging

            logger = logging.getLogger(__name__)
            logger.error(
                f"Failed to fetch container logs for execution {execution_id}: {e}"
            )
            # Fall back to database logs if container logs fail
        finally:
            # Always cleanup agent resources to avoid leaking connections
            if agent is not None:
                await agent.cleanup()

    # For finished executions or if container logs failed, return database logs.
    # Prefer the normalized flow_execution_log table; fall back to legacy JSONB column.

    # If using pagination, default to ascending order
    # If neither tail nor limit is provided, use limit=5000 to prevent massive payloads
    actual_limit = limit if limit is not None else (tail if tail is not None else 5000)

    # Fetch one extra row to determine if there are more
    query_limit = actual_limit + 1 if actual_limit else None

    log_rows = crud_flow_execution_log.get_by_execution_id(
        db,
        execution_id,
        tail=query_limit
        if getattr(execution, "status", "") != "RUNNING" and tail is not None
        else (tail if getattr(execution, "status", "") == "RUNNING" else None),
        desc=tail is not None,  # If tail is used, query descending
        skip=skip,
        limit=query_limit if tail is None else None,
    )

    has_more = False
    if query_limit is not None and len(log_rows) > actual_limit:
        has_more = True
        if tail is not None:
            # If tail is used, the extra row is the oldest (at the start of the reversed list)
            log_rows = log_rows[1:]
        else:
            # If limit is used, the extra row is the newest (at the end of the list)
            log_rows = log_rows[:actual_limit]

    if log_rows:
        # DB returns logs chronologically (oldest first).
        # Crud layer already handles reversing if tail+desc was used.
        logs = [
            {
                "execution_id": str(row.execution_id),
                "timestamp": row.timestamp.isoformat() if row.timestamp else None,
                "type": row.log_type or "log",
                "payload": {**(row.metadata_ or {}), "message": row.message},
            }
            for row in log_rows
        ]
        return {"logs": logs, "source": "database", "has_more": has_more}
    elif execution.execution_logs and isinstance(execution.execution_logs, list):
        # Legacy fallback for executions logged before the migration
        return {
            "logs": execution.execution_logs,
            "source": "database",
            "has_more": False,
        }
    else:
        return {"logs": [], "source": "database", "has_more": False}


@router.get("/flows/executions/{execution_id}/gateway-events")
@require_permission("view_flows")
def get_flow_execution_gateway_events(
    *,
    db: Session = Depends(get_db),
    execution_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
    tail: int | None = None,
    metadata_only: bool = False,
) -> Dict[str, Any]:
    """Get normalized model gateway events for a flow execution."""
    execution = crud_flow_execution.get(
        db=db, id=execution_id, account_id=current_user.account_id
    )
    if not execution:
        raise HTTPException(status_code=404, detail="Flow execution not found")

    actual_tail = tail if tail is not None else 5000
    rows = crud_flow_execution_log.get_by_execution_id(
        db, execution_id, tail=actual_tail, desc=True
    )

    events = []
    # Reverse rows so chronological order is maintained (oldest to newest)
    for row in reversed(rows):
        payload = row.metadata_ or {}
        if metadata_only:
            # Strip large payload objects for metadata-only response
            # Need to avoid mutating the original dict if it's tied to SQLAlchemy
            payload = payload.copy()
            payload.pop("conversation_preview", None)
            payload.pop("tools", None)
            payload.pop("result", None)
            payload.pop("stream_events", None)
        else:
            payload = {**payload, "message": row.message}

        events.append(
            {
                "id": str(row.id),
                "execution_id": str(row.execution_id),
                "timestamp": row.timestamp.isoformat() if row.timestamp else None,
                "type": row.log_type or "log",
                "payload": payload,
            }
        )
    return {"logs": events, "source": "database"}


@router.get("/flows/executions/{execution_id}/gateway-events/{event_id}")
@require_permission("view_flows")
def get_flow_execution_gateway_event(
    *,
    db: Session = Depends(get_db),
    execution_id: uuid.UUID,
    event_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """Get a single normalized model gateway event."""
    execution = crud_flow_execution.get(
        db=db, id=execution_id, account_id=current_user.account_id
    )
    if not execution:
        raise HTTPException(status_code=404, detail="Flow execution not found")

    row = crud_flow_execution_log.get_event_by_id(db, execution_id, event_id)

    if not row:
        raise HTTPException(status_code=404, detail="Gateway event not found")

    return {
        "id": str(row.id),
        "execution_id": str(row.execution_id),
        "timestamp": row.timestamp.isoformat() if row.timestamp else None,
        "type": row.log_type or "log",
        "payload": {**(row.metadata_ or {}), "message": row.message},
    }


@router.get("/flows/executions/{execution_id}/metrics")
@require_permission("view_flows")
def get_flow_execution_metrics(
    *,
    db: Session = Depends(get_db),
    execution_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """Get execution metrics including tool calls, API usage, and costs.

    Returns:
        Dictionary with:
        - tool_calls: Number of MCP tool calls made
        - api_requests: Number of API requests made during execution
        - token_usage: Token usage statistics
        - estimated_cost: Estimated cost based on token usage (0.0 if no pricing)
        - has_pricing: Whether pricing is configured in AI model metadata
    """
    from preloop.services.execution_metrics import ExecutionMetricsService

    # Verify execution exists and user has access
    execution = crud_flow_execution.get(
        db=db, id=execution_id, account_id=current_user.account_id
    )
    if not execution:
        raise HTTPException(status_code=404, detail="Flow execution not found")

    # Calculate metrics
    metrics_service = ExecutionMetricsService(db)
    try:
        metrics = metrics_service.get_execution_metrics(str(execution_id))
        return metrics
    except Exception as e:
        # Log error but return zero metrics instead of failing
        import logging

        logger = logging.getLogger(__name__)
        logger.error(f"Failed to calculate metrics for execution {execution_id}: {e}")
        return {
            "tool_calls": 0,
            "api_requests": 0,
            "token_usage": {"total_tokens": 0, "input_tokens": 0, "output_tokens": 0},
            "estimated_cost": 0.0,
            "has_pricing": False,
        }


@router.get(
    "/flows/{flow_id}/gateway-usage/summary",
    response_model=FlowGatewayUsageSummaryResponse,
)
@require_permission("view_flows")
def get_flow_gateway_usage_summary(
    *,
    db: Session = Depends(get_db),
    flow_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
):
    """Get model gateway usage summary for a flow."""
    flow = crud_flow.get(db=db, id=flow_id, account_id=current_user.account_id)
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")

    account = crud_account.get(db, id=current_user.account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    return ModelGatewayUsageService(db).get_flow_summary(
        account=account,
        flow=flow,
        start_date=start_date,
        end_date=end_date,
    )


@router.post("/flows/executions/{execution_id}/command")
@require_permission("execute_flows")
async def send_execution_command(
    *,
    db: Session = Depends(get_db),
    execution_id: uuid.UUID,
    command_data: schemas.FlowExecutionCommand,
    current_user: User = Depends(get_current_active_user),
):
    """Send a command to a running flow execution."""
    import logging
    from datetime import datetime, timezone
    from preloop.agents.container import ContainerAgentExecutor
    from preloop.agents.codex import CodexAgent
    from preloop.sync.services.event_bus import get_nats_client
    import os

    logger = logging.getLogger(__name__)

    # Get NATS client for sending commands
    try:
        nats_client = await get_nats_client()
    except Exception as e:
        logger.error(f"Failed to get NATS client: {e}")
        nats_client = None

    # Verify execution exists and user has access
    execution = crud_flow_execution.get(
        db=db, id=execution_id, account_id=current_user.account_id
    )
    if not execution:
        raise HTTPException(status_code=404, detail="Flow execution not found")

    # Handle stop command - stop container directly
    if command_data.command == "stop":
        # Stop the container if it's running
        if execution.agent_session_reference and execution.status in [
            "RUNNING",
            "STARTING",
            "INITIALIZING",
            "PENDING",
        ]:
            try:
                # Get the flow to determine agent type
                flow = crud_flow.get(
                    db=db, id=execution.flow_id, account_id=current_user.account_id
                )
                if flow:
                    use_kubernetes = (
                        os.getenv("USE_KUBERNETES_FOR_AGENTS", "false").lower()
                        == "true"
                    )

                    # Create agent executor to fetch logs and stop the container
                    # CodexAgent auto-detects Kubernetes environment, no need to pass use_kubernetes
                    if flow.agent_type == "codex":
                        agent = CodexAgent(config={})
                    else:
                        agent = ContainerAgentExecutor(
                            agent_type=flow.agent_type,
                            config={},
                            image="dummy-image",
                            use_kubernetes=use_kubernetes,
                        )

                    # Fetch final logs before stopping the container
                    try:
                        container_logs = await agent.get_logs(
                            execution.agent_session_reference, tail=5000
                        )

                        # Persist final logs to normalized flow_execution_log table
                        if container_logs:
                            for log_line in container_logs:
                                crud_flow_execution.append_log(
                                    db,
                                    execution_id=str(execution_id),
                                    log_data={
                                        "type": "agent_log_line",
                                        "payload": {"line": log_line},
                                    },
                                    commit=False,
                                )
                            db.commit()
                            logger.info(
                                f"Persisted {len(container_logs)} log lines to database for execution {execution_id}"
                            )
                    except Exception as log_error:
                        logger.error(
                            f"Failed to fetch and persist logs before stopping: {log_error}"
                        )
                        # Continue with stop even if log fetching fails

                    # Stop the container
                    await agent.stop(execution.agent_session_reference)
                    logger.info(
                        f"Stopped container {execution.agent_session_reference} for execution {execution_id}"
                    )
            except Exception as e:
                logger.error(
                    f"Failed to stop container for execution {execution_id}: {e}"
                )
                # Continue with status update even if container stop fails

        # Update execution status
        update_data = schemas.FlowExecutionUpdate(
            status="STOPPED",
            error_message="Manually stopped by user",
            end_time=datetime.now(timezone.utc),
        )
        crud_flow_execution.update(db=db, db_obj=execution, obj_in=update_data)
        db.commit()

        # Try to send stop command via NATS (best effort - don't fail if this doesn't work)
        try:
            from preloop.services.flow_orchestrator import (
                FlowExecutionOrchestrator,
            )

            await FlowExecutionOrchestrator.send_command(
                execution_id=str(execution_id),
                command=command_data.command,
                payload=command_data.payload,
                nats_client=nats_client,
            )
        except Exception as e:
            logger.warning(f"Failed to send stop command via NATS: {e}")
            # Not a critical error - container is already stopped

        return {"status": "stopped"}

    # For other commands, try to send via NATS
    try:
        from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

        await FlowExecutionOrchestrator.send_command(
            execution_id=str(execution_id),
            command=command_data.command,
            payload=command_data.payload,
            nats_client=nats_client,
        )
        return {"status": "command_sent"}
    except Exception as e:
        logger.error(f"Failed to send command via NATS: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to send command: {str(e)}")


@router.post("/flows/{flow_id}/trigger")
@require_permission("execute_flows")
async def trigger_flow_execution(
    *,
    db: Session = Depends(get_db),
    flow_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
    trigger_event_data: Optional[Dict[str, Any]] = None,
):
    """
    Trigger a test execution for a flow.

    Args:
        flow_id: Flow to trigger
        trigger_event_data: Optional custom trigger event data for testing template variables

    Returns:
        Execution details
    """
    # Verify flow exists and user has access
    flow = crud_flow.get(db=db, id=flow_id, account_id=current_user.account_id)
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")

    # Trigger flow execution
    from preloop.services.flow_trigger_service import FlowTriggerService

    trigger_service = FlowTriggerService(db)
    result = await trigger_service.trigger_flow(
        flow_id=flow_id, test_mode=True, trigger_event_data=trigger_event_data
    )

    return result


@router.post("/flows/executions/{execution_id}/retry")
@require_permission("execute_flows")
async def retry_flow_execution(
    *,
    db: Session = Depends(get_db),
    execution_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
):
    """
    Retry a failed, stopped, or cancelled flow execution.

    Creates a new execution with the same trigger event data as the original.
    The new execution is linked to the original via retry_of_execution_id.

    Args:
        execution_id: The execution to retry

    Returns:
        New execution details
    """
    # Get the original execution
    original = crud_flow_execution.get(
        db=db, id=execution_id, account_id=current_user.account_id
    )
    if not original:
        raise HTTPException(status_code=404, detail="Execution not found")

    # Verify execution is in a retryable state
    retryable_statuses = {"FAILED", "STOPPED", "TIMEOUT", "CANCELLED"}
    if original.status not in retryable_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Execution cannot be retried in status '{original.status}'. "
            f"Only executions with status {retryable_statuses} can be retried.",
        )

    # Verify the flow still exists
    flow = crud_flow.get(db=db, id=original.flow_id, account_id=current_user.account_id)
    if not flow:
        raise HTTPException(
            status_code=404,
            detail="The flow associated with this execution no longer exists.",
        )

    # Trigger a new execution with the same trigger event data
    from preloop.services.flow_trigger_service import FlowTriggerService

    trigger_service = FlowTriggerService(db)
    result = await trigger_service.trigger_flow(
        flow_id=original.flow_id,
        test_mode=False,
        trigger_event_data=original.trigger_event_details,
        retry_of_execution_id=original.id,
    )

    return result


@router.get("/flows/{flow_id}", response_model=schemas.FlowResponse)
@require_permission("view_flows")
def read_flow(
    *,
    db: Session = Depends(get_db),
    flow_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
):
    """Get flow by ID."""
    flow = crud_flow.get(db=db, id=flow_id, account_id=current_user.account_id)
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")
    return flow


@router.put("/flows/{flow_id}", response_model=schemas.FlowResponse)
@require_permission("edit_flows")
def update_flow(
    *,
    db: Session = Depends(get_db),
    flow_id: uuid.UUID,
    flow_in: schemas.FlowUpdate,
    current_user: User = Depends(get_current_active_user),
):
    """Update a flow."""
    flow = crud_flow.get(db=db, id=flow_id, account_id=current_user.account_id)
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")

    # Security: Prevent modifying source_preset_id during update
    # The preset link should only be set during creation (via clone_preset or create)
    # Allowing arbitrary changes could let users pull content from other accounts' flows
    # We forcibly preserve the existing source_preset_id to prevent any modification,
    # including unlinking by setting to None.
    flow_in.source_preset_id = flow.source_preset_id

    # Check for name uniqueness if name is being changed
    # Note: We intentionally allow flows to have the same name as global presets
    if flow_in.name and flow_in.name != flow.name:
        existing_in_account = crud_flow.get_by_name_and_account(
            db, name=flow_in.name, account_id=current_user.account_id
        )
        if existing_in_account and str(existing_in_account.id) != str(flow_id):
            raise HTTPException(
                status_code=400,
                detail=f"A flow with name '{flow_in.name}' already exists in your account",
            )

    # Security check: Only superusers can configure custom commands
    if flow_in.custom_commands and flow_in.custom_commands.enabled:
        if not current_user.is_superuser:
            raise HTTPException(
                status_code=403,
                detail="Only administrators can configure custom commands for security reasons",
            )

    # Detect customization for template-tracked flows
    # If the user modifies the prompt or tools, mark them as customized
    # so they won't be auto-updated when the source preset changes
    if flow.source_preset_id:
        # Check if prompt is being changed
        if flow_in.prompt_template is not None:
            new_prompt_hash = compute_content_hash(flow_in.prompt_template)
            if new_prompt_hash != flow.source_prompt_hash:
                # User is customizing the prompt
                flow_in.prompt_customized = True
                # Clear update notification since they're making their own changes
                flow_in.preset_update_available = False

        # Check if tools are being changed
        if flow_in.allowed_mcp_tools is not None:
            new_tools_hash = compute_content_hash(flow_in.allowed_mcp_tools)
            if new_tools_hash != flow.source_tools_hash:
                # User is customizing the tools
                flow_in.tools_customized = True
                # Clear update notification since they're making their own changes
                flow_in.preset_update_available = False

    old_enabled = flow.is_enabled
    flow = crud_flow.update(
        db=db, db_obj=flow, flow_in=flow_in, account_id=current_user.account_id
    )

    # Determine if this was an enable/disable toggle
    update_fields = flow_in.model_dump(exclude_unset=True)
    if "is_enabled" in update_fields and update_fields["is_enabled"] != old_enabled:
        action = "enabled" if flow.is_enabled else "disabled"
    else:
        action = "updated"

    log_config_change(
        db,
        user=current_user,
        config_type="flow",
        action=action,
        new_value={"id": str(flow.id), "name": flow.name},
    )

    return flow


@router.delete("/flows/{flow_id}", response_model=schemas.FlowResponse)
@require_permission("delete_flows")
def delete_flow(
    *,
    db: Session = Depends(get_db),
    flow_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
):
    """Delete a flow."""
    import logging

    logger = logging.getLogger(__name__)

    # Log the delete attempt for debugging
    logger.info(
        f"Attempting to delete flow {flow_id} for account {current_user.account_id}"
    )

    # Check if flow exists and belongs to the user's account
    flow = crud_flow.get(db=db, id=flow_id, account_id=current_user.account_id)
    if not flow:
        # Flow not found with account filter - check if it exists at all for better error messaging
        flow_any = crud_flow.get(db=db, id=flow_id)
        if not flow_any:
            logger.warning(f"Flow {flow_id} not found in database")
            raise HTTPException(status_code=404, detail="Flow not found")
        else:
            logger.warning(
                f"Flow {flow_id} exists but doesn't belong to account {current_user.account_id} "
                f"(belongs to {flow_any.account_id}, is_preset={flow_any.is_preset})"
            )
            raise HTTPException(status_code=404, detail="Flow not found")

    # Prevent deletion of built-in presets
    if flow.is_preset and flow.account_id is None:
        logger.warning(f"Attempt to delete built-in preset {flow_id}")
        raise HTTPException(
            status_code=403, detail="Cannot delete built-in flow presets"
        )

    flow_name = flow.name  # capture before delete
    crud_flow.remove(db=db, id=flow_id, account_id=current_user.account_id)

    log_config_change(
        db,
        user=current_user,
        config_type="flow",
        action="deleted",
        old_value={"id": str(flow_id), "name": flow_name},
    )

    logger.info(f"Successfully deleted flow {flow_id}")
    return flow


@router.post(
    "/flows/{flow_id}/apply-preset-update", response_model=schemas.FlowResponse
)
@require_permission("edit_flows")
def apply_preset_update(
    *,
    db: Session = Depends(get_db),
    flow_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
):
    """
    Apply pending preset update to a flow.

    This overwrites the flow's prompt and tools with the latest version
    from its source preset. Any customizations will be lost.

    Returns the updated flow.
    """
    from preloop.services.flow_presets_service import apply_preset_update_to_flow

    # Check that flow belongs to user's account
    flow = crud_flow.get(db=db, id=flow_id, account_id=current_user.account_id)
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")

    if not flow.source_preset_id:
        raise HTTPException(
            status_code=400, detail="This flow is not linked to a preset template"
        )

    try:
        updated_flow = apply_preset_update_to_flow(db, flow_id)
        return updated_flow
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/flows/{flow_id}/dismiss-preset-update", response_model=schemas.FlowResponse
)
@require_permission("edit_flows")
def dismiss_preset_update(
    *,
    db: Session = Depends(get_db),
    flow_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
):
    """
    Dismiss the preset update notification for a flow.

    The notification won't reappear until the source preset changes again.

    Returns the updated flow.
    """
    from preloop.services.flow_presets_service import (
        dismiss_preset_update as dismiss_update,
    )

    # Check that flow belongs to user's account
    flow = crud_flow.get(db=db, id=flow_id, account_id=current_user.account_id)
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")

    try:
        updated_flow = dismiss_update(db, flow_id)
        return updated_flow
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/webhooks/flows/{flow_id}/{webhook_secret}")
async def trigger_flow_via_webhook(
    *,
    db: Session = Depends(get_db),
    flow_id: uuid.UUID,
    webhook_secret: str,
    request: Request,
):
    """
    Trigger a flow via webhook (no authentication required - uses secret token in URL).

    This endpoint allows external services to trigger flows without authentication.
    Security is provided by the unguessable webhook_secret in the URL.
    """
    # Get the flow without account filtering
    flow = crud_flow.get(db=db, id=flow_id)
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")

    # Verify this is a webhook trigger
    if flow.trigger_event_source != "webhook":
        raise HTTPException(
            status_code=400, detail="This flow is not configured for webhook triggers"
        )

    # Verify webhook secret
    if (
        not flow.webhook_config
        or flow.webhook_config.get("webhook_secret") != webhook_secret
    ):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    # Check if flow is enabled
    if not flow.is_enabled:
        raise HTTPException(status_code=400, detail="Flow is disabled")

    # Parse webhook payload
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    # Trigger flow execution with webhook payload
    from preloop.services.flow_trigger_service import FlowTriggerService

    trigger_service = FlowTriggerService(db)

    # Create event data from webhook payload
    event_data = {
        "source": "webhook",
        "type": "webhook",
        "payload": payload,
        "account_id": str(flow.account_id),
    }

    # Process the event (will trigger flow execution)
    await trigger_service.process_event(event_data)

    return {"status": "triggered", "flow_id": str(flow_id)}
