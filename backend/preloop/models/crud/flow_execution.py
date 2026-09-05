import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session, joinedload, load_only, with_expression
from sqlalchemy.future import select

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
        self, db: Session, id: Any, *, account_id: Optional[str] = None
    ) -> Optional[FlowExecution]:
        """Get flow execution by ID.

        Overrides base get to properly filter by account_id through Flow relationship.
        """
        query = db.query(FlowExecution).filter(FlowExecution.id == id)
        if account_id:
            query = query.join(Flow).filter(Flow.account_id == account_id)
        return query.first()

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
        eager_load: bool = False,
        lightweight: bool = False,
        **filters,
    ) -> List[FlowExecution]:
        """Get multiple flow executions with optional filtering.

        Overrides base get_multi to properly filter by account_id through Flow relationship.

        Args:
            eager_load: If True, eagerly load the flow relationship to avoid N+1 queries.
            lightweight: If True, defer heavy text/JSON columns used only by detail views.
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

        # Filter by account_id through the Flow relationship
        if account_id:
            query = query.join(Flow).filter(Flow.account_id == account_id)

        if flow_id:
            query = query.filter(FlowExecution.flow_id == flow_id)

        if statuses:
            query = query.filter(FlowExecution.status.in_(statuses))

        # Apply any additional filters
        for key, value in filters.items():
            if hasattr(FlowExecution, key):
                query = query.filter(getattr(FlowExecution, key) == value)

        # Order by start_time descending (most recent first)
        query = query.order_by(FlowExecution.start_time.desc())

        return query.offset(skip).limit(limit).all()

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
                    func.sum(case((self.model.status == "FAILED", 1), else_=0)).label(
                        "failed"
                    ),
                    func.max(self.model.start_time).label("last_run_at"),
                )
                .filter(
                    self.model.flow_id.in_(flow_ids),
                    self.model.start_time >= start_date,
                )
                .group_by(self.model.flow_id)
                .all()
            )
            window_cost = (
                db.query(
                    ApiUsage.flow_id,
                    func.coalesce(func.sum(ApiUsage.estimated_cost), 0.0).label(
                        "estimated_cost"
                    ),
                )
                .filter(
                    ApiUsage.flow_id.in_(flow_ids),
                    ApiUsage.action_type == "model_gateway",
                    ApiUsage.timestamp >= start_date,
                    exclude_replay_usage_condition(),
                )
                .group_by(ApiUsage.flow_id)
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
                FlowExecution.status.in_(self.ACTIVE_ORCHESTRATOR_STATUSES),
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
                FlowExecution.status.in_(self.ACTIVE_ORCHESTRATOR_STATUSES),
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
