import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, ColumnElement
from sqlalchemy.orm import Session, joinedload, load_only, with_expression
from sqlalchemy.future import select

from preloop.models import models

from preloop.models.models.flow_execution import (
    TRIGGER_SUBJECT_KEY,
    FlowExecution,
)
from preloop.models.models.flow import Flow
from preloop.models.schemas.flow_execution import (
    FlowExecutionCreate,
    FlowExecutionUpdate,
)
from .base import CRUDBase


async def get_flow_execution(
    db: Session, flow_execution_id: uuid.UUID
) -> Optional[FlowExecution]:
    """
    Retrieve a flow execution by its ID.
    """
    result = await db.execute(
        select(FlowExecution).filter(FlowExecution.id == flow_execution_id)
    )
    return result.scalars().first()


async def get_flow_executions_by_flow(
    db: Session,
    flow_id: uuid.UUID,
    skip: int = 0,
    limit: int = 100,
    account_id: Optional[str] = None,
) -> List[FlowExecution]:
    """
    Retrieve flow executions for a specific flow.
    """
    query = (
        select(FlowExecution)
        .filter(FlowExecution.flow_id == flow_id)
        .order_by(FlowExecution.start_time.desc())
    )
    if account_id:
        query = query.join(Flow).filter(Flow.account_id == account_id)

    result = await db.execute(query.offset(skip).limit(limit))
    return result.scalars().all()


async def create_flow_execution(
    db: Session, flow_execution_in: FlowExecutionCreate
) -> FlowExecution:
    """
    Create a new flow execution.
    This is typically called by the Flow Trigger Service.
    """
    db_flow_execution = FlowExecution(**flow_execution_in.model_dump())
    db.add(db_flow_execution)
    await db.commit()
    await db.refresh(db_flow_execution)
    return db_flow_execution


async def update_flow_execution(
    db: Session, flow_execution: FlowExecution, flow_execution_in: FlowExecutionUpdate
) -> FlowExecution:
    """
    Update an existing flow execution.
    This is typically called by the Flow Execution Orchestrator to update status, logs, etc.
    """
    update_data = flow_execution_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(flow_execution, field, value)

    await db.commit()
    await db.refresh(flow_execution)
    return flow_execution


async def delete_flow_execution(
    db: Session, flow_execution_id: uuid.UUID
) -> Optional[FlowExecution]:
    """
    Delete a flow execution (primarily for cleanup or testing, not a standard operation).
    """
    db_flow_execution = await get_flow_execution(db, flow_execution_id)
    if db_flow_execution:
        await db.delete(db_flow_execution)
        await db.commit()
    return db_flow_execution


class CRUDFlowExecution(CRUDBase[FlowExecution]):
    """CRUD operations for FlowExecution model."""

    def __init__(self):
        """Initialize with the FlowExecution model."""
        super().__init__(model=FlowExecution)

    def get(
        self,
        db: Session,
        id: Any,
        *,
        account_id: Optional[str] = None,
        refresh: bool = False,
    ) -> Optional[FlowExecution]:
        """Get flow execution by ID.

        Overrides base get to properly filter by account_id through Flow relationship.
        Set refresh for monitors that must replace cached attributes with updates
        committed by another session, such as a runner WebSocket handler.
        """
        query = db.query(FlowExecution).filter(FlowExecution.id == id)
        if refresh:
            query = query.populate_existing()
        if account_id:
            query = query.join(Flow).filter(Flow.account_id == account_id)
        return query.first()

    def purge_workspace_snapshots(self, db: Session, *, cutoff: Any) -> int:
        """Release terminal legacy snapshots; active executions retain state."""
        from preloop.models import models

        count = (
            db.query(models.FlowExecution)
            .filter(
                models.FlowExecution.workspace_snapshot.isnot(None),
                models.FlowExecution.end_time < cutoff,
                models.FlowExecution.status.in_(
                    ["SUCCEEDED", "FAILED", "STOPPED", "CANCELLED", "TIMED_OUT"]
                ),
            )
            .update(
                {models.FlowExecution.workspace_snapshot: None},
                synchronize_session=False,
            )
        )
        if count:
            db.commit()
        return count

    def existing_ids(self, db: Session, ids: List[Any]) -> set:
        """Return the subset of ``ids`` that exist as flow execution rows.

        Used by the log persister to distinguish logs for a since-deleted
        execution (drop quietly) from real persistence failures. Ids that are
        not valid UUIDs cannot exist and are simply excluded from the result.

        Args:
            db: Database session.
            ids: Candidate execution ids (str or UUID).

        Returns:
            Set of canonical string forms of the ids that exist.
        """
        candidates: dict[uuid.UUID, str] = {}
        for raw_id in ids:
            try:
                candidates[uuid.UUID(str(raw_id))] = str(raw_id)
            except (ValueError, AttributeError, TypeError):
                continue
        if not candidates:
            return set()
        rows = (
            db.query(FlowExecution.id)
            .filter(FlowExecution.id.in_(list(candidates)))
            .all()
        )
        return {candidates[row[0]] for row in rows}

    def create(self, db: Session, obj_in: FlowExecutionCreate) -> FlowExecution:
        """Create a new flow execution (synchronous)."""
        db_obj = FlowExecution(**obj_in.model_dump())
        db.add(db_obj)
        db.flush()  # Use flush instead of commit to stay in transaction
        return db_obj

    def update(
        self, db: Session, db_obj: FlowExecution, obj_in: FlowExecutionUpdate
    ) -> FlowExecution:
        """Update an existing flow execution (synchronous)."""
        import logging

        logger = logging.getLogger(__name__)

        update_data = obj_in.model_dump(exclude_unset=True)

        # Debug logging for metrics updates
        if "tool_calls_count" in update_data or "total_tokens" in update_data:
            logger.debug(
                f"CRUD update - Setting metrics on FlowExecution {db_obj.id}: "
                f"tool_calls_count={update_data.get('tool_calls_count')}, "
                f"total_tokens={update_data.get('total_tokens')}, "
                f"estimated_cost={update_data.get('estimated_cost')}"
            )
            logger.debug(
                f"Current DB values before update: tool_calls_count={db_obj.tool_calls_count}, "
                f"total_tokens={db_obj.total_tokens}, estimated_cost={db_obj.estimated_cost}"
            )

        for field, value in update_data.items():
            setattr(db_obj, field, value)

        db.flush()  # Use flush instead of commit to stay in transaction

        # Debug logging after flush
        if "tool_calls_count" in update_data or "total_tokens" in update_data:
            logger.debug(
                f"After flush: tool_calls_count={db_obj.tool_calls_count}, "
                f"total_tokens={db_obj.total_tokens}, estimated_cost={db_obj.estimated_cost}"
            )

        return db_obj

    def set_evidence_archive(
        self, db: Session, *, db_obj: FlowExecution, archive: bytes
    ) -> FlowExecution:
        """Persist the captured evidence pack archive (tar.gz bytes).

        Separate from ``update`` because the archive is binary and must never
        travel through the FlowExecutionUpdate schema (which is serialized to
        NATS for UI updates).
        """
        db_obj.evidence_archive = archive  # type: ignore[assignment]
        db.flush()
        return db_obj

    def set_workspace_snapshot(
        self, db: Session, *, db_obj: FlowExecution, archive: Optional[bytes]
    ) -> FlowExecution:
        """Persist (or clear) the captured workspace snapshot (tar.gz bytes).

        Separate from ``update`` for the same reason as the evidence pack: the
        archive is binary and must never travel through the
        FlowExecutionUpdate schema that is serialized to NATS.
        """
        db_obj.workspace_snapshot = archive  # type: ignore[assignment]
        db.flush()
        return db_obj

    def set_cli_session(
        self, db: Session, *, db_obj: FlowExecution, cli_session: Optional[dict]
    ) -> FlowExecution:
        """Persist (or clear) the agent CLI session reference.

        Separate from ``update`` because the value is written from the log
        streaming task the moment the agent reports it (and from the terminal
        rescan fallback), outside any FlowExecutionUpdate round trip. Shape:
        ``{"agent_type": "opencode", "session_id": "ses_..."}``.
        """
        db_obj.cli_session = cli_session  # type: ignore[assignment]
        db.flush()
        return db_obj

    def record_native_resume(
        self, db: Session, *, execution_id: uuid.UUID, outcome: dict
    ) -> None:
        """Persist only the native resume outcome, never session content."""
        execution = self.get(db, id=execution_id)
        if execution is None:
            return
        execution.result = {
            **(execution.result or {}),
            "native_resume": {
                key: outcome[key]
                for key in ("mode", "reason", "session_id")
                if key in outcome
            },
        }
        db.commit()

    def get_by_flow(
        self,
        db: Session,
        flow_id: uuid.UUID,
        skip: int = 0,
        limit: int = 100,
        account_id: Optional[str] = None,
    ) -> List[FlowExecution]:
        """Get flow executions for a specific flow (synchronous)."""
        query = (
            db.query(FlowExecution)
            .filter(FlowExecution.flow_id == flow_id)
            .order_by(FlowExecution.start_time.desc())
        )
        if account_id:
            query = query.join(Flow).filter(Flow.account_id == account_id)
        return query.offset(skip).limit(limit).all()

    def get_by_result_pr_url(
        self,
        db: Session,
        flow_id: Any,
        pr_url: str,
    ) -> Optional[FlowExecution]:
        """Return the newest execution of this flow that recorded ``pr_url``.

        Matches ``FlowExecution.result['pr_url']`` exactly so resume does not
        depend on a recency window. Callers should pass a normalized URL.
        """
        if not pr_url:
            return None
        return (
            db.query(FlowExecution)
            .filter(
                FlowExecution.flow_id == flow_id,
                FlowExecution.result["pr_url"].astext == pr_url,
            )
            .order_by(FlowExecution.start_time.desc())
            .first()
        )

    def get_by_batch(
        self,
        db: Session,
        batch_id: uuid.UUID,
        account_id: Optional[str] = None,
    ) -> List[FlowExecution]:
        """Get all executions created by one matrix/batch trigger.

        Ordered by creation time as a stable default; note this does NOT
        guarantee matrix-cell order (ids are random UUIDs and created_at has
        limited resolution) — callers that need cell order must sort by the
        recorded matrix index, as the batch listing endpoint does. Batches are
        capped at trigger time, so no pagination is needed. The flow
        relationship is eagerly loaded because callers render flow names per
        row.
        """
        query = (
            db.query(FlowExecution)
            .options(joinedload(FlowExecution.flow))
            .filter(FlowExecution.batch_id == batch_id)
            .order_by(FlowExecution.created_at.asc(), FlowExecution.id.asc())
        )
        if account_id:
            query = query.join(Flow).filter(Flow.account_id == account_id)
        return query.all()

    def get_running_by_flow(
        self,
        db: Session,
        flow_id: uuid.UUID,
        account_id: Optional[uuid.UUID] = None,
        running_statuses: Optional[List[str]] = None,
    ) -> List[FlowExecution]:
        """Get running flow executions for a specific flow.

        Unlike get_by_flow, this specifically queries for executions in running states
        without a limit, ensuring long-running executions are not missed.

        Args:
            db: Database session
            flow_id: The flow ID to query
            account_id: Optional account ID to filter by
            running_statuses: List of statuses considered "running".
                             Defaults to ["PENDING", "INITIALIZING", "STARTING", "RUNNING"]

        Returns:
            List of flow executions in running states
        """
        if running_statuses is None:
            running_statuses = ["PENDING", "INITIALIZING", "STARTING", "RUNNING"]

        query = db.query(FlowExecution).filter(
            FlowExecution.flow_id == flow_id,
            FlowExecution.status.in_(running_statuses),
        )
        if account_id:
            query = query.join(Flow).filter(Flow.account_id == account_id)
        return query.all()

    def get_multi(
        self,
        db: Session,
        *,
        skip: int = 0,
        limit: int = 100,
        account_id: Optional[str] = None,
        flow_id: Optional[Any] = None,
        statuses: Optional[List[str]] = None,
        search: Optional[str] = None,
        started_after: Optional[datetime] = None,
        eager_load: bool = False,
        lightweight: bool = False,
        **filters,
    ) -> List[FlowExecution]:
        """Get multiple flow executions with optional filtering.

        Overrides base get_multi to properly filter by account_id through Flow relationship.

        Args:
            eager_load: If True, eagerly load the flow relationship to avoid N+1 queries.
            lightweight: If True, defer heavy text/JSON columns used only by detail views.
            search: Case-insensitive match on the flow name or the trigger subject.
            started_after: Only runs that started at or after this instant.
        """
        query = db.query(FlowExecution)

        if lightweight:
            query = query.options(
                load_only(
                    FlowExecution.id,
                    FlowExecution.flow_id,
                    FlowExecution.status,
                    FlowExecution.start_time,
                    FlowExecution.end_time,
                    FlowExecution.error_message,
                    # Small and the whole point of the list view for
                    # failures; omitting it here would make the schema
                    # projection lazy-load it one row at a time.
                    FlowExecution.failure_category,
                    FlowExecution.runner_id,
                    FlowExecution.agent_session_reference,
                    FlowExecution.retry_of_execution_id,
                    FlowExecution.batch_id,
                    FlowExecution.tool_calls_count,
                    FlowExecution.total_tokens,
                    FlowExecution.estimated_cost,
                    FlowExecution.created_at,
                    FlowExecution.updated_at,
                )
            )
            # Project the precomputed subject out of the trigger payload
            # instead of loading the (potentially very large) JSONB column.
            # Rows created before subjects existed simply yield NULL.
            subject = FlowExecution.trigger_event_details[TRIGGER_SUBJECT_KEY]
            query = query.options(
                with_expression(
                    FlowExecution.trigger_subject,
                    subject["text"].astext,
                ),
                with_expression(
                    FlowExecution.trigger_subject_url,
                    subject["url"].astext,
                ),
            )

        # Eagerly load flow relationship to avoid N+1 queries
        if eager_load:
            flow_loader = joinedload(FlowExecution.flow)
            if lightweight:
                flow_loader = flow_loader.load_only(Flow.id, Flow.name)
            query = query.options(flow_loader)

        query = self._apply_list_filters(
            query,
            account_id=account_id,
            flow_id=flow_id,
            statuses=statuses,
            search=search,
            started_after=started_after,
            filters=filters,
        )

        # Order by start_time descending (most recent first)
        query = query.order_by(FlowExecution.start_time.desc())

        return query.offset(skip).limit(limit).all()

    def count(
        self,
        db: Session,
        *,
        account_id: Optional[str] = None,
        flow_id: Optional[Any] = None,
        statuses: Optional[List[str]] = None,
        search: Optional[str] = None,
        started_after: Optional[datetime] = None,
        **filters,
    ) -> int:
        """How many executions match the filters, ignoring the page window.

        The console prints "25 of N executions" over a page of 25, and N has
        to be the number the filters actually matched, not the page size.
        """
        query = self._apply_list_filters(
            db.query(FlowExecution),
            account_id=account_id,
            flow_id=flow_id,
            statuses=statuses,
            search=search,
            started_after=started_after,
            filters=filters,
        )
        return query.count()

    def _apply_list_filters(
        self,
        query,
        *,
        account_id: Optional[str],
        flow_id: Optional[Any],
        statuses: Optional[List[str]],
        search: Optional[str] = None,
        started_after: Optional[datetime] = None,
        filters: Optional[Dict[str, Any]] = None,
    ):
        """The list filters, shared by the page query and its count."""
        # Filter by account_id through the Flow relationship. Search reads the
        # flow name, so it needs the same join even without an account.
        if account_id or search:
            query = query.join(Flow)
        if account_id:
            query = query.filter(Flow.account_id == account_id)

        if flow_id:
            query = query.filter(FlowExecution.flow_id == flow_id)

        if statuses:
            query = query.filter(FlowExecution.status.in_(statuses))

        if started_after is not None:
            query = query.filter(FlowExecution.start_time >= started_after)

        if search:
            # Both halves of what the row shows: the flow it belongs to and
            # the subject that tells one run of that flow from the next.
            pattern = f"%{search.strip()}%"
            subject = FlowExecution.trigger_event_details[TRIGGER_SUBJECT_KEY]
            query = query.filter(
                or_(
                    Flow.name.ilike(pattern),
                    subject["text"].astext.ilike(pattern),
                )
            )

        # Apply any additional filters
        for key, value in (filters or {}).items():
            if hasattr(FlowExecution, key):
                query = query.filter(getattr(FlowExecution, key) == value)

        return query

    def get_by_statuses(
        self, db: Session, statuses: List[str], account_id: Optional[str] = None
    ) -> List[FlowExecution]:
        """Get flow executions filtered by status list."""
        query = db.query(FlowExecution).filter(FlowExecution.status.in_(statuses))
        if account_id:
            query = query.join(Flow).filter(Flow.account_id == account_id)
        return query.all()

    def get_execution_stats_for_flows(
        self, db: Session, flow_ids: List[Any], start_date: Optional[datetime] = None
    ) -> List[Any]:
        """Get execution statistics for a list of flow IDs.

        Args:
            db: Database session.
            flow_ids: Flows to aggregate.
            start_date: When given, each row also carries the counts for that
                window (``runs``, ``failed``, ``cost``, ``last_run_at``,
                ``since``). The flows list states one period in its header
                ("in the last 30d") and used to fill it from two sources: runs
                counted client-side from a sample of the 200 most recent
                executions, spend from a per-range usage endpoint. A flow
                whose runs fell outside the sample then read "No run in the
                last 30d" beside a real spend. These fields answer runs,
                failures and spend for the same window, from the database.
                The all-time fields keep their meaning either way, because
                other callers (the agents view) show lifetime totals.

        Returns:
            One row per flow that has ever executed.
        """
        if not flow_ids:
            return []

        from sqlalchemy import func, case
        from preloop.models.crud.api_usage import exclude_replay_usage_condition
        from preloop.models.models.api_usage import ApiUsage
        from preloop.services.flow_failure_category import FAILURE_STATUSES

        # Fetch execution stats
        exec_stats = (
            db.query(
                self.model.flow_id,
                func.count(self.model.id).label("total_execs"),
                func.sum(
                    case(
                        (
                            self.model.status.in_(
                                ["PENDING", "INITIALIZING", "STARTING", "RUNNING"]
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("running_execs"),
                func.max(self.model.updated_at).label("last_seen_at"),
            )
            .filter(self.model.flow_id.in_(flow_ids))
            .group_by(self.model.flow_id)
            .all()
        )

        # Fetch cost stats from actual API usage
        cost_stats = (
            db.query(
                ApiUsage.flow_id,
                func.coalesce(func.sum(ApiUsage.estimated_cost), 0.0).label(
                    "estimated_cost"
                ),
            )
            .filter(
                ApiUsage.flow_id.in_(flow_ids),
                ApiUsage.action_type == "model_gateway",
                # Replay-validation traffic is not the flow's spend. The
                # Overview usage summary and the per-execution aggregation
                # both exclude it, so this had to as well or the same window
                # would read differently in two places.
                exclude_replay_usage_condition(),
            )
            .group_by(ApiUsage.flow_id)
            .all()
        )

        cost_map = {str(row.flow_id): row.estimated_cost for row in cost_stats}

        window_map: Dict[str, Dict[str, Any]] = {}
        if start_date is not None:
            window_stats = (
                db.query(
                    self.model.flow_id,
                    func.count(self.model.id).label("runs"),
                    func.sum(
                        case(
                            (self.model.status.in_(FAILURE_STATUSES), 1),
                            else_=0,
                        )
                    ).label("failed"),
                    func.max(self.model.start_time).label("last_run_at"),
                )
                .filter(
                    self.model.flow_id.in_(flow_ids),
                    self.model.start_time >= start_date,
                )
                .group_by(self.model.flow_id)
                .all()
            )
            # Spend belongs to the run, so the window is the run's
            # start_time, not the usage row's timestamp. A long run, a
            # delayed gateway write, or a backdated start_time would
            # otherwise print cost for a period the runs count does not.
            window_cost = (
                db.query(
                    self.model.flow_id,
                    func.coalesce(func.sum(ApiUsage.estimated_cost), 0.0).label(
                        "estimated_cost"
                    ),
                )
                .join(self.model, ApiUsage.flow_execution_id == self.model.id)
                .filter(
                    self.model.flow_id.in_(flow_ids),
                    self.model.start_time >= start_date,
                    ApiUsage.action_type == "model_gateway",
                    exclude_replay_usage_condition(),
                )
                .group_by(self.model.flow_id)
                .all()
            )
            window_cost_map = {
                str(row.flow_id): float(row.estimated_cost or 0.0)
                for row in window_cost
            }
            for row in window_stats:
                window_map[str(row.flow_id)] = {
                    "runs": int(row.runs or 0),
                    "failed": int(row.failed or 0),
                    "last_run_at": row.last_run_at,
                    "cost": window_cost_map.get(str(row.flow_id), 0.0),
                }

        class FlowStatResponse:
            def __init__(self, row):
                self.flow_id = row.flow_id
                self.total_execs = row.total_execs
                self.running_execs = row.running_execs
                self.last_seen_at = row.last_seen_at
                self.estimated_cost = cost_map.get(str(row.flow_id), 0.0)
                self.since = start_date
                window = window_map.get(str(row.flow_id))
                # A flow with no run in the window is a real answer (0 runs,
                # 0 failed, no spend), not a missing one.
                self.runs = window["runs"] if window else 0
                self.failed = window["failed"] if window else 0
                self.last_run_at = window["last_run_at"] if window else None
                self.cost = window["cost"] if window else 0.0

        return [FlowStatResponse(row) for row in exec_stats]

    def append_log(
        self, db: Session, execution_id: str, log_data: dict, *, commit: bool = True
    ) -> None:
        """Append a log entry to the flow_execution_log table.

        Uses a simple INSERT instead of rewriting the JSONB execution_logs
        column, avoiding O(n) write amplification per append.

        Args:
            db: Database session
            execution_id: ID of the flow execution
            log_data: Log message data to append
            commit: If True (default), commit after the insert. Set to
                False when batching many entries and commit manually
                after the loop.
        """
        from preloop.models.models.flow_execution_log import FlowExecutionLog
        from preloop.utils.secret_scrubbing import scrub_secrets, scrub_structure

        # NATS messages nest actual content under "payload" (e.g. payload.line
        # for agent_log_line).  Derive message from the best available field
        # and persist the full payload as metadata so nothing is lost.
        payload = log_data.get("payload") or {}
        message = (
            log_data.get("message") or payload.get("line") or payload.get("message")
        )
        metadata = payload or log_data.get("metadata") or log_data.get("data")

        # Last gate before persistence: redact known credential formats so a
        # secret cannot be stored even if its producer skipped scrubbing
        # (issue #173).
        log_entry = FlowExecutionLog(
            execution_id=execution_id,
            log_type=log_data.get("type", "log"),
            message=scrub_secrets(message),
            metadata_=scrub_structure(metadata) if metadata else None,
        )
        db.add(log_entry)
        if commit:
            db.commit()

    ACTIVE_ORCHESTRATOR_STATUSES = (
        "PENDING",
        "INITIALIZING",
        "STARTING",
        "RUNNING",
    )

    def request_account_stop(
        self,
        db: Session,
        *,
        account_id: Any,
        now: datetime,
        reason: Optional[str],
    ) -> int:
        """Persist stop intent for every admitted runtime under the account lock."""
        return (
            db.query(models.FlowExecution)
            .filter(
                models.FlowExecution.flow_id.in_(
                    db.query(models.Flow.id).filter(
                        models.Flow.account_id == account_id
                    )
                ),
                models.FlowExecution.status.in_(self.ACTIVE_ORCHESTRATOR_STATUSES),
                or_(
                    models.FlowExecution.status != "PENDING",
                    models.FlowExecution.agent_session_reference.isnot(None),
                    models.FlowExecution.orchestrator_worker_id.isnot(None),
                ),
                models.FlowExecution.stop_requested_at.is_(None),
            )
            .update(
                {
                    models.FlowExecution.stop_requested_at: now,
                    models.FlowExecution.stop_reason: reason,
                    models.FlowExecution.stop_source: "account_halt",
                },
                synchronize_session=False,
            )
        )

    def admit_runtime_start(
        self,
        db: Session,
        *,
        execution_id: Any,
        commit: bool = True,
    ) -> bool:
        """Serialize launch admission against halt activation and queued leasing.

        A launch admitted first is included in activation's stop snapshot. A
        launch arriving after activation never dispatches. No lock spans I/O.
        """
        from .account_halt import crud_account_halt

        account_id = (
            db.query(models.Flow.account_id)
            .join(
                models.FlowExecution,
                models.FlowExecution.flow_id == models.Flow.id,
            )
            .filter(models.FlowExecution.id == execution_id)
            .scalar()
        )
        if account_id is None:
            raise ValueError("Execution flow not found")
        crud_account_halt.lock_account(db, account_id=account_id)
        execution = self.get(db, id=execution_id, refresh=True)
        allowed = (
            execution is not None
            and execution.stop_requested_at is None
            and (
                "flows"
                not in crud_account_halt.active_scopes(db, account_id=account_id)
            )
        )
        if allowed:
            from datetime import timezone

            execution.launch_requested_at = (
                execution.launch_requested_at or datetime.now(timezone.utc)
            )
            execution.status = "STARTING"
            db.flush()
        if commit:
            db.commit()
        return allowed

    def cancel_unstarted_stop(self, db: Session, *, execution_id: Any) -> bool:
        """Complete a durable stop when no runtime was ever dispatched."""
        from datetime import timezone

        count = (
            db.query(models.FlowExecution)
            .filter(
                models.FlowExecution.id == execution_id,
                models.FlowExecution.agent_session_reference.is_(None),
                models.FlowExecution.launch_requested_at.is_(None),
                models.FlowExecution.stop_requested_at.isnot(None),
            )
            .update(
                {
                    models.FlowExecution.status: "STOPPED",
                    models.FlowExecution.stop_confirmed_at: datetime.now(timezone.utc),
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return bool(count)

    def get_stop_request(self, db: Session, *, execution_id: Any) -> Optional[dict]:
        """Read intent fresh on each monitor poll, independent of halt caching."""
        row = (
            db.query(
                models.FlowExecution.stop_requested_at,
                models.FlowExecution.stop_reason,
                models.FlowExecution.stop_confirmed_at,
            )
            .filter(models.FlowExecution.id == execution_id)
            .first()
        )
        if row is None or row.stop_requested_at is None:
            return None
        return {
            "requested_at": row.stop_requested_at,
            "reason": row.stop_reason,
            "confirmed_at": row.stop_confirmed_at,
        }

    def confirm_stop(
        self, db: Session, *, execution_id: Any, commit: bool = True
    ) -> None:
        """Record confirmed terminal runtime evidence, never an optimistic request."""
        from datetime import timezone

        db.query(models.FlowExecution).filter(
            models.FlowExecution.id == execution_id,
            models.FlowExecution.stop_requested_at.isnot(None),
            models.FlowExecution.stop_confirmed_at.is_(None),
        ).update(
            {models.FlowExecution.stop_confirmed_at: datetime.now(timezone.utc)},
            synchronize_session=False,
        )
        if commit:
            db.commit()

    def request_runner_stop(self, db: Session, *, execution_id: Any) -> None:
        """Signal only this execution's runner, or confirm cancellation before lease."""
        from .account_halt import crud_account_halt

        account_id = (
            db.query(models.Flow.account_id)
            .join(
                models.FlowExecution,
                models.FlowExecution.flow_id == models.Flow.id,
            )
            .filter(models.FlowExecution.id == execution_id)
            .scalar()
        )
        if account_id is None:
            raise ValueError("Execution flow not found")
        crud_account_halt.lock_account(db, account_id=account_id)
        execution = self.get(db, id=execution_id, refresh=True)
        runner = (
            db.query(models.FlowRunner)
            .filter(
                models.FlowRunner.current_execution_id == execution_id,
            )
            .with_for_update()
            .first()
        )
        if runner is not None:
            runner.halt_requested = True
        elif str(execution.agent_session_reference or "").startswith("runner:queued:"):
            execution.status = "STOPPED"
            self.confirm_stop(db, execution_id=execution_id, commit=False)
        db.commit()

    @staticmethod
    def _recovery_eligible() -> ColumnElement[bool]:
        """Keep monitoring pending stops during halt; exclude unstarted halt churn."""
        from sqlalchemy import exists

        halted = exists().where(
            models.AccountHalt.account_id == models.Flow.account_id,
            models.AccountHalt.scope == "flows",
            models.AccountHalt.is_active,
            models.Flow.id == models.FlowExecution.flow_id,
        )
        return or_(
            models.FlowExecution.agent_session_reference.isnot(None),
            models.FlowExecution.stop_requested_at.isnot(None),
            ~halted,
        )

    @classmethod
    def _active_or_unconfirmed_stop(cls) -> ColumnElement[bool]:
        from sqlalchemy import and_

        return or_(
            models.FlowExecution.status.in_(cls.ACTIVE_ORCHESTRATOR_STATUSES),
            and_(
                models.FlowExecution.stop_requested_at.isnot(None),
                models.FlowExecution.stop_confirmed_at.is_(None),
                models.FlowExecution.agent_session_reference.isnot(None),
            ),
        )

    def claim_execution(
        self,
        db: Session,
        *,
        execution_id: Any,
        worker_id: str,
        stale_after_seconds: int = 120,
    ) -> Optional[FlowExecution]:
        """Atomically claim an active execution for a worker.

        Uses ``FOR UPDATE SKIP LOCKED`` so concurrent workers never double-claim.
        An execution is claimable when unclaimed, claimed by this worker, or the
        previous claim heartbeat is older than ``stale_after_seconds``.

        Args:
            db: Database session.
            execution_id: Flow execution id.
            worker_id: Stable id for the claiming worker (pod name / hostname).
            stale_after_seconds: Seconds after last heartbeat before a claim is
                considered abandoned.

        Returns:
            The claimed execution row, or ``None`` if another worker holds a
            fresh claim or the execution is not claimable.
        """
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import or_

        now = datetime.now(timezone.utc)
        stale_before = now - timedelta(seconds=max(1, stale_after_seconds))

        row = (
            db.query(FlowExecution)
            .filter(
                FlowExecution.id == execution_id,
                self._active_or_unconfirmed_stop(),
                self._recovery_eligible(),
                or_(
                    FlowExecution.orchestrator_worker_id.is_(None),
                    FlowExecution.orchestrator_worker_id == worker_id,
                    FlowExecution.orchestrator_heartbeat_at.is_(None),
                    FlowExecution.orchestrator_heartbeat_at < stale_before,
                ),
            )
            .with_for_update(skip_locked=True)
            .first()
        )
        if row is None:
            return None

        row.orchestrator_worker_id = worker_id
        row.orchestrator_claimed_at = now
        row.orchestrator_heartbeat_at = now
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    def touch_heartbeat(
        self,
        db: Session,
        *,
        execution_id: Any,
        worker_id: str,
    ) -> bool:
        """Refresh the claim heartbeat for the owning worker.

        Returns:
            True if the heartbeat was updated for this worker.
        """
        from datetime import datetime, timezone

        row = (
            db.query(FlowExecution)
            .filter(
                FlowExecution.id == execution_id,
                FlowExecution.orchestrator_worker_id == worker_id,
            )
            .with_for_update()
            .first()
        )
        if row is None:
            return False
        row.orchestrator_heartbeat_at = datetime.now(timezone.utc)
        db.add(row)
        db.commit()
        return True

    def release_claim(
        self,
        db: Session,
        *,
        execution_id: Any,
        worker_id: Optional[str] = None,
    ) -> bool:
        """Clear orchestrator claim fields after terminal status or abort.

        Args:
            db: Database session.
            execution_id: Flow execution id.
            worker_id: When set, only release if this worker still owns the claim.

        Returns:
            True if a claim was cleared.
        """
        query = db.query(FlowExecution).filter(FlowExecution.id == execution_id)
        if worker_id is not None:
            query = query.filter(FlowExecution.orchestrator_worker_id == worker_id)
        row = query.with_for_update().first()
        if row is None:
            return False
        row.orchestrator_worker_id = None
        row.orchestrator_claimed_at = None
        row.orchestrator_heartbeat_at = None
        db.add(row)
        db.commit()
        return True

    def list_stale_or_unclaimed_active(
        self,
        db: Session,
        *,
        stale_after_seconds: int = 120,
        limit: int = 200,
    ) -> List[FlowExecution]:
        """List active executions that need dispatch/resume (unclaimed or stale)."""
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import or_

        now = datetime.now(timezone.utc)
        stale_before = now - timedelta(seconds=max(1, stale_after_seconds))
        return (
            db.query(FlowExecution)
            .options(joinedload(FlowExecution.flow))
            .filter(
                self._active_or_unconfirmed_stop(),
                self._recovery_eligible(),
                or_(
                    FlowExecution.orchestrator_worker_id.is_(None),
                    FlowExecution.orchestrator_heartbeat_at.is_(None),
                    FlowExecution.orchestrator_heartbeat_at < stale_before,
                ),
            )
            .order_by(FlowExecution.start_time.asc())
            .limit(limit)
            .all()
        )

    def list_claimed_by_worker(
        self,
        db: Session,
        *,
        worker_id: str,
        active_only: bool = True,
    ) -> List[FlowExecution]:
        """List executions currently claimed by ``worker_id``."""
        query = db.query(FlowExecution).filter(
            FlowExecution.orchestrator_worker_id == worker_id
        )
        if active_only:
            query = query.filter(
                FlowExecution.status.in_(self.ACTIVE_ORCHESTRATOR_STATUSES)
            )
        return query.order_by(FlowExecution.start_time.asc()).all()
