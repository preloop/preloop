import asyncio
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any, Dict, List, NoReturn, Optional, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from preloop.models import schemas
from preloop.models.crud import (
    crud_account,
    crud_ai_model,
    crud_api_usage,
    crud_flow_runner,
    crud_flow_feedback,
    crud_runtime_session_activity,
)
from preloop.models.crud.flow import CRUDFlow
from preloop.models.crud.flow_execution import CRUDFlowExecution
from preloop.models.db.session import get_db_session as get_db
from preloop.api.auth import get_current_active_user
from preloop.models.models.user import User
from preloop.schemas.gateway_usage import FlowGatewayUsageSummaryResponse
from preloop.services.execution_metrics import project_execution_totals
from preloop.services.kill_switch import FlowHaltActiveError
from preloop.services.issue_triage_controller import TriageControllerError
from preloop.services.model_gateway_usage import ModelGatewayUsageService
from preloop.utils.hashing import compute_content_hash
from preloop.utils.audit import log_config_change
from preloop.utils.permissions import require_permission
from preloop.utils.workspace_seed import (
    WORKSPACE_FILES_KEY,
    WorkspaceSeedError,
    WorkspaceSeedSizeError,
    parse_workspace_files,
    workspace_seed_payload,
)
from preloop.models.crud.flow_execution_log import crud_flow_execution_log
from preloop.services.runner_service import (
    derive_execution_runner,
    pool_from_session_reference,
    resolve_runner_pool,
    runner_id_from_session_reference,
)
from preloop.services.flow_delegation import (
    CallableFlowsError,
    validate_callable_flows,
)
from preloop.services import flow_tree_stop
from preloop.services.model_routing import (
    ModelRoutingError,
    model_usable_for_agent as _model_usable_for_agent,
    validate_stored_model_routing,
)
from preloop.services.host_exec import (
    host_exec_flow_error,
    host_exec_profile_name,
    host_exec_unavailable_reason,
)
from preloop.services.product_provenance import (
    ProductProvenanceError,
    extract_product_provenance_payload,
    validate_mapping_shape,
)

from preloop.schemas.flow_continuation import (
    ContinuationPreview,
    ContinuationAdoptRequest,
    ContinuationAdoptResponse,
)
from preloop.services.flow_continuation_adoption import (
    ContinuationAdoptionError,
    preview_continuation,
    adopt_continuation,
)
from preloop.cra.evidence_pack import (
    EvidenceMemberError,
    list_evidence_members,
    read_evidence_member,
)
from preloop.services.flow_artifacts import (
    EvidenceUnavailableError,
    attach_evidence_signature,
    load_evidence,
    integrity_state,
    public_evidence_status,
)


router = APIRouter()


crud_flow = CRUDFlow()
crud_flow_execution = CRUDFlowExecution()


def _reject_host_exec_flow(
    *,
    agent_type: Any,
    agent_config: Any,
    runner_pool: Any,
    git_clone_config: Any,
    custom_commands: Any = None,
) -> None:
    """Reject hosted Cursor / publication / invalid host-exec combinations."""
    error = host_exec_flow_error(
        agent_type=agent_type,
        agent_config=agent_config,
        runner_pool=runner_pool,
    )
    if error:
        raise HTTPException(status_code=400, detail=error)
    if host_exec_profile_name(agent_config):
        blocked = host_exec_unavailable_reason(
            git_clone_config=git_clone_config, custom_commands=custom_commands
        )
        if blocked:
            raise HTTPException(status_code=400, detail=blocked)


def _reject_unsupported_persistent_preset(agent_config: Any, preset: Any) -> None:
    """Reject a persistent execution path for a catalog preset that opts out."""
    if preset is None:
        return
    from preloop.services.persistent_workspace import persistent_preset_rejection

    reason = persistent_preset_rejection(agent_config, getattr(preset, "name", None))
    if reason:
        raise HTTPException(status_code=422, detail=reason)


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

    # A schedule_config implies a schedule trigger: default the source
    # (mirrors the webhook auto-default below) and reject contradictory
    # sources so the config can never sit inert on a non-schedule flow.
    if flow_in.schedule_config and not flow_in.trigger_event_source:
        flow_in.trigger_event_source = "schedule"
    if flow_in.schedule_config and flow_in.trigger_event_source != "schedule":
        raise HTTPException(
            status_code=400,
            detail="schedule_config is only valid when trigger_event_source "
            "is 'schedule'",
        )

    # Schedule triggers require a cron schedule_config
    if flow_in.trigger_event_source == "schedule":
        if not flow_in.schedule_config:
            raise HTTPException(
                status_code=400,
                detail="schedule_config is required for schedule triggers",
            )
        flow_in.trigger_event_types = ["schedule"]

    # If this is a webhook trigger, auto-generate a secure webhook secret
    if flow_in.trigger_event_source == "webhook" or (
        not flow_in.trigger_event_source and not flow_in.trigger_event_types
    ):
        # Generate a secure 32-byte URL-safe token
        webhook_secret = secrets.token_urlsafe(32)
        flow_in.webhook_config = schemas.WebhookConfig(webhook_secret=webhook_secret)
        flow_in.trigger_event_source = "webhook"
        flow_in.trigger_event_types = ["webhook"]

    _reject_host_exec_flow(
        agent_type=flow_in.agent_type,
        agent_config=flow_in.agent_config,
        runner_pool=flow_in.runner_pool,
        git_clone_config=flow_in.git_clone_config,
        custom_commands=flow_in.custom_commands,
    )

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
        _reject_unsupported_persistent_preset(flow_in.agent_config, preset)

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

    try:
        validate_stored_model_routing(db, flow_in.agent_config, current_user.account_id)
    except ModelRoutingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Delegation allowlist: every entry has to name a flow in this account.
    # Nothing enforces the list at call time yet, but an entry stored now that
    # resolves to nothing would be an allowlist nobody can read.
    try:
        validate_callable_flows(
            db,
            callable_flows=flow_in.callable_flows,
            account_id=current_user.account_id,
            flow_name=flow_in.name,
        )
    except CallableFlowsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

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
    stats_since: Annotated[
        Optional[datetime],
        Query(
            description=(
                "When set, execution_stats also carries the counts for the "
                "window starting at this time: runs, failed, cost, "
                "token_usage and last_run_at. The flows list states one "
                "period and needs runs and spend measured over the same one. "
                "Without it, token_usage covers all time, like estimated_cost."
            )
        ),
    ] = None,
    current_user: User = Depends(get_current_active_user),
):
    """Retrieve flows for the account."""
    flows = crud_flow.get_multi(
        db, account_id=current_user.account_id, skip=skip, limit=limit
    )

    if flows:
        flow_ids = [f.id for f in flows]
        stats = crud_flow_execution.get_execution_stats_for_flows(
            db, flow_ids, start_date=stats_since
        )

        def _window_fields(stat: Any = None) -> Dict[str, Any]:
            """The per-window counts, only when a window was asked for."""
            if stats_since is None:
                return {}
            last_run_at = getattr(stat, "last_run_at", None) if stat else None
            return {
                "since": stats_since.isoformat(),
                "runs": int(getattr(stat, "runs", 0) or 0) if stat else 0,
                "failed": int(getattr(stat, "failed", 0) or 0) if stat else 0,
                "cost": float(getattr(stat, "cost", 0.0) or 0.0) if stat else 0.0,
                # Tokens before cost: null when no run fell in the window,
                # which the list reads as "nothing to state", not as zero.
                # This shadows the all-time figure below on purpose, so
                # `token_usage` always covers the same period as the counts
                # printed next to it.
                "token_usage": (getattr(stat, "token_usage", None) if stat else None),
                "last_run_at": last_run_at.isoformat() if last_run_at else None,
            }

        stats_map = {
            s.flow_id: {
                "total_execs": s.total_execs,
                "running_execs": int(s.running_execs or 0),
                "last_seen_at": s.last_seen_at.isoformat() if s.last_seen_at else None,
                "estimated_cost": float(s.estimated_cost or 0.0),
                # All-time tokens beside the all-time spend. A row that
                # states money and no volume reads as unmeasured traffic.
                "token_usage": getattr(s, "token_usage", None),
                **_window_fields(s),
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
                    "token_usage": None,
                    **_window_fields(),
                },
            )

    return flows


@router.post("/flows/schedule/preview", response_model=schemas.SchedulePreviewResponse)
@require_permission("view_flows")
def preview_flow_schedule(
    *,
    db: Session = Depends(get_db),
    preview_in: schemas.SchedulePreviewRequest,
    current_user: User = Depends(get_current_active_user),
):
    """Preview a schedule trigger configuration.

    Validates the schedule config (invalid configs are rejected with a
    422 by the schema) and returns a human-readable description plus the
    next few run times, for display in the flow editor.
    """
    config = preview_in.schedule_config
    return schemas.SchedulePreviewResponse(
        type=config.type,
        description=config.describe(),
        timezone=config.timezone,
        next_run_times=config.next_fire_times(count=3),
    )


@router.get("/flows/presets", response_model=List[schemas.FlowResponse])
@require_permission("view_flows")
def read_presets(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Retrieve flow presets available to the account.

    Returns global presets (account_id=None) plus any account-specific presets.
    Global rows carry their catalog ``slug``, the stable identifier
    ``POST /flows/run-preset`` takes, so scripted callers do not have to
    match on a display name that can be renamed.
    """
    from preloop.flow_presets import PRESET_SLUGS_BY_NAME, supports_persistent_for_slug

    presets = crud_flow.get_presets_for_account(db, account_id=current_user.account_id)
    by_id = {}
    for preset in presets:
        preset_id = getattr(preset, "id", None)
        if preset_id is not None:
            by_id[preset_id] = preset
    for preset in presets:
        # Account-specific rows are copies: their name is user-editable and
        # is not catalog identity, so they stay unslugged. Persistent support
        # still follows the catalog preset they were cloned from.
        slug = None
        if getattr(preset, "account_id", None) is None:
            slug = PRESET_SLUGS_BY_NAME.get(getattr(preset, "name", None) or "")
            preset.slug = slug
        else:
            source = by_id.get(getattr(preset, "source_preset_id", None))
            if source is not None and getattr(source, "account_id", None) is None:
                slug = PRESET_SLUGS_BY_NAME.get(getattr(source, "name", None) or "")
            else:
                slug = PRESET_SLUGS_BY_NAME.get(getattr(preset, "name", None) or "")
        preset.supports_persistent = supports_persistent_for_slug(slug)
    return presets


def _resolve_clone_model_binding(
    db: Session, preset: Any, account_id: uuid.UUID
) -> uuid.UUID:
    """Pick the AI model a cloned preset should run with, at CLONE time.

    Presets ship with ``ai_model_id`` unset (they are account-agnostic), so a
    verbatim clone runs the default agent with no credentials and dies at RUN
    time with a raw provider/gateway error. Resolve the binding here instead:

    1. Keep the preset's model when it is set and visible to this account.
    2. Otherwise bind an account model that the preset's agent can use,
       preferring the account default, then the newest.
    3. Otherwise fail the clone with an actionable message.

    Raises:
        HTTPException: 422 when the account has no usable model credentials.
    """
    if preset.ai_model_id:
        model = crud_ai_model.get(db, id=str(preset.ai_model_id))
        if model is not None and (
            model.account_id is None or str(model.account_id) == str(account_id)
        ):
            return cast(uuid.UUID, preset.ai_model_id)

    candidates = [
        model
        for model in crud_ai_model.get_by_account(db, account_id=account_id)
        if _model_usable_for_agent(model, preset.agent_type)
    ]
    if candidates:
        candidates.sort(
            key=lambda m: (
                not m.is_default,
                -(m.created_at.timestamp() if m.created_at else 0.0),
            )
        )
        return cast(uuid.UUID, candidates[0].id)

    raise HTTPException(
        status_code=422,
        detail=(
            f"This preset runs on the '{preset.agent_type}' agent, which needs "
            "an AI model with working credentials, but your account has no "
            "usable AI model configured for it. Add an AI model (with an API "
            "key or gateway access) under Settings, then clone the preset "
            "again."
        ),
    )


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

    # Bind an AI model that will actually work for this account BEFORE
    # creating the clone: presets ship without a model, and a clone without
    # one fails at run time with a raw provider error (never do that to a
    # first run). Raises 422 with an actionable message when the account has
    # no usable model.
    bound_ai_model_id = _resolve_clone_model_binding(
        db, preset, current_user.account_id
    )

    from preloop.services.flow_presets_service import clone_preset_for_account

    return clone_preset_for_account(
        db,
        preset,
        current_user.account_id,
        name=None,
        bound_ai_model_id=bound_ai_model_id,
        flow_crud=crud_flow,
    )


@router.post("/flows/run-preset", response_model=schemas.RunPresetResponse)
@require_permission("execute_flows")
def run_preset(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    body: schemas.RunPresetRequest,
) -> schemas.RunPresetResponse:
    """Run a catalog preset on an issue or a capped triage batch.

    ``confirm_create=false`` resolves only: 409 ``flow_missing`` when the
    account has no clone, or 200 with flow metadata and no execution when
    one exists. ``confirm_create=true`` creates if needed (requires
    ``create_flows``) and starts the run. ``targets`` is a 1-25 issue
    list for ``issue-triage-assistant`` only.
    """
    # Sync handler: an async def would hold Session on the event loop and
    # fail test_async_sync_session_route_count_does_not_grow.
    from preloop.services.preset_runner import PresetRunnerError, run_preset_on_target

    try:
        result = asyncio.run(
            run_preset_on_target(
                db,
                current_user=current_user,
                preset_slug=body.preset_slug,
                target=body.target,
                targets=body.targets,
                confirm_create=body.confirm_create,
                triggered_by=_display_name(current_user),
            )
        )
    except PresetRunnerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return schemas.RunPresetResponse(**result)


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
    # FastAPI injects the response. Optional[Response] makes FastAPI treat it
    # as a Pydantic field and breaks OpenAPI generation, so the default stays
    # None for tests and other direct callers.
    response: Response = None,  # type: ignore[assignment]
    db: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 25,
    flow_id: Optional[uuid.UUID] = None,
    status: Optional[List[str]] = Query(default=None),
    search: Optional[str] = None,
    started_after: Optional[datetime] = None,
    current_user: User = Depends(get_current_active_user),
):
    """Retrieve lightweight flow execution summaries for the account.

    `X-Total-Count` carries how many executions the filters matched, so a
    console page of 25 can say "25 of 1,412 executions" honestly.
    """
    statuses = _normalize_status_filters(status)
    limit = max(1, min(limit, 100))
    search_term = search.strip() if isinstance(search, str) else None
    # Use eager_load=True to load flow relationship in single query (avoids N+1)
    executions = crud_flow_execution.get_multi(
        db,
        account_id=current_user.account_id,
        skip=skip,
        limit=limit,
        flow_id=flow_id,
        statuses=statuses,
        search=search_term or None,
        started_after=started_after,
        eager_load=True,
        lightweight=True,
    )
    total = crud_flow_execution.count(
        db,
        account_id=current_user.account_id,
        flow_id=flow_id,
        statuses=statuses,
        search=search_term or None,
        started_after=started_after,
    )
    if response is not None and isinstance(total, int):
        response.headers["X-Total-Count"] = str(total)

    # Flow is already loaded via joinedload - no additional queries needed
    for execution in executions:
        execution.flow_name = execution.flow.name if execution.flow else None

    _project_models_used(db, executions)
    _project_execution_runners(db, executions)
    # Tool calls and cost from the same aggregation the execution page shows,
    # so a row and the page it opens never state different numbers.
    project_execution_totals(db, executions)

    return executions


def _as_runner_uuid(value: Any) -> Optional[uuid.UUID]:
    """Accept a UUID (or UUID string); ignore mocks and other junk."""
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


def _project_execution_runners(
    db: Session,
    executions: List[Any],
    *,
    resolve_pool_from_flow: bool = False,
) -> None:
    """Attach the derived runner read model to each execution row.

    Private runs are identified by ``runner_id`` or a ``runner:...`` session
    reference. Names come from one batched FlowRunner lookup. Pool is the
    queued-ref pool, or (on detail) the resolved flow/trigger pool.
    """
    if not executions:
        return
    runner_ids: List[uuid.UUID] = []
    for execution in executions:
        runner_id = _as_runner_uuid(getattr(execution, "runner_id", None))
        if runner_id is None:
            runner_id = runner_id_from_session_reference(
                getattr(execution, "agent_session_reference", None)
            )
        if runner_id is not None:
            runner_ids.append(runner_id)
    runners_by_id = {
        row.id: row for row in crud_flow_runner.get_by_ids(db, ids=runner_ids)
    }
    for execution in executions:
        ref = getattr(execution, "agent_session_reference", None)
        runner_id = _as_runner_uuid(getattr(execution, "runner_id", None))
        if runner_id is None:
            runner_id = runner_id_from_session_reference(ref)
        runner = runners_by_id.get(runner_id) if runner_id else None
        pool = pool_from_session_reference(ref)
        if pool is None and resolve_pool_from_flow:
            flow = getattr(execution, "flow", None)
            details = getattr(execution, "trigger_event_details", None) or {}
            if flow is not None:
                pool = resolve_runner_pool(flow, {"trigger_event_data": details})
        execution.runner = derive_execution_runner(
            runner_id=runner_id,
            agent_session_reference=ref if isinstance(ref, str) else None,
            runner_name=getattr(runner, "name", None) if runner else None,
            pool=pool,
        )


def _project_models_used(db: Session, executions: List[Any]) -> None:
    """Attach the model projection to each execution row in one query.

    "Which model ran this" is answered from gateway usage, which the
    execution row itself does not carry. One grouped aggregate covers the
    whole page (see ``crud_api_usage.get_models_used_for_executions``); doing
    it per row would put a query behind every line of the table.
    """
    if not executions:
        return
    models_by_execution = crud_api_usage.get_models_used_for_executions(
        db, [execution.id for execution in executions]
    )
    for execution in executions:
        models = models_by_execution.get(str(execution.id), [])
        execution.models_used = models
        # The alias that served most requests leads; the rest stay in
        # models_used so the console can render "+N".
        execution.model_alias = models[0]["model_alias"] if models else None
        execution.provider_name = models[0]["provider_name"] if models else None


# Terminal execution statuses, mirroring the orchestrator's state machine.
_TERMINAL_EXECUTION_STATUSES = {
    "SUCCEEDED",
    "FAILED",
    "STOPPED",
    "TIMEOUT",
    "CANCELLED",
}


@router.get(
    "/flows/batches/{batch_id}/executions",
    response_model=schemas.BatchExecutionsResponse,
)
@require_permission("view_flows")
def read_batch_executions(
    *,
    db: Session = Depends(get_db),
    batch_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
):
    """List all executions of one matrix/batch trigger with a rollup.

    The rollup aggregates status counts, tokens, tool calls and estimated
    cost across the batch so an eval matrix can be observed as a unit.
    """
    from preloop.models.models.flow_execution import MATRIX_OVERRIDES_KEY

    executions = crud_flow_execution.get_by_batch(
        db, batch_id=batch_id, account_id=current_user.account_id
    )
    if not executions:
        raise HTTPException(status_code=404, detail="Batch not found")

    # Projected before validation below, so each cell of a model matrix says
    # which model actually served it.
    _project_models_used(db, executions)
    _project_execution_runners(db, executions, resolve_pool_from_flow=True)

    by_status: Dict[str, int] = {}
    total_tokens = 0
    total_cost = 0.0
    total_tool_calls = 0
    completed = 0
    items = []
    for execution in executions:
        by_status[execution.status] = by_status.get(execution.status, 0) + 1
        total_tokens += execution.total_tokens or 0
        total_cost += float(execution.estimated_cost or 0)
        total_tool_calls += execution.tool_calls_count or 0
        if execution.status in _TERMINAL_EXECUTION_STATUSES:
            completed += 1
        cell = (execution.trigger_event_details or {}).get(MATRIX_OVERRIDES_KEY)
        item = schemas.BatchExecutionListItem.model_validate(execution)
        item.flow_name = execution.flow.name if execution.flow else None
        item.matrix = (
            {
                "index": cell.get("index"),
                "agent_type": cell.get("agent_type"),
                "ai_model_id": cell.get("ai_model_id"),
            }
            if cell
            else None
        )
        items.append(item)

    # Present cells in matrix order (creation order is the fallback for rows
    # without a recorded index).
    items.sort(
        key=lambda i: (
            i.matrix["index"]
            if i.matrix and i.matrix.get("index") is not None
            else 1_000_000
        )
    )

    return schemas.BatchExecutionsResponse(
        batch_id=batch_id,
        flow_id=executions[0].flow_id,
        rollup=schemas.BatchRollup(
            total=len(executions),
            by_status=by_status,
            completed=completed,
            total_tokens=total_tokens,
            total_estimated_cost=round(total_cost, 4),
            total_tool_calls=total_tool_calls,
        ),
        executions=items,
    )


#: Most executions one tree read returns. The depth and fan-out caps
#: (``FLOW_DELEGATION_MAX_DEPTH``, ``FLOW_DELEGATION_MAX_CHILDREN``) already
#: bound a tree far below this, but they are settings and a read that trusts a
#: setting is a read with no bound at all. A tree at the cap is reported as
#: truncated rather than paginated: nobody reads the thousandth child.
EXECUTION_TREE_MAX_NODES = 1000


def _tree_node(execution: Any, *, label: Optional[str]) -> schemas.ExecutionTreeNode:
    """Turn one execution row into a tree row."""
    node = schemas.ExecutionTreeNode.model_validate(execution)
    node.flow_name = execution.flow.name if execution.flow else None
    node.label = label
    return node


def _label_from_trigger_details(details: Any) -> Optional[str]:
    """Read the delegation label out of an already loaded trigger payload.

    Only for the one execution the request names, which is read in full
    anyway. Every other row in the tree gets the same value projected by the
    query, because loading a webhook payload per row to find one string is
    not a trade worth making.
    """
    from preloop.models.models.flow_execution import DELEGATION_DETAILS_KEY

    if not isinstance(details, dict):
        return None
    delegation = details.get(DELEGATION_DETAILS_KEY)
    if not isinstance(delegation, dict):
        return None
    label = delegation.get("label")
    return str(label) if label else None


@router.get(
    "/flows/executions/{execution_id}/tree",
    response_model=schemas.ExecutionTreeResponse,
)
@require_permission("view_flows")
def read_execution_tree(
    *,
    db: Session = Depends(get_db),
    execution_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
):
    """List what one execution delegated, with a rollup over the subtree.

    The whole lineage is read in one indexed query (every descendant carries
    the root's id) and the subtree under the requested execution is walked
    from that in memory, so asking a child about its own children costs the
    same as asking the root. A run that delegated nothing answers with an
    empty list and a zeroed rollup rather than a 404: "nothing here" is the
    common case and the page renders it as an empty state.

    The rollup covers the descendants only. The requested execution's own
    cost and tokens come back on ``execution``, so the console can show "this
    run cost X, what it started cost Y" without adding the two numbers.
    """
    execution = crud_flow_execution.get(
        db=db, id=execution_id, account_id=current_user.account_id
    )
    if not execution:
        raise HTTPException(status_code=404, detail="Flow execution not found")

    root_execution_id = execution.root_execution_id or execution.id
    lineage = crud_flow_execution.get_lineage(
        db,
        root_execution_id=root_execution_id,
        account_id=current_user.account_id,
        limit=EXECUTION_TREE_MAX_NODES + 1,
    )
    truncated = len(lineage) > EXECUTION_TREE_MAX_NODES
    lineage = lineage[:EXECUTION_TREE_MAX_NODES]

    children_by_parent: Dict[uuid.UUID, List[Any]] = {}
    for row in lineage:
        parent_id = row.parent_execution_id
        if parent_id is None:
            continue
        children_by_parent.setdefault(parent_id, []).append(row)

    # Breadth first from the execution asked about, so a row's parent is
    # always earlier in the list than the row itself and the console can
    # build the tree in one pass. Bounded by the rows actually read.
    subtree: List[Any] = []
    frontier = [execution.id]
    while frontier:
        next_frontier: List[uuid.UUID] = []
        for parent_id in frontier:
            for row in children_by_parent.get(parent_id, []):
                subtree.append(row)
                next_frontier.append(row.id)
        frontier = next_frontier

    by_status: Dict[str, int] = {}
    total_tokens = 0
    total_cost = 0.0
    total_tool_calls = 0
    completed = 0
    for row in subtree:
        by_status[row.status] = by_status.get(row.status, 0) + 1
        total_tokens += row.total_tokens or 0
        total_cost += float(row.estimated_cost or 0)
        total_tool_calls += row.tool_calls_count or 0
        if row.status in _TERMINAL_EXECUTION_STATUSES:
            completed += 1

    return schemas.ExecutionTreeResponse(
        execution_id=execution.id,
        root_execution_id=root_execution_id,
        execution=_tree_node(
            execution,
            label=_label_from_trigger_details(execution.trigger_event_details),
        ),
        executions=[
            _tree_node(row, label=getattr(row, "delegation_label", None))
            for row in subtree
        ],
        rollup=schemas.BatchRollup(
            total=len(subtree),
            by_status=by_status,
            completed=completed,
            total_tokens=total_tokens,
            total_estimated_cost=round(total_cost, 4),
            total_tool_calls=total_tool_calls,
        ),
        truncated=truncated,
    )


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

    # The gateway's recorded activity is authoritative for the outcome of a
    # governed call (succeeded/refused/failed and the refusal string), so it is
    # preferred over the parsed "detected" markers. Parsed rows whose tool was
    # not recorded by the gateway (for example an ungoverned MCP server) are
    # kept so the timeline does not lose calls. Matching is one recorded row
    # to one parsed marker (correlation_id, else timestamp proximity) so a
    # single recorded call does not drop the tool's whole parse history.
    activity_rows = crud_runtime_session_activity.list_tool_calls_for_flow_execution(
        db,
        account_id=current_user.account_id,
        flow_execution_id=execution.id,
    )
    if activity_rows:

        def _activity_log(row: Any) -> Dict[str, Any]:
            # Present the outcome with the same keys the parsed rows use, so
            # the console shows a refusal string where it already shows an
            # error, and never leaks the raw argument payload.
            succeeded = str(row.status or "").startswith("succ")
            entry: Dict[str, Any] = {
                "timestamp": row.timestamp.isoformat() if row.timestamp else None,
                "tool_name": row.tool_name,
                "server_name": row.server_name,
                "status": row.status,
                "summary": row.summary,
                "result_summary": row.summary if succeeded else None,
                "error": None if succeeded else row.summary,
                "correlation_id": (row.metadata_ or {}).get("correlation_id"),
                "arguments_summary": (row.metadata_ or {}).get("arguments_summary"),
            }
            started_at = (row.metadata_ or {}).get("started_at")
            if started_at:
                entry["started_at"] = started_at
            return entry

        activity_logs = [_activity_log(row) for row in activity_rows]
        existing_logs: List[Any] = (
            execution.mcp_usage_logs
            if isinstance(execution.mcp_usage_logs, list)
            else []
        )
        parsed_logs = [log for log in existing_logs if isinstance(log, dict)]
        used_parsed: set[int] = set()
        for activity in activity_logs:
            match_index = _match_parsed_mcp_marker(activity, parsed_logs, used_parsed)
            if match_index is not None:
                used_parsed.add(match_index)
        leftover_parsed = [
            log for index, log in enumerate(parsed_logs) if index not in used_parsed
        ]
        leftover_other = [log for log in existing_logs if not isinstance(log, dict)]
        execution.mcp_usage_logs = sorted(
            activity_logs + leftover_parsed + leftover_other,
            key=lambda log: (
                (log.get("timestamp") if isinstance(log, dict) else "") or ""
            ),
        )

    # The model that ran this execution, from the same gateway usage the list
    # projects, so the detail page and the table never disagree.
    _project_models_used(db, [execution])
    _project_execution_runners(db, [execution], resolve_pool_from_flow=True)
    # Same for tool calls and cost: the page hydrates its strip from this row
    # before /metrics answers, and the number must not change under the user.
    project_execution_totals(db, [execution])
    _project_execution_park(db, execution)

    return execution


def _parse_mcp_log_timestamp(value: Any) -> Optional[datetime]:
    """Parse a timeline timestamp from an ISO string or datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


# Slack around a call's [started_at, timestamp] interval. Parsed markers
# are stamped at call start and gateway rows at call end, so a fixed window
# around the row timestamp misses any call longer than the window.
_MCP_USAGE_MATCH_WINDOW_SECONDS = 5.0


def _match_parsed_mcp_marker(
    activity: Dict[str, Any],
    parsed_logs: List[Dict[str, Any]],
    used: set[int],
) -> Optional[int]:
    """Return the index of one parsed marker matching a recorded activity.

    Prefers correlation_id when both sides carry it; otherwise matches the
    same client-visible tool_name when the marker falls inside
    ``[started_at - slack, timestamp + slack]``. Rows without ``started_at``
    use the row timestamp for both ends. Nearest marker wins, one to one.

    Args:
        activity: Recorded gateway usage row projected for the timeline.
        parsed_logs: Parsed "detected" markers from the agent log.
        used: Indices already matched to another recorded row.

    Returns:
        Index into ``parsed_logs`` to retire, or None if no marker matches.
    """
    corr = activity.get("correlation_id")
    if corr:
        for index, parsed in enumerate(parsed_logs):
            if index in used:
                continue
            if parsed.get("correlation_id") == corr:
                return index

    act_tool = activity.get("tool_name")
    if not act_tool:
        return None
    act_end = _parse_mcp_log_timestamp(activity.get("timestamp"))
    act_start = _parse_mcp_log_timestamp(activity.get("started_at")) or act_end
    if act_end is None:
        act_end = act_start
    best_index: Optional[int] = None
    best_delta: Optional[float] = None
    for index, parsed in enumerate(parsed_logs):
        if index in used:
            continue
        if parsed.get("tool_name") != act_tool:
            continue
        parsed_ts = _parse_mcp_log_timestamp(parsed.get("timestamp"))
        if act_end is None or parsed_ts is None:
            # Same tool name without usable timestamps: retire the first
            # unmatched marker so one recorded call still maps to one parse.
            if best_index is None:
                return index
            continue
        if act_start is not None and act_end is not None and act_start > act_end:
            act_start, act_end = act_end, act_start
        window_start = act_start - timedelta(seconds=_MCP_USAGE_MATCH_WINDOW_SECONDS)
        window_end = act_end + timedelta(seconds=_MCP_USAGE_MATCH_WINDOW_SECONDS)
        if parsed_ts < window_start or parsed_ts > window_end:
            continue
        if parsed_ts < act_start:
            delta = (act_start - parsed_ts).total_seconds()
        elif parsed_ts > act_end:
            delta = (parsed_ts - act_end).total_seconds()
        else:
            delta = 0.0
        if best_delta is None or delta < best_delta:
            best_index = index
            best_delta = delta
    return best_index


def _project_execution_park(db: Session, execution: Any) -> None:
    """Attach why a parked execution is waiting, and until when.

    Read-time projection: the park columns on the execution say which request
    it is blocked on, and the request says who is being waited on and what was
    asked. The console renders "waiting for <who> since <when>, expires
    <when>" from this without knowing how approvals are stored.
    """
    import logging

    from preloop.models.schemas.flow_execution import ExecutionPark
    from preloop.services.approval_park import WAITING_FOR_HUMAN

    execution.park = None
    if str(getattr(execution, "status", "")) != WAITING_FOR_HUMAN:
        return
    request_id = getattr(execution, "park_request_id", None)
    if request_id is None:
        return
    waiting_for = None
    tool_name = None
    question = None
    try:
        from preloop.models.models.approval_request import ApprovalRequest

        request = db.get(ApprovalRequest, request_id)
        if request is not None:
            tool_name = getattr(request, "tool_name", None)
            tool_args = getattr(request, "tool_args", None) or {}
            if isinstance(tool_args, dict):
                question = tool_args.get("question") or tool_args.get("reason")
            workflow = getattr(request, "approval_workflow", None)
            waiting_for = getattr(workflow, "name", None)
    except Exception:
        logging.getLogger(__name__).debug(
            "Could not load the approval behind a parked execution", exc_info=True
        )
    execution.park = ExecutionPark(
        request_id=request_id,
        since=getattr(execution, "parked_at", None),
        expires_at=getattr(execution, "park_expires_at", None),
        waiting_for=waiting_for,
        tool_name=tool_name,
        question=str(question)[:2000] if question else None,
    )


@router.get(
    "/flows/executions/{execution_id}/continuation", response_model=ContinuationPreview
)
@require_permission("view_flows")
def preview_execution_continuation(
    *,
    db: Session = Depends(get_db),
    execution_id: uuid.UUID,
    pr_url: str | None = None,
    branch: str | None = None,
    current_user: User = Depends(get_current_active_user),
) -> ContinuationPreview:
    """Inspect one publication without enabling repairs or holding DB during I/O."""
    account_id = current_user.account_id
    crud_flow_feedback.release_read(db)
    try:
        return preview_continuation(
            account_id, execution_id, pr_url=pr_url, branch=branch
        )
    except ContinuationAdoptionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post(
    "/flows/executions/{execution_id}/continuation",
    response_model=ContinuationAdoptResponse,
)
@require_permission("execute_flows")
def adopt_execution_continuation(
    *,
    db: Session = Depends(get_db),
    execution_id: uuid.UUID,
    request: ContinuationAdoptRequest,
    current_user: User = Depends(get_current_active_user),
) -> ContinuationAdoptResponse:
    """Explicitly subscribe one previously published PR to bounded repairs."""
    account_id = current_user.account_id
    crud_flow_feedback.release_read(db)
    try:
        return adopt_continuation(account_id, execution_id, request)
    except ContinuationAdoptionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/flows/executions/{execution_id}/result")
@require_permission("view_flows")
def get_flow_execution_result(
    *,
    db: Session = Depends(get_db),
    execution_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """Get the structured result artifact reported by a flow execution.

    Eval/observe flows write ``/workspace/result.json`` as their final
    report; the runner captures it as a first-class execution artifact.
    Returns 404 if the execution does not exist or reported no result.

    ``evidence`` is the persisted receipt only: bounded, account-scoped,
    no decrypt. Availability is not integrity proof; download
    ``GET .../evidence`` to verify digest.
    """
    execution = crud_flow_execution.get(
        db=db, id=execution_id, account_id=current_user.account_id
    )
    if not execution:
        raise HTTPException(status_code=404, detail="Flow execution not found")
    if execution.result is None:
        raise HTTPException(
            status_code=404,
            detail="Flow execution did not report a result artifact",
        )
    return {
        "execution_id": str(execution.id),
        "status": execution.status,
        "result": execution.result,
        "evidence": public_evidence_status(execution),
    }


@router.get("/flows/executions/{execution_id}/evidence-status")
@require_permission("view_flows")
def get_flow_execution_evidence_status(
    *,
    db: Session = Depends(get_db),
    execution_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """Report evidence availability without downloading the pack.

    Status is one of ``available``, ``missing``, ``expired``, or ``failed``.
    Served from the persisted execution receipt (no decrypt). Availability
    is not integrity proof. ``object_lock`` is always false: this API does
    not implement WORM retention and cannot verify a property of the storage
    layer beneath it. ``legal_hold`` is true while a hold covers the pack or
    its execution, which blocks payload expiry and purge inside Preloop.

    ``signature`` is the detached Ed25519 signature made when the pack was
    captured, with the payload it covers, or null for a pack captured before
    signing existed. ``preloop evidence verify`` checks it against the
    account's public key.
    """
    execution = crud_flow_execution.get(
        db=db, id=execution_id, account_id=current_user.account_id
    )
    if not execution:
        raise HTTPException(status_code=404, detail="Flow execution not found")
    return attach_evidence_signature(
        db,
        account_id=current_user.account_id,
        receipt=public_evidence_status(execution),
    )


@router.get("/flows/executions/{execution_id}/evidence")
@require_permission("view_flows")
def get_flow_execution_evidence(
    *,
    db: Session = Depends(get_db),
    execution_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
) -> Response:
    """Download the evidence pack captured for a flow execution.

    Audit-style flows write an evidence pack under ``/workspace/evidence``;
    the runner captures it as a tar.gz archive after the agent finishes.
    Durable artifacts are served after digest verification. Distinct
    outcomes: 404 missing, 410 expired, 409 failed. Legacy executions that
    only stored ``evidence_archive`` remain readable.
    """
    execution = crud_flow_execution.get(
        db=db, id=execution_id, account_id=current_user.account_id
    )
    if not execution:
        raise HTTPException(status_code=404, detail="Flow execution not found")
    archive, receipt = _load_verified_evidence(
        db, execution=execution, account_id=current_user.account_id
    )
    return Response(
        content=archive,
        media_type="application/gzip",
        headers=_evidence_download_headers(
            execution,
            receipt,
            filename=f"evidence-{execution.id}.tar.gz",
        ),
    )


def _evidence_download_headers(
    execution: Any,
    receipt: dict[str, Any],
    *,
    filename: str,
    member_path: str | None = None,
) -> dict[str, str]:
    """Integrity and signature headers shared by the pack and member reads.

    The full download is not written to the audit log. A member read uses the
    same headers and the same omission, so the two reads leave the same trail.
    """
    digest = receipt.get("sha256") or receipt.get("digest") or ""
    signature = receipt.get("signature") or {}
    safe_name = filename.replace('"', "").replace("\r", "").replace("\n", "")
    headers = {
        "Content-Disposition": f'attachment; filename="{safe_name}"',
        "Cache-Control": "no-store",
        "X-Preloop-Evidence-Status": str(receipt.get("status") or "available"),
        "X-Preloop-Evidence-Kind": "evidence",
        "X-Preloop-Evidence-Integrity": (
            "verified" if receipt.get("integrity_verified") else "unverified"
        ),
        "X-Preloop-Evidence-Integrity-State": str(
            receipt.get("integrity")
            or integrity_state(
                verified=bool(receipt.get("integrity_verified")),
                error=receipt.get("error"),
            )
        ),
    }
    if digest:
        headers["X-Preloop-Evidence-SHA256"] = str(digest)
    if member_path:
        headers["X-Preloop-Evidence-Member"] = member_path
    if signature:
        headers["X-Preloop-Signature"] = str(signature.get("signature") or "")
        headers["X-Preloop-Signing-Key-Id"] = str(signature.get("key_id") or "")
        headers["X-Preloop-Signed-At"] = str(signature.get("signed_at") or "")
    return headers


def _load_verified_evidence(
    db: Session, *, execution: Any, account_id: Any
) -> tuple[bytes, dict[str, Any]]:
    """Decrypt and verify a pack, or raise the same HTTP errors as download."""
    try:
        archive, receipt = load_evidence(db, account_id=account_id, execution=execution)
    except EvidenceUnavailableError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return archive, attach_evidence_signature(
        db, account_id=account_id, receipt=receipt
    )


@router.get(
    "/flows/executions/{execution_id}/evidence/members",
    response_model=None,
    responses={
        200: {
            "description": (
                "JSON member list, or one member's bytes when path is set."
            ),
            "content": {
                "application/json": {"schema": {"type": "object"}},
                "text/markdown": {"schema": {"type": "string"}},
                "text/plain": {"schema": {"type": "string"}},
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                },
            },
            "headers": {
                "X-Preloop-Evidence-Integrity": {"schema": {"type": "string"}},
                "X-Preloop-Evidence-SHA256": {"schema": {"type": "string"}},
                "X-Preloop-Evidence-Member": {"schema": {"type": "string"}},
            },
        }
    },
)
@require_permission("view_flows")
def get_flow_execution_evidence_members(
    *,
    db: Session = Depends(get_db),
    execution_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
    path: str | None = None,
) -> Dict[str, Any] | Response:
    """List a pack's manifest members, or return one member when ``path`` is set.

    Auth, decryption, digest verification and legal hold follow
    ``GET .../evidence``. Only a path that the manifest lists is readable.
    Absolute paths and ``..`` are refused. A member larger than 8 MiB is
    refused; download the pack for that file. Markdown, JSON and plain text
    are served with those content types.

    The full download is not audited. This read is not audited either.
    """
    execution = crud_flow_execution.get(
        db=db, id=execution_id, account_id=current_user.account_id
    )
    if not execution:
        raise HTTPException(status_code=404, detail="Flow execution not found")
    archive, receipt = _load_verified_evidence(
        db, execution=execution, account_id=current_user.account_id
    )
    try:
        if path is None:
            members = list_evidence_members(archive)
        else:
            body, meta = read_evidence_member(archive, path)
    except EvidenceMemberError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    if path is None:
        return {
            "execution_id": str(execution.id),
            "status": receipt.get("status") or "available",
            "sha256": receipt.get("sha256") or receipt.get("digest"),
            "integrity": receipt.get("integrity"),
            "integrity_note": receipt.get("integrity_note"),
            "legal_hold": bool(receipt.get("legal_hold")),
            "members": members,
        }
    leaf = meta["path"].rsplit("/", 1)[-1]
    return Response(
        content=body,
        media_type=meta["content_type"],
        headers=_evidence_download_headers(
            execution,
            receipt,
            filename=leaf or "member",
            member_path=meta["path"],
        ),
    )


@router.get(
    "/flows/executions/{execution_id}/workspace",
    responses={
        200: {
            "description": "Size-capped tar.gz of /workspace from this execution.",
            "content": {
                "application/gzip": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
@require_permission("view_flows")
def get_flow_execution_workspace(
    *,
    db: Session = Depends(get_db),
    execution_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
) -> Response:
    """Download the workspace snapshot captured for a flow execution.

    Every hosted run leaves a size-capped tar.gz of ``/workspace`` (``.git``
    included, so commits that were never pushed are in it) behind. Returns 404
    if the execution does not exist, if the workspace was too large to keep,
    or if the retention window has passed and the janitor reaped it.
    """
    execution = crud_flow_execution.get(
        db=db, id=execution_id, account_id=current_user.account_id
    )
    if not execution:
        raise HTTPException(status_code=404, detail="Flow execution not found")
    if not execution.workspace_snapshot:
        raise HTTPException(
            status_code=404,
            detail="Flow execution has no captured workspace snapshot",
        )
    return Response(
        content=bytes(execution.workspace_snapshot),
        media_type="application/gzip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="workspace-{execution.id}.tar.gz"'
            )
        },
    )


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
    """Get execution logs from the live container or from persisted database rows.

    For running container-backed executions, fetches logs directly from the
    Docker/Kubernetes container. Runner-backed executions (lease references
    such as ``runner:{runner_id}:{execution_id}``) always return persisted
    database logs, even while they are still running, because their output
    arrives over the runner WebSocket rather than a container log stream.
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

    # Runner-backed executions carry a lease reference
    # (``runner:{runner_id}:{execution_id}`` or
    # ``runner:queued:{pool}:{execution_id}``), not a container or Job name.
    # Their output arrives over the runner WebSocket into flow_execution_log,
    # so the container path would only build an invalid Kubernetes selector
    # (HTTP 400) and return an empty list that hides the database logs.
    session_reference = execution.agent_session_reference
    runner_backed = bool(
        session_reference
        and (
            runner_id_from_session_reference(session_reference) is not None
            or pool_from_session_reference(session_reference) is not None
        )
    )

    if is_running and session_reference and not runner_backed:
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
        - estimated_cost: Estimated cost in USD, or null when no usage could
          be priced (never a placeholder 0.0, which would misreport real
          spend as free)
        - has_pricing: Whether any request could be priced
        - cost_is_partial: Whether estimated_cost excludes unpriced requests
        - unpriced_requests: Requests that could not be priced
        - unpriced_tokens: Token volume behind the unpriced requests
        - limits: Configured per-execution ceilings (only keys that are set)
        - limit_status: Current usage against those ceilings
          (``total_tokens``, ``estimated_cost_usd``, ``turns``)
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
        # Per-execution ceilings and how the run measures against them, so the
        # page can render "used / allowed" without re-reading the flow. Empty
        # limits and zero usage for flows that configure none.
        from preloop.services.flow_execution_limits import (
            describe_execution_limits,
        )

        limits, usage = describe_execution_limits(db, execution)
        metrics["limits"] = limits.as_dict()
        metrics["limit_status"] = {
            "total_tokens": usage.total_tokens,
            "estimated_cost_usd": usage.cost_usd,
            "turns": usage.turns,
        }
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
            # Metrics could not be computed, so the cost is unknown rather
            # than zero; the UI renders this as unavailable, not as free.
            "estimated_cost": None,
            "has_pricing": False,
            "cost_is_partial": False,
            "unpriced_requests": 0,
            "unpriced_tokens": 0,
            "limits": {},
            "limit_status": {
                "total_tokens": 0,
                "estimated_cost_usd": None,
                "turns": 0,
            },
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
        # A parent parked on the flows it started leaves the park here, before
        # any I/O, and terminally (#689). From this write on, a child reaching
        # a terminal state claims nothing and the sweep lists nothing, so the
        # stop cannot race a resume into existence while the container teardown
        # below takes its seconds. The tree itself is stopped after the status
        # update, once this execution is unambiguously terminal.
        stops_a_tree = flow_tree_stop.parked_on_children(execution)
        if stops_a_tree:
            flow_tree_stop.close_children_park(db, parent=execution)

        session_reference = execution.agent_session_reference
        stoppable = execution.status in [
            "RUNNING",
            "STARTING",
            "INITIALIZING",
            "PENDING",
        ]
        runner_id = runner_id_from_session_reference(session_reference)
        queued_pool = pool_from_session_reference(session_reference)
        if runner_id is not None and stoppable:
            # Runner-backed execution: the lease reference is not a container
            # or Job name, so a container executor cannot see or stop the
            # runner's process (and only builds an invalid Kubernetes
            # selector trying). Flag the halt so the runner stops the job
            # itself; its output already streams into flow_execution_log.
            # Halt is per assignment: this runner may be running other jobs
            # that nobody asked to stop.
            if crud_flow_runner.request_halt(
                db, runner_id=runner_id, execution_id=execution_id
            ):
                logger.info(
                    f"Requested halt on runner {runner_id} for execution {execution_id}"
                )
        elif queued_pool is not None and stoppable:
            # Queued for a private pool: nothing runs yet, so there is no
            # container, Job, or runner to stop. The status update below is
            # all that is needed.
            pass
        # Stop the container if it's running
        elif session_reference and stoppable:
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

        # Stopping a parent stops the flows it was waiting for (#689): the
        # decision, why it is the one taken, and what the operator sees are in
        # preloop/services/flow_tree_stop.py. Never fails the stop: this
        # execution is already terminal and every write below is retried by
        # nothing, so a failure here must be visible in the log rather than as
        # a 500 on a stop that did happen.
        if stops_a_tree:
            try:
                await flow_tree_stop.stop_tree_for_stopped_parent(
                    db,
                    parent=execution,
                    account_id=current_user.account_id,
                    nats_client=nats_client,
                )
            except Exception:
                logger.exception(
                    "Failed to stop the tree of execution %s", execution_id
                )

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


def _display_name(user: User) -> str:
    """The name to attribute a manually triggered run to in the console.

    Prefers the operator's own name, then the username, and finally the
    email, so the executions list says "Manual Run by Ada Lovelace" rather
    than repeating an opaque id.
    """
    name = (getattr(user, "full_name", None) or "").strip()
    return name or getattr(user, "username", None) or getattr(user, "email", "") or ""


def _halted_response() -> HTTPException:
    """Distinct 403 for manual triggers blocked by the kill switch (#157)."""
    from preloop.services.kill_switch import FLOW_DENIAL_MESSAGE

    return HTTPException(status_code=403, detail=FLOW_DENIAL_MESSAGE)


def _reject_oversized_workspace_seeds(
    trigger_event_data: Optional[Dict[str, Any]],
) -> None:
    """Validate ``workspace_files`` before an execution row exists.

    Seeds used to be checked only in the orchestrator, so an invalid or
    oversized declaration produced a FAILED execution and a message about the
    agent rather than a rejected request. The caller learns nothing actionable
    from a 200 followed by a failure, and the failure text pointed at the
    wrong subsystem (preloop/preloop#505).

    413 rather than 400 for the size caps: the request is well formed, it is
    the payload that is too large, and 413 is what a client library retries
    with a smaller body.

    The lookup is ``workspace_seed_payload``, shared with every other reader,
    so a declaration beside ``payload`` is validated here instead of being
    accepted with 200 and then seeding nothing (preloop/preloop#509).
    """
    try:
        container = workspace_seed_payload(trigger_event_data)
    except WorkspaceSeedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not isinstance(container, dict) or WORKSPACE_FILES_KEY not in container:
        return
    try:
        parse_workspace_files(container)
    except WorkspaceSeedError as exc:
        raise HTTPException(
            status_code=413 if isinstance(exc, WorkspaceSeedSizeError) else 400,
            detail=str(exc),
        ) from exc


# Top-level trigger keys the platform writes itself. A manual trigger that
# sets one is rejected rather than merged: every one of them is read later as
# trusted control state, not as data. ``_resume`` and ``_answers`` steer the
# park/resume handshake (approval_park.py), ``_answers_prompt`` and
# ``_feedback_prompt`` are concatenated into the agent prompt, ``_ci_failure``
# carries CI feedback, and ``_workspace_file_paths`` / ``_subject`` /
# ``_stop_coverage`` are audit stamps a caller must not be able to author: a
# forged coverage record would claim a stopped tree reached work it never did.
#
# This list is deliberately not "every key FlowCreate does not define". The
# body is documented as free-form template data: flow_orchestrator.py:1252
# resolves ``{{name}}`` through ``_simple_resolve`` (line 1531) straight off
# the top level of trigger_event_data, so rejecting unknown top-level keys
# would break the documented way to pass template variables. Reserved keys are
# the ones where a caller can actually forge platform state, so those are the
# ones that get a 4xx.
#
# ``_matrix`` and ``_model_routing`` are deliberately absent: they are also
# reserved, but they already have a stripped-and-recomputed contract with
# tests of its own (test_model_routing.py::TestRoutingEndpointTrust), and
# neutralizing a key is a fine answer when the platform recomputes the value
# from trusted state. The keys below have no such recomputation. A forged
# ``_resume.source_branch`` reaches container.py:3417 and decides which branch
# the agent clones and pushes to, so it is refused rather than carried.
RESERVED_TRIGGER_KEYS = frozenset(
    {
        "_resume",
        "_answers",
        "_answers_prompt",
        "_children",
        "_children_prompt",
        "_stop_coverage",
        "_feedback_prompt",
        "_ci_failure",
        "_workspace_file_paths",
        "_subject",
        "_agent_control",
    }
)


def _reject_invalid_product_provenance(
    trigger_event_data: Optional[Dict[str, Any]],
) -> None:
    """Validate ``product_provenance`` before an execution row exists.

    A mapping the platform will always refuse is a bad request, not a failed
    run. Validating it only in the orchestrator meant the caller paid an
    execution to receive a validation message, and the flow's history carried
    a FAILED row for something no agent ever attempted (staging execution
    42b9d159, 0 tokens). Same treatment as the workspace-seed budget check
    directly below: 400 at the trigger, nothing created.

    Only the body-decided half runs here. Whether the declared SHAs match the
    checkout the run actually got is a runtime fact, so it stays in the
    orchestrator where the facts are. Webhook triggers wrap the caller's
    JSON as ``payload``, so the same check runs there: a mapping inside the
    webhook body is still a bad request, not a failed execution.
    """
    try:
        mapping = extract_product_provenance_payload(trigger_event_data)
        if mapping is not None:
            validate_mapping_shape(mapping)
    except ProductProvenanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _reject_reserved_trigger_keys(
    trigger_event_data: Optional[Dict[str, Any]],
) -> None:
    """Reject a manual trigger body that forges platform-internal keys."""
    if not isinstance(trigger_event_data, dict):
        return
    present = sorted(RESERVED_TRIGGER_KEYS.intersection(trigger_event_data))
    if not present:
        return
    raise HTTPException(
        status_code=400,
        detail=(
            f"Trigger body sets reserved key(s) {', '.join(present)}, which "
            "the platform writes itself. Remove them; flow inputs go in "
            "'payload' (see docs/guide/flows/security-audit-presets.md)."
        ),
    )


def _validate_matrix(
    db: Session, matrix: Any, account_id: uuid.UUID
) -> List[Dict[str, Any]]:
    """Validate a matrix trigger payload; raise HTTP 422 on any bad entry.

    Validation is all-or-nothing: no execution is created unless every entry
    passes, so a batch never partially materialises from a malformed request.
    Entry shape (allowed keys, UUID coercion) is enforced by the
    ``FlowMatrixEntry`` schema; the allowed agent types come straight from the
    agent factory registry, so both stay in one place.
    """
    from pydantic import ValidationError

    from preloop.agents.factory import SUPPORTED_AGENT_TYPES
    from preloop.services.flow_trigger_service import MATRIX_MAX_ENTRIES

    def _reject(detail: str) -> NoReturn:
        raise HTTPException(status_code=422, detail=detail)

    if not isinstance(matrix, list) or not matrix:
        _reject("matrix must be a non-empty list of objects")
    if len(matrix) > MATRIX_MAX_ENTRIES:
        _reject(f"matrix supports at most {MATRIX_MAX_ENTRIES} entries")

    validated: List[Dict[str, Any]] = []
    for index, entry in enumerate(matrix):
        if not isinstance(entry, dict):
            _reject(f"matrix[{index}] must be an object")
        try:
            parsed = schemas.FlowMatrixEntry.model_validate(entry)
        except ValidationError as exc:
            unknown = sorted(
                str(error["loc"][0])
                for error in exc.errors()
                if error["type"] == "extra_forbidden"
            )
            if unknown:
                _reject(
                    f"matrix[{index}] has unsupported keys: {unknown}; "
                    "allowed keys are 'agent_type' and 'ai_model_id'"
                )
            first = exc.errors()[0]
            field = first["loc"][0] if first["loc"] else "entry"
            if field == "ai_model_id":
                _reject(f"matrix[{index}].ai_model_id must be a UUID")
            _reject(f"matrix[{index}].{field}: {first['msg']}")
        cell: Dict[str, Any] = {}
        if parsed.agent_type is not None:
            if parsed.agent_type.lower() not in SUPPORTED_AGENT_TYPES:
                _reject(
                    f"matrix[{index}].agent_type '{parsed.agent_type}' is not "
                    f"supported; supported types: {sorted(SUPPORTED_AGENT_TYPES)}"
                )
            cell["agent_type"] = parsed.agent_type.lower()
        if parsed.ai_model_id is not None:
            ai_model = crud_ai_model.get(db, id=str(parsed.ai_model_id))
            # Same visibility rule as model listing: account-owned or shared
            # system model (account_id is NULL).
            if not ai_model or ai_model.account_id not in (None, account_id):
                _reject(f"matrix[{index}].ai_model_id '{parsed.ai_model_id}' not found")
            cell["ai_model_id"] = str(parsed.ai_model_id)
        validated.append(cell)
    return validated


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

    The body is free-form trigger event data, except for the reserved key
    ``matrix``: when present it must be a list of up to 25
    ``{"agent_type"?, "ai_model_id"?}`` objects and the trigger fans out to
    one execution per entry, all sharing a ``batch_id`` (an empty object runs
    the flow defaults). Without ``matrix`` the behaviour is unchanged.

    Args:
        flow_id: Flow to trigger
        trigger_event_data: Optional custom trigger event data for testing
            template variables, plus the optional reserved ``matrix`` key

    Returns:
        Execution details, or batch details (``batch_id`` + per-cell
        ``execution_id``) when a matrix was given
    """
    # Verify flow exists and user has access
    flow = crud_flow.get(db=db, id=flow_id, account_id=current_user.account_id)
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")

    _reject_reserved_trigger_keys(trigger_event_data)
    _reject_oversized_workspace_seeds(trigger_event_data)
    _reject_invalid_product_provenance(trigger_event_data)

    # Pop the reserved matrix key so it never leaks into template variables.
    matrix = None
    if trigger_event_data and "matrix" in trigger_event_data:
        matrix = trigger_event_data.pop("matrix")

    # Trigger flow execution
    from preloop.services.flow_trigger_service import FlowTriggerService

    trigger_service = FlowTriggerService(db)

    if matrix is not None:
        validated_matrix = _validate_matrix(
            db, matrix, account_id=current_user.account_id
        )
        try:
            return await trigger_service.trigger_flow_matrix(
                flow_id=flow_id,
                matrix=validated_matrix,
                test_mode=True,
                trigger_event_data=trigger_event_data,
                triggered_by=_display_name(current_user),
            )
        except TriageControllerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ModelRoutingError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FlowHaltActiveError:
            raise _halted_response()

    try:
        result = await trigger_service.trigger_flow(
            flow_id=flow_id,
            test_mode=True,
            trigger_event_data=trigger_event_data,
            triggered_by=_display_name(current_user),
        )
    except TriageControllerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ModelRoutingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FlowHaltActiveError:
        raise _halted_response()

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
    from preloop.models.models.flow_execution import ROUTING_RECORD_KEY
    from preloop.services.flow_trigger_service import FlowTriggerService

    trigger_data = dict(original.trigger_event_details or {})
    trigger_data.pop(ROUTING_RECORD_KEY, None)

    trigger_service = FlowTriggerService(db)
    try:
        result = await trigger_service.trigger_flow(
            flow_id=original.flow_id,
            test_mode=False,
            trigger_event_data=trigger_data,
            retry_of_execution_id=original.id,
            triggered_by=_display_name(current_user),
        )
    except TriageControllerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ModelRoutingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FlowHaltActiveError:
        raise _halted_response()

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
    # The same lifetime counts the list carries. The detail page shows ten
    # recent runs and has to be able to say how many there are in total; a
    # link that reads "View all executions" over 95 of them tells the operator
    # nothing about whether it is worth the click.
    stats = crud_flow_execution.get_execution_stats_for_flows(db, [flow.id])
    stat = stats[0] if stats else None
    flow.execution_stats = {
        "total_execs": int(getattr(stat, "total_execs", 0) or 0),
        "running_execs": int(getattr(stat, "running_execs", 0) or 0),
        "last_seen_at": (
            stat.last_seen_at.isoformat()
            if stat and getattr(stat, "last_seen_at", None)
            else None
        ),
        "estimated_cost": float(getattr(stat, "estimated_cost", 0.0) or 0.0),
        "token_usage": getattr(stat, "token_usage", None),
    }
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
    source_id = getattr(flow, "source_preset_id", None)
    if isinstance(source_id, uuid.UUID):
        source_preset = crud_flow.get(db=db, id=source_id)
        agent_config = (
            flow_in.agent_config
            if flow_in.agent_config is not None
            else flow.agent_config
        )
        _reject_unsupported_persistent_preset(agent_config, source_preset)

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

    # Schedule triggers must always keep a valid cron schedule_config
    effective_source = (
        flow_in.trigger_event_source
        if flow_in.trigger_event_source is not None
        else flow.trigger_event_source
    )
    # Reject a schedule_config sent for a flow whose (effective) trigger
    # source is not 'schedule' - it would be stored but never reconciled.
    if flow_in.schedule_config and effective_source != "schedule":
        raise HTTPException(
            status_code=400,
            detail="schedule_config is only valid when trigger_event_source "
            "is 'schedule'",
        )
    if effective_source == "schedule":
        effective_schedule = (
            flow_in.schedule_config
            if "schedule_config" in flow_in.model_fields_set
            else flow.schedule_config
        )
        if not effective_schedule:
            raise HTTPException(
                status_code=400,
                detail="schedule_config is required for schedule triggers",
            )
        if flow_in.trigger_event_source == "schedule":
            flow_in.trigger_event_types = ["schedule"]

    # Webhook triggers need a secret to build the trigger URL. The create
    # path auto-generates one, but flows switched to a webhook trigger
    # later (e.g. cloned presets, which start with no trigger) ended up
    # with webhook_config=None: the console never shows a webhook URL and
    # the flow is untriggerable. Mirror the create-path behavior here.
    if (
        effective_source == "webhook"
        and not flow.webhook_config
        and not flow_in.webhook_config
    ):
        flow_in.webhook_config = schemas.WebhookConfig(
            webhook_secret=secrets.token_urlsafe(32)
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

    if flow_in.agent_config is not None:
        try:
            validate_stored_model_routing(
                db, flow_in.agent_config, current_user.account_id
            )
        except ModelRoutingError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Only validate the allowlist when this write carries one: an update that
    # does not mention callable_flows leaves the stored list alone, and an
    # explicit null or [] revokes it (which always validates).
    if "callable_flows" in flow_in.model_fields_set:
        try:
            validate_callable_flows(
                db,
                callable_flows=flow_in.callable_flows,
                account_id=current_user.account_id,
                flow_id=flow.id,
                flow_name=flow_in.name or flow.name,
            )
        except CallableFlowsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    _reject_host_exec_flow(
        agent_type=(
            flow_in.agent_type if flow_in.agent_type is not None else flow.agent_type
        ),
        agent_config=(
            flow_in.agent_config
            if flow_in.agent_config is not None
            else flow.agent_config
        ),
        runner_pool=(
            flow_in.runner_pool
            if "runner_pool" in flow_in.model_fields_set
            else flow.runner_pool
        ),
        git_clone_config=(
            flow_in.git_clone_config
            if flow_in.git_clone_config is not None
            else flow.git_clone_config
        ),
        custom_commands=(
            flow_in.custom_commands
            if flow_in.custom_commands is not None
            else flow.custom_commands
        ),
    )

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

    # Deleting a flow cascades to its executions (and their logs). If an agent
    # is still running for one of those executions it would keep streaming
    # logs for a row that no longer exists (FK violations in the log
    # persister, orphaned containers). Require executions to be stopped first,
    # via the existing stop command endpoint, before the flow can be deleted.
    active_executions = crud_flow_execution.get_running_by_flow(
        db, flow_id=flow.id, account_id=current_user.account_id
    )
    if active_executions:
        logger.warning(
            f"Refusing to delete flow {flow_id}: "
            f"{len(active_executions)} active execution(s)"
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"Flow has {len(active_executions)} active execution(s). "
                "Stop the active executions first (use the execution's stop command "
                "endpoint) and retry the delete."
                'with {"command": "stop"}) and retry the delete.'
            ),
        )

    # A committed deletion detaches the ORM row. Resolve relationship-backed
    # response fields (such as ai_model_name) while its session is available.
    # Validate before mutating: a stored row that fails FlowResponse should
    # remain so operators can inspect it, rather than being deleted and then
    # returning HTTP 500 (a retry would 404 and hide the failed delete).
    response = schemas.FlowResponse.model_validate(flow)
    flow_name = flow.name
    crud_flow.remove(db=db, id=flow_id, account_id=current_user.account_id)

    log_config_change(
        db,
        user=current_user,
        config_type="flow",
        action="deleted",
        old_value={"id": str(flow_id), "name": flow_name},
    )

    logger.info(f"Successfully deleted flow {flow_id}")
    return response


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


@router.post(
    "/webhooks/flows/{flow_id}/{webhook_secret}",
    responses={
        200: {
            "description": (
                "Execution triggered (or deduplicated to an existing running "
                "execution for the same repo + commit, see 'deduplicated')."
            )
        },
        202: {
            "description": (
                "Execution row was created but dispatch failed; poll "
                "execution_url or retry from the console. Do not re-send "
                "the webhook."
            )
        },
        422: {
            "description": (
                "Payload does not match the flow's trigger_config; no "
                "execution was created. Also returned for invalid request "
                "input (e.g. malformed flow_id)."
            )
        },
        500: {
            "description": (
                "No execution could be created (failure at/before the insert)."
            )
        },
    },
)
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

    Success responses carry a nested ``execution`` object with the same
    ``{id, status, flow_id}`` shape as ``POST /flows/{flow_id}/trigger``, plus
    flat ``execution_id``/``execution_status``/``execution_url`` fields and
    ``"status": "triggered"`` for backwards compatibility.

    Deduplication: redelivered payloads carrying the same commit SHA as a
    still-running execution of this flow return that execution with
    ``"deduplicated": true`` instead of creating a duplicate. Commit-less
    payloads (e.g. GlitchTip alerts) are coalesced on a resource key derived
    from ``webhook_config.dedupe_path`` or the default paths
    ``attachments.0.title_link`` / ``data.issue.id``; payloads with neither
    identity are never deduplicated.
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
    import logging

    from fastapi.responses import JSONResponse

    from preloop.config import settings
    from preloop.services.flow_trigger_service import (
        FlowDispatchError,
        FlowTriggerService,
    )

    logger = logging.getLogger(__name__)
    trigger_service = FlowTriggerService(db)

    # Create event data from webhook payload
    event_data = {
        "source": "webhook",
        "type": "webhook",
        "payload": payload,
        "account_id": str(flow.account_id),
    }

    _reject_oversized_workspace_seeds(event_data)
    _reject_invalid_product_provenance(event_data)

    def _execution_url(execution_id: str) -> str:
        # Built from settings.preloop_url; self-hosted deployments where the
        # console lives on a different origin must set PRELOOP_URL (same
        # pattern as flow_orchestrator and agents/container).
        return f"{settings.preloop_url}/console/flows/executions/{execution_id}"

    def _response_body(
        execution_id: str,
        execution_status: str,
        *,
        status: str = "triggered",
        deduplicated: bool = False,
    ) -> Dict[str, Any]:
        # `execution` mirrors the /flows/{flow_id}/trigger response shape;
        # the flat execution_* fields and "status": "triggered" are kept for
        # backwards compatibility with existing webhook callers.
        return {
            "status": status,
            "flow_id": str(flow_id),
            "deduplicated": deduplicated,
            "execution": {
                "id": execution_id,
                "status": execution_status,
                "flow_id": str(flow_id),
            },
            "execution_id": execution_id,
            "execution_status": execution_status,
            "execution_url": _execution_url(execution_id),
        }

    # Enforce the addressed flow's trigger_config policy explicitly. Unlike
    # the old generic-matching path (process_event), a mismatch is an
    # explicit 422 rather than a silent skip that still reports success.
    if not trigger_service.matches_trigger_config(flow, event_data):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Webhook payload does not match trigger_config for flow "
                f"{flow_id}. Configured filter: {flow.trigger_config}. "
                "No execution was created. Adjust the payload or the flow's "
                "trigger_config."
            ),
        )

    # Preserve commit-SHA deduplication for direct triggers: a redelivered
    # webhook (at-least-once delivery, CI retries) for the same repo + commit
    # returns the EXISTING execution instead of creating a duplicate — and
    # never a bare skip.
    duplicate = trigger_service.find_duplicate_execution(flow, event_data)
    if duplicate is not None:
        logger.info(
            f"Webhook for flow {flow_id} deduplicated to existing execution "
            f"{duplicate.id} (status {duplicate.status})"
        )
        return _response_body(str(duplicate.id), duplicate.status, deduplicated=True)

    # Trigger this specific flow directly. The flow was already resolved and
    # validated above (id + secret + enabled + webhook source) and its
    # trigger_config checked, so we must not route through generic event
    # matching (process_event), which can silently skip the flow or swallow
    # errors while this endpoint still reports success.
    try:
        result = await trigger_service.trigger_flow(
            flow_id=flow_id,
            test_mode=False,
            trigger_event_data=event_data,
        )
    except ModelRoutingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FlowHaltActiveError:
        # The account kill switch halts new executions (#157): nothing was
        # created, so a clean 403 (not 202/500) tells webhook senders this
        # trigger was deliberately refused, not lost.
        raise _halted_response()
    except FlowDispatchError as e:
        # The execution row was committed before dispatch failed: report 202
        # with the execution id so callers can poll, and do NOT claim that no
        # execution was created (a blind retry would duplicate it — though
        # commit-SHA dedup above also guards redelivery).
        logger.error(
            f"Webhook trigger for flow {flow_id} created execution "
            f"{e.execution_id} but dispatch failed: {e.original}",
            exc_info=True,
        )
        body = _response_body(e.execution_id, e.execution_status, status="accepted")
        body["detail"] = (
            f"Execution {e.execution_id} was created but could not be "
            "dispatched yet. It remains queued; poll execution_url or retry "
            "it from the console. Do not re-send this webhook."
        )
        return JSONResponse(status_code=202, content=body)
    except Exception as e:
        logger.error(
            f"Webhook trigger failed to create execution for flow {flow_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "Webhook received but no execution could be created for "
                f"flow {flow_id}. Check server logs for details."
            ),
        )

    return _response_body(result["id"], result["status"])
