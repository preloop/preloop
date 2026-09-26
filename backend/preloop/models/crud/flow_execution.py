import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional

from sqlalchemy import ColumnElement, and_, func, or_
from sqlalchemy.orm import Session, joinedload, load_only, with_expression
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.future import select

from preloop.models import models

from preloop.models.models.flow_execution import (
    AGENT_CONTROL_BINDING_KEY,
    DELEGATION_DETAILS_KEY,
    STOP_COVERAGE_KEY,
    TRIGGER_SUBJECT_KEY,
    FlowExecution,
)
from preloop.models.models.flow import Flow
from preloop.schemas.gateway_usage import GatewayTokenUsage
from preloop.models.schemas.flow_execution import (
    FlowExecutionCreate,
    FlowExecutionUpdate,
)
from .base import CRUDBase

logger = logging.getLogger(__name__)


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


# Worst-case rows loaded for one-object coalescing. The JSONB match should
# return 0 or 1; this only caps a malformed key or a filter miss.
TRACKER_OBJECT_LOOKUP_LIMIT = 16


def tracker_object_payload_match(object_key: str) -> Optional[ColumnElement[bool]]:
    """SQL filter that narrows trigger payloads to one tracker object.

    The Python extractor remains the source of truth. This only avoids
    loading every active JSONB blob; an over-inclusive match is fine.
    """
    parts = object_key.split(":")
    if len(parts) < 4:
        return None
    source = parts[0].lower()
    ident = parts[-1]
    kind = parts[-2]
    repo = ":".join(parts[1:-2])
    if not source or not kind or not ident or not repo:
        return None

    details = FlowExecution.trigger_event_details
    payload = details["payload"]
    source_col = details["source"].astext

    if source == "github" and kind == "pr":
        return and_(
            source_col == "github",
            payload["repository"]["full_name"].astext == repo,
            payload["pull_request"]["number"].astext == ident,
        )
    if source == "github" and kind == "issue":
        return and_(
            source_col == "github",
            payload["repository"]["full_name"].astext == repo,
            payload["issue"]["number"].astext == ident,
        )
    if source == "gitlab":
        return and_(
            source_col == "gitlab",
            payload["project"]["path_with_namespace"].astext == repo,
            payload["object_kind"].astext == kind,
            payload["object_attributes"]["iid"].astext == ident,
        )
    return None


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
        """Release terminal and orphaned snapshots; recent active runs retain state."""
        from preloop.models import models

        count = (
            db.query(models.FlowExecution)
            .filter(
                models.FlowExecution.workspace_snapshot.isnot(None),
                or_(
                    and_(
                        models.FlowExecution.end_time < cutoff,
                        models.FlowExecution.status.in_(
                            [
                                "SUCCEEDED",
                                "FAILED",
                                "STOPPED",
                                "CANCELLED",
                                "TIMED_OUT",
                            ]
                        ),
                    ),
                    and_(
                        models.FlowExecution.end_time.is_(None),
                        models.FlowExecution.start_time < cutoff,
                    ),
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
        update_data = obj_in.model_dump(exclude_unset=True)
        if "result" in update_data:
            # Result writers may hold an old ORM snapshot while the runner
            # advances publication. Lock/read the current protected state and
            # preserve it in the same transaction as this terminal update.
            with db.no_autoflush:
                stored = (
                    db.query(FlowExecution.result)
                    .filter(FlowExecution.id == db_obj.id)
                    .with_for_update()
                    .first()
                )
            existing = stored[0] if stored is not None else None
            incoming = update_data["result"]
            if isinstance(incoming, dict):
                incoming = dict(incoming)
                incoming.pop("_private_publication", None)
            state = (
                existing.get("_private_publication")
                if isinstance(existing, dict)
                else None
            )
            if isinstance(state, dict):
                incoming = dict(incoming) if isinstance(incoming, dict) else {}
                incoming["_private_publication"] = state
                incoming.pop("trusted_publication", None)
                if (
                    state.get("phase") == "complete"
                    and isinstance(state.get("receipt"), dict)
                    and existing.get("trusted_publication") == state["receipt"]
                ):
                    incoming["trusted_publication"] = state["receipt"]
            if isinstance(existing, dict):
                for key in (
                    "pr_url",
                    "pr_source_branch",
                    "native_resume",
                    "continuation",
                    "pending_followup",
                    "pending_followup_comment_url",
                    "resume_count",
                ):
                    if key in existing:
                        incoming = dict(incoming) if isinstance(incoming, dict) else {}
                        incoming[key] = existing[key]
            update_data["result"] = incoming

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

        # Orchestrator, stop, and cancel finish a resume child through this
        # path rather than apply_runner_completion. Close the parked parent
        # so it does not stay RESUMING after the child is terminal.
        terminal_status = update_data.get("status")
        if terminal_status in self.PARK_PARENT_CLOSE_STATUSES:
            self.close_parked_parent_for_resume(
                db,
                resume_execution_id=db_obj.id,
                status=terminal_status,
                end_time=getattr(db_obj, "end_time", None),
                commit=False,
            )

        # Debug logging after flush
        if "tool_calls_count" in update_data or "total_tokens" in update_data:
            logger.debug(
                f"After flush: tool_calls_count={db_obj.tool_calls_count}, "
                f"total_tokens={db_obj.total_tokens}, estimated_cost={db_obj.estimated_cost}"
            )

        return db_obj

    def bind_agent_control_command(
        self,
        db: Session,
        *,
        execution_id: Any,
        command_id: str,
        managed_agent_id: Any,
        runtime_session_id: Any = None,
        history_session_id: Any = None,
        session_reference: Optional[str] = None,
        commit: bool = True,
    ) -> Optional[FlowExecution]:
        """Bind a persistent Agent Control command to one flow execution.

        Stores the command id under ``AGENT_CONTROL_BINDING_KEY`` in
        ``trigger_event_details`` and sets ``agent_session_reference`` to
        ``control:{managed_agent_id}:{command_id}`` so the stop path can
        recover both ids without a migration.

        Args:
            db: Database session.
            execution_id: Flow execution to update.
            command_id: Persisted Agent Control command id.
            managed_agent_id: Target managed agent.
            runtime_session_id: Live control session at dispatch, if any.
            history_session_id: Tracking session minted for a new session.
            session_reference: Override for ``agent_session_reference``.
            commit: Whether to commit.

        Returns:
            The updated execution, or None when it does not exist.
        """
        execution = self.get(db, id=execution_id)
        if execution is None:
            return None
        details = dict(execution.trigger_event_details or {})
        details[AGENT_CONTROL_BINDING_KEY] = {
            "command_id": command_id,
            "managed_agent_id": str(managed_agent_id),
            "runtime_session_id": (
                str(runtime_session_id) if runtime_session_id else None
            ),
            "history_session_id": (
                str(history_session_id) if history_session_id else None
            ),
        }
        execution.trigger_event_details = details
        flag_modified(execution, "trigger_event_details")
        execution.agent_session_reference = session_reference or (
            f"control:{managed_agent_id}:{command_id}"
        )
        db.add(execution)
        if commit:
            db.commit()
            db.refresh(execution)
        else:
            db.flush()
        return execution

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

    OPEN_ARTIFACT_PUT_STATUSES = (
        "PENDING",
        "INITIALIZING",
        "RUNNING",
    )

    def set_evidence_receipt(
        self, db: Session, *, db_obj: FlowExecution, receipt: dict[str, Any]
    ) -> FlowExecution:
        """Persist the evidence availability receipt without NATS binary payload.

        The receipt is metadata only (digest, size, expiry, status). It must
        not carry archive bytes or transport credentials.
        """
        db_obj.evidence_receipt = receipt  # type: ignore[assignment]
        db.flush()
        return db_obj

    def lock_for_artifact_put(
        self,
        db: Session,
        *,
        execution_id: uuid.UUID,
        require_open: bool = True,
    ) -> FlowExecution:
        """Lock the execution and refresh identity before authorizing a PUT.

        ``populate_existing`` replaces a stale identity-map status so a
        completion committed on another session is visible. External uploads
        pass ``require_open=True``; controller retention after terminal
        failure passes False.
        """
        execution = (
            db.query(FlowExecution)
            .filter(FlowExecution.id == execution_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
        if execution is None:
            raise ValueError("artifact_execution_missing")
        if require_open and execution.status not in self.OPEN_ARTIFACT_PUT_STATUSES:
            raise ValueError("artifact_execution_closed")
        return execution

    def apply_runner_completion(
        self,
        db: Session,
        *,
        db_obj: FlowExecution,
        status: str,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> FlowExecution:
        """Persist terminal runner status and sanitized result via CRUD.

        Protected publication keys are merged from the locked current row
        inside ``update``; this method does not refresh the whole identity.
        """
        if status in {"SUCCEEDED", "FAILED", "STOPPED"}:
            self.confirm_stop(db, execution_id=db_obj.id, commit=False)
        payload: dict[str, Any] = {
            "status": status,
            "end_time": datetime.now(timezone.utc),
        }
        if error:
            payload["error_message"] = error
        if result is not None:
            payload["result"] = result
        updated = self.update(db, db_obj=db_obj, obj_in=FlowExecutionUpdate(**payload))
        # A parked parent stays RESUMING until this child finishes; copy the
        # terminal status so the console does not keep a blue "Resuming" chip.
        # Prefer the payload timestamp when update() did not apply end_time.
        self.close_parked_parent_for_resume(
            db,
            resume_execution_id=updated.id,
            status=status,
            end_time=getattr(updated, "end_time", None) or payload["end_time"],
            commit=False,
        )
        return updated

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

    def capture_publication_recovery(
        self,
        db: Session,
        *,
        db_obj: FlowExecution,
        archive: bytes | None,
        workspace: bytes | None,
    ) -> FlowExecution:
        """Commit recovery bytes before a publisher destroys the owned agent runtime."""
        if archive is None and workspace is None:
            raise ValueError("Publication recovery artifacts are missing")
        try:
            if archive is not None:
                self.set_evidence_archive(db, db_obj=db_obj, archive=archive)
            if workspace is not None:
                self.set_workspace_snapshot(db, db_obj=db_obj, archive=workspace)
            db.commit()
        except Exception:
            db.rollback()
            raise
        return db_obj

    def lock_for_runner_completion(
        self, db: Session, *, execution_id: uuid.UUID, account_id: uuid.UUID
    ) -> Optional[FlowExecution]:
        """Refresh and lock one owned execution without committing its transaction."""
        with db.no_autoflush:
            return (
                db.query(FlowExecution)
                .join(Flow, Flow.id == FlowExecution.flow_id)
                .filter(FlowExecution.id == execution_id, Flow.account_id == account_id)
                .populate_existing()
                .with_for_update(of=FlowExecution)
                .one_or_none()
            )

    def bind_publication(
        self,
        db: Session,
        *,
        execution_id: uuid.UUID,
        pr_url: str,
        source_branch: Optional[str] = None,
        commit: bool = True,
    ) -> Optional[FlowExecution]:
        """Merge one publication under a row lock; never replace a different PR.

        The caller resolves ownership before calling this internal write. The
        lock protects concurrent result writers and refreshes stale ORM state.
        An identical URL (and branch, when supplied) is a no-op. Explicit
        adoption can defer commit to its thread-registration boundary.
        """
        with db.no_autoflush:
            execution = (
                db.query(FlowExecution)
                .filter(FlowExecution.id == execution_id)
                .populate_existing()
                .with_for_update()
                .one_or_none()
            )
        if execution is None:
            return None
        current = execution.result if isinstance(execution.result, dict) else {}
        stored_url = current.get("pr_url")
        if stored_url and stored_url != pr_url:
            from preloop.services.flow_pr_binding import normalize_pr_url

            stored_key = normalize_pr_url(stored_url) or stored_url
            incoming_key = normalize_pr_url(pr_url) or pr_url
            if stored_key != incoming_key:
                raise ValueError("Publishing execution binding changed")
        if (
            source_branch
            and current.get("pr_source_branch")
            and current["pr_source_branch"] != source_branch
        ):
            raise ValueError("Publishing execution binding changed")
        if current.get("pr_url") == pr_url and (
            not source_branch or current.get("pr_source_branch") == source_branch
        ):
            return execution
        execution.result = {
            **current,
            "pr_url": pr_url,
            **({"pr_source_branch": source_branch} if source_branch else {}),
        }
        db.flush()
        if commit:
            db.commit()
        return execution

    def set_cli_session(
        self, db: Session, *, db_obj: FlowExecution, cli_session: Optional[dict]
    ) -> FlowExecution:
        """Persist (or clear) the agent CLI session reference.

        Separate from ``update`` because the value is written from the log
        streaming task the moment the agent reports it (and from the terminal
        rescan fallback), outside any FlowExecutionUpdate round trip. Shape:
        ``{"agent_type": "opencode", "session_id": "ses_..."}``.
        """
        with db.no_autoflush:
            stored = (
                db.query(FlowExecution.cli_session)
                .filter(FlowExecution.id == db_obj.id)
                .with_for_update()
                .first()
            )
        existing = stored[0] if stored is not None else None
        if (
            isinstance(existing, dict)
            and isinstance(cli_session, dict)
            and existing.get("session_id") == cli_session.get("session_id")
        ):
            cli_session = {**existing, **cli_session}
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

    def latest_with_result(
        self,
        db: Session,
        *,
        flow_id: Any,
        account_id: Optional[str] = None,
        exclude_execution_id: Any = None,
    ) -> Optional[FlowExecution]:
        """Newest execution of this flow that stored a result artifact.

        Backs the ``previous_result_execution_id: "last"`` payload
        sentinel, which is how a scheduled review run diffs against its own
        previous run: a schedule cannot know an execution id in advance,
        and a pinned id would freeze every future run against one baseline.
        The current execution is excluded explicitly, so a run started
        before the query cannot pick itself.
        """
        # "Stored a result" means a JSON document, not the JSON literal
        # ``null``: a row created with ``result=None`` persists as JSON null,
        # which satisfies ``IS NOT NULL`` and would otherwise be picked as a
        # baseline that contains nothing.
        query = db.query(FlowExecution).filter(
            FlowExecution.flow_id == flow_id,
            FlowExecution.result.isnot(None),
            func.jsonb_typeof(FlowExecution.result) != "null",
        )
        if exclude_execution_id:
            query = query.filter(FlowExecution.id != exclude_execution_id)
        if account_id:
            query = query.join(Flow).filter(Flow.account_id == account_id)
        return query.order_by(FlowExecution.start_time.desc()).first()

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

    def get_children(
        self,
        db: Session,
        parent_execution_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> List[FlowExecution]:
        """Get the executions started directly by one execution.

        Direct children only: a grandchild carries its own parent id, so a
        caller that wants the whole tree matches the root by its own id and
        the descendants by ``root_execution_id``. Ordered deterministically
        (``start_time``, with ``id`` as the tiebreak for rows that share a
        timestamp) so a tree renders in a stable order across calls. Returns
        an empty list for a leaf, which is the common case: most executions
        start nothing.

        ``account_id`` is required and joins through ``flow``: an execution
        id alone must not cross accounts.
        """
        return (
            db.query(FlowExecution)
            .options(joinedload(FlowExecution.flow))
            .join(Flow)
            .filter(
                FlowExecution.parent_execution_id == parent_execution_id,
                Flow.account_id == account_id,
            )
            .order_by(FlowExecution.start_time.asc(), FlowExecution.id.asc())
            .all()
        )

    def get_by_root(
        self,
        db: Session,
        root_execution_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> List[FlowExecution]:
        """Get every descendant of one root execution, at any depth.

        Full rows, including ``trigger_event_details``: a cost rollup reads
        each child's admitted ceiling from the delegation record. The UI tree
        uses :meth:`get_lineage` instead, which projects a lighter column
        set.

        The root row itself is NOT in the result: ``root_execution_id`` is
        null on the root (it is the root), so the whole tree is this list
        plus the row whose id is ``root_execution_id``. One indexed query
        rather than a recursive walk over ``parent_execution_id``, which is
        the reason the column exists (#626): a cost rollup over a tree is a
        single filter.

        ``account_id`` is required and joins through ``flow``: an execution
        id alone must not cross accounts. Ordered by start time so a tree
        renders in a stable order.
        """
        return (
            db.query(FlowExecution)
            .options(joinedload(FlowExecution.flow))
            .join(Flow)
            .filter(
                FlowExecution.root_execution_id == root_execution_id,
                Flow.account_id == account_id,
            )
            .order_by(FlowExecution.start_time.asc(), FlowExecution.id.asc())
            .all()
        )

    def get_lineage(
        self,
        db: Session,
        root_execution_id: uuid.UUID,
        account_id: uuid.UUID,
        limit: int = 1000,
    ) -> List[FlowExecution]:
        """Get every execution started under one lineage root.

        One query for a whole delegation tree: the root row is the caller's
        own execution and every descendant carries the root's id in
        ``root_execution_id`` (indexed), so no recursive walk is needed and
        depth is read off each row. The root itself is NOT returned: its own
        ``root_execution_id`` is NULL by definition, and a caller that has the
        root already does not need it back.

        Rows are lightly loaded on purpose. A tree row shows flow, label,
        state, times and cost, so the large JSON columns (trigger payload,
        result, prompt) stay out of the read and the delegation label is
        projected out of ``trigger_event_details`` instead of shipping it.

        Ordered by depth, then start time, then id, so a tree renders parents
        before children and in a stable order across calls.

        ``limit`` bounds the read: the fan-out and depth caps already bound a
        tree, but a configurable cap is not a guarantee, so a caller can tell
        a truncated answer from a complete one by asking for one row more than
        it means to show.

        ``account_id`` is required and joins through ``flow``: an execution
        id alone must not cross accounts.
        """
        label = FlowExecution.trigger_event_details[DELEGATION_DETAILS_KEY][
            "label"
        ].astext
        subject = FlowExecution.trigger_event_details[TRIGGER_SUBJECT_KEY]
        return (
            db.query(FlowExecution)
            .options(
                load_only(
                    FlowExecution.id,
                    FlowExecution.flow_id,
                    FlowExecution.status,
                    FlowExecution.start_time,
                    FlowExecution.end_time,
                    FlowExecution.error_message,
                    FlowExecution.failure_category,
                    FlowExecution.queued_reason,
                    # Why a row in this tree changed state, which for a
                    # cascading stop (#689) is the only place the tree can
                    # say "stopped with its parent" rather than "stopped".
                    FlowExecution.stop_reason,
                    FlowExecution.parent_execution_id,
                    FlowExecution.root_execution_id,
                    FlowExecution.delegation_depth,
                    FlowExecution.retry_of_execution_id,
                    FlowExecution.batch_id,
                    FlowExecution.parked_at,
                    FlowExecution.park_expires_at,
                    FlowExecution.tool_calls_count,
                    FlowExecution.total_tokens,
                    FlowExecution.estimated_cost,
                    FlowExecution.created_at,
                    FlowExecution.updated_at,
                ),
                with_expression(FlowExecution.delegation_label, label),
                # Projected for the same reason the list view projects them:
                # the row is rendered with a subject, and a deferred query
                # expression that nothing populates cannot even be read.
                with_expression(FlowExecution.trigger_subject, subject["text"].astext),
                with_expression(
                    FlowExecution.trigger_subject_url, subject["url"].astext
                ),
                joinedload(FlowExecution.flow).load_only(Flow.id, Flow.name),
            )
            .join(Flow)
            .filter(
                FlowExecution.root_execution_id == root_execution_id,
                Flow.account_id == account_id,
            )
            .order_by(
                FlowExecution.delegation_depth.asc(),
                FlowExecution.start_time.asc(),
                FlowExecution.id.asc(),
            )
            .limit(max(1, int(limit)))
            .all()
        )

    def get_running_by_flow(
        self,
        db: Session,
        flow_id: uuid.UUID,
        account_id: Optional[uuid.UUID] = None,
        running_statuses: Optional[List[str]] = None,
        *,
        tracker_object_key: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[FlowExecution]:
        """Get running flow executions for a specific flow.

        Unlike get_by_flow, this specifically queries for executions in running states
        without a limit, ensuring long-running executions are not missed.

        When ``tracker_object_key`` is set (one-active-run coalescing), the
        query loads only ``id``, ``status`` and ``trigger_event_details``,
        pushes the object-key match into JSONB, and caps the rows read.

        Args:
            db: Database session
            flow_id: The flow ID to query
            account_id: Optional account ID to filter by
            running_statuses: List of statuses considered "running".
                             Defaults to ["PENDING", "INITIALIZING", "STARTING", "RUNNING"]
            tracker_object_key: Optional ``source:repo:kind:id`` key. When
                set, only matching payloads are loaded.
            limit: Optional row cap. Defaults to
                ``TRACKER_OBJECT_LOOKUP_LIMIT`` when ``tracker_object_key``
                is set, otherwise unbounded.

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
        if tracker_object_key is not None:
            query = query.options(
                load_only(
                    FlowExecution.id,
                    FlowExecution.status,
                    FlowExecution.trigger_event_details,
                )
            )
            payload_match = tracker_object_payload_match(tracker_object_key)
            if payload_match is not None:
                query = query.filter(payload_match)
            row_limit = (
                TRACKER_OBJECT_LOOKUP_LIMIT if limit is None else max(1, int(limit))
            )
            rows = query.limit(row_limit + 1).all()
            if len(rows) > row_limit:
                logger.debug(
                    "Tracker-object lookup for flow %s key %s exceeded "
                    "limit %s; extra rows are ignored",
                    flow_id,
                    tracker_object_key,
                    row_limit,
                )
                return rows[:row_limit]
            return rows
        if limit is not None:
            return query.limit(max(1, int(limit))).all()
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
                    # Same reason: the list view is where a queued run is
                    # noticed, so "why is it not starting" must not be a
                    # per-row lazy load.
                    FlowExecution.queued_reason,
                    FlowExecution.runner_id,
                    FlowExecution.agent_session_reference,
                    FlowExecution.retry_of_execution_id,
                    FlowExecution.parent_execution_id,
                    FlowExecution.root_execution_id,
                    FlowExecution.delegation_depth,
                    FlowExecution.parked_at,
                    FlowExecution.park_expires_at,
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
                with_expression(
                    FlowExecution.resume_of,
                    FlowExecution.trigger_event_details["_resume"][
                        "resume_root"
                    ].astext,
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
                ``token_usage`` follows the counts it sits beside: the window
                when one is given, all time otherwise.

        Returns:
            One row per flow that has ever executed.
        """
        if not flow_ids:
            return []

        from sqlalchemy import func, case
        from preloop.models.crud.api_usage import (
            cache_split_columns,
            cache_split_from_row,
            exclude_replay_usage_condition,
        )
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

        # Fetch cost and token stats from actual API usage. Tokens are
        # summed beside the money because a row that states spend and leaves
        # the token cell empty reads as "we measured nothing" on traffic we
        # did measure (the agents list asks for these lifetime figures).
        cost_stats = (
            db.query(
                ApiUsage.flow_id,
                func.coalesce(func.sum(ApiUsage.estimated_cost), 0.0).label(
                    "estimated_cost"
                ),
                func.coalesce(func.sum(ApiUsage.prompt_tokens), 0).label(
                    "prompt_tokens"
                ),
                func.coalesce(func.sum(ApiUsage.completion_tokens), 0).label(
                    "completion_tokens"
                ),
                func.coalesce(func.sum(ApiUsage.total_tokens), 0).label("total_tokens"),
                *cache_split_columns(),
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
        # Lifetime tokens, split the same way the windowed figures are, so a
        # caller that asks for no window still gets volume before price.
        token_map = {
            str(row.flow_id): GatewayTokenUsage.from_row(
                {
                    "prompt_tokens": int(row.prompt_tokens or 0),
                    "completion_tokens": int(row.completion_tokens or 0),
                    "total_tokens": int(row.total_tokens or 0),
                    **cache_split_from_row(row),
                }
            ).model_dump()
            for row in cost_stats
        }

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
                    func.coalesce(func.sum(ApiUsage.prompt_tokens), 0).label(
                        "prompt_tokens"
                    ),
                    func.coalesce(func.sum(ApiUsage.completion_tokens), 0).label(
                        "completion_tokens"
                    ),
                    func.coalesce(func.sum(ApiUsage.total_tokens), 0).label(
                        "total_tokens"
                    ),
                    *cache_split_columns(),
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
            # Tokens for the same window as the cost, so the flows list can
            # lead with volume and price it second.
            window_token_map = {
                str(row.flow_id): GatewayTokenUsage.from_row(
                    {
                        "prompt_tokens": int(row.prompt_tokens or 0),
                        "completion_tokens": int(row.completion_tokens or 0),
                        "total_tokens": int(row.total_tokens or 0),
                        **cache_split_from_row(row),
                    }
                ).model_dump()
                for row in window_cost
            }
            for row in window_stats:
                window_map[str(row.flow_id)] = {
                    "runs": int(row.runs or 0),
                    "failed": int(row.failed or 0),
                    "last_run_at": row.last_run_at,
                    "cost": window_cost_map.get(str(row.flow_id), 0.0),
                    "token_usage": window_token_map.get(str(row.flow_id)),
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
                # Tokens are always measured over the same period as the
                # counts beside them: the window when one was asked for (None
                # there means "no run in this window", not "no tokens"), the
                # lifetime otherwise, so a caller that shows lifetime spend
                # can show the lifetime volume that earned it.
                if start_date is not None:
                    self.token_usage = window.get("token_usage") if window else None
                else:
                    self.token_usage = token_map.get(str(row.flow_id))

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

    # --- Park / resume on a human decision ---------------------------------
    #
    # The approval path and the orchestrator run in different processes, so
    # the park handshake is three durable steps on this row: request (approval
    # path), confirm (orchestrator, once the runtime is released), claim
    # (decision path, exactly once).

    WAITING_FOR_HUMAN_STATUS = "WAITING_FOR_HUMAN"
    #: Sibling park: the run is waiting for the executions it started (#633).
    WAITING_FOR_CHILDREN_STATUS = "WAITING_FOR_CHILDREN"
    RESUMING_STATUS = "RESUMING"
    #: What a parked run is waiting on. Closed vocabulary, written on the row
    #: by whoever requests the park and read by the orchestrator to decide
    #: which parked status to confirm into.
    PARK_KIND_HUMAN = "human"
    PARK_KIND_CHILDREN = "children"
    PARKED_STATUS_BY_KIND = {
        PARK_KIND_HUMAN: WAITING_FOR_HUMAN_STATUS,
        PARK_KIND_CHILDREN: WAITING_FOR_CHILDREN_STATUS,
    }
    PARK_PARENT_CLOSE_STATUSES = frozenset(
        {"SUCCEEDED", "FAILED", "STOPPED", "CANCELLED"}
    )

    def parked_status_for_kind(self, kind: Optional[str]) -> str:
        """Which parked status one park kind confirms into.

        An unknown or missing kind reads as a human park: rows written before
        the column existed are all approval parks, and a typo must not invent
        a status nothing sweeps.
        """
        return self.PARKED_STATUS_BY_KIND.get(
            str(kind or self.PARK_KIND_HUMAN), self.WAITING_FOR_HUMAN_STATUS
        )

    def request_park(
        self,
        db: Session,
        *,
        execution_id: Any,
        approval_request_id: Any,
        expires_at: Optional[datetime] = None,
        kind: str = PARK_KIND_HUMAN,
        commit: bool = True,
    ) -> bool:
        """Ask the orchestrator to park this execution on an approval.

        Only a live execution can be parked; a run that already finished
        (the human answered a question its agent had abandoned) must not be
        resurrected into a park. Returns True when the request was recorded.

        ``kind`` says what the run is waiting on: an approval request by
        default, or the children it started (``PARK_KIND_CHILDREN``, #633),
        in which case ``approval_request_id`` is the wait id grouping them.

        A human park is refused when that approval is no longer pending.
        The decision and this write run in different transactions; without
        the check, a decision that commits first resumes nothing, and the
        park then suspends a run whose answer is already on the request.
        Children parks are not approval requests and skip the check.
        """
        if kind == self.PARK_KIND_HUMAN and not self._approval_still_pending(
            db, approval_request_id
        ):
            return False
        count = (
            db.query(models.FlowExecution)
            .filter(
                models.FlowExecution.id == execution_id,
                models.FlowExecution.status.in_(self.ACTIVE_ORCHESTRATOR_STATUSES),
                models.FlowExecution.park_request_id.is_(None),
            )
            .update(
                {
                    models.FlowExecution.park_request_id: approval_request_id,
                    models.FlowExecution.park_kind: kind,
                    models.FlowExecution.park_requested_at: datetime.now(timezone.utc),
                    models.FlowExecution.park_expires_at: expires_at,
                },
                synchronize_session=False,
            )
        )
        if commit:
            db.commit()
        return bool(count)

    def _approval_still_pending(self, db: Session, approval_request_id: Any) -> bool:
        """Lock the approval row and report whether it is still pending.

        Same transaction as the park write that follows. A missing row is
        not pending: there is nothing to wait on.
        """
        from preloop.models.models.approval_request import ApprovalRequest

        row = (
            db.query(ApprovalRequest.id)
            .filter(
                ApprovalRequest.id == approval_request_id,
                ApprovalRequest.status == "pending",
            )
            .with_for_update()
            .first()
        )
        return row is not None

    def get_park_request(self, db: Session, *, execution_id: Any) -> Optional[dict]:
        """Read park intent fresh on each monitor poll (see get_stop_request)."""
        row = (
            db.query(
                models.FlowExecution.park_request_id,
                models.FlowExecution.park_kind,
                models.FlowExecution.park_requested_at,
                models.FlowExecution.park_expires_at,
                models.FlowExecution.parked_at,
            )
            .filter(models.FlowExecution.id == execution_id)
            .first()
        )
        if row is None or row.park_request_id is None:
            return None
        return {
            "request_id": row.park_request_id,
            "kind": str(row.park_kind or self.PARK_KIND_HUMAN),
            "requested_at": row.park_requested_at,
            "expires_at": row.park_expires_at,
            "parked_at": row.parked_at,
        }

    def confirm_park(
        self,
        db: Session,
        *,
        execution_id: Any,
        compute_seconds: int,
        kind: str = PARK_KIND_HUMAN,
        commit: bool = True,
    ) -> None:
        """Record that the runtime is released and the run is genuinely parked.

        ``compute_seconds`` is the agent wall clock this park chain has spent
        so far. Time spent waiting (for a human, or for a child) is never
        added to it, which is what makes the flow's timeout budget pause
        while parked.
        """
        db.query(models.FlowExecution).filter(
            models.FlowExecution.id == execution_id,
            models.FlowExecution.status.notin_(tuple(self.TERMINAL_EXECUTION_STATUSES)),
            models.FlowExecution.stop_requested_at.is_(None),
            models.FlowExecution.parked_at.is_(None),
        ).update(
            {
                models.FlowExecution.status: self.parked_status_for_kind(kind),
                models.FlowExecution.parked_at: datetime.now(timezone.utc),
                models.FlowExecution.parked_compute_seconds: max(0, compute_seconds),
            },
            synchronize_session=False,
        )
        if commit:
            db.commit()

    def claim_parked_for_resume(
        self, db: Session, *, execution_id: Any, approval_request_id: Any
    ) -> bool:
        """Claim a parked execution for exactly one resume.

        A decision can arrive twice (console and mobile, a retried webhook, an
        expiry sweep racing a late approval). The conditional update is the
        whole idempotency story: the second caller claims zero rows and does
        nothing.

        The heartbeat is the lease: if this process dies before the resume
        execution is committed, ``reclaim_stale_resuming_claims`` returns the
        row to WAITING_FOR_HUMAN after the same stale timeout other claims
        use.
        """
        now = datetime.now(timezone.utc)
        count = (
            db.query(models.FlowExecution)
            .filter(
                models.FlowExecution.id == execution_id,
                models.FlowExecution.status == self.WAITING_FOR_HUMAN_STATUS,
                models.FlowExecution.park_request_id == approval_request_id,
                models.FlowExecution.resume_execution_id.is_(None),
            )
            .update(
                {
                    models.FlowExecution.status: self.RESUMING_STATUS,
                    models.FlowExecution.orchestrator_claimed_at: now,
                    models.FlowExecution.orchestrator_heartbeat_at: now,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return bool(count)

    def claim_parked_children_for_resume(
        self, db: Session, *, execution_id: Any, wait_id: Any
    ) -> bool:
        """Claim a parent parked on children for exactly one resume (#633).

        The sibling of ``claim_parked_for_resume``, and the same single
        conditional UPDATE: two children finishing at the same instant both
        try to resume the parent, and the second one claims zero rows. The
        wait id is matched too, so a park that was already consumed and
        re-requested cannot be claimed by a late child of the previous wait.
        A durable stop intent also refuses the claim: a park-finalize that
        overwrote STOPPED into WAITING_FOR_CHILDREN must not resume spend
        after the operator stopped the tree.
        """
        now = datetime.now(timezone.utc)
        count = (
            db.query(models.FlowExecution)
            .filter(
                models.FlowExecution.id == execution_id,
                models.FlowExecution.status == self.WAITING_FOR_CHILDREN_STATUS,
                models.FlowExecution.park_request_id == wait_id,
                models.FlowExecution.resume_execution_id.is_(None),
                models.FlowExecution.stop_requested_at.is_(None),
            )
            .update(
                {
                    models.FlowExecution.status: self.RESUMING_STATUS,
                    models.FlowExecution.orchestrator_claimed_at: now,
                    models.FlowExecution.orchestrator_heartbeat_at: now,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return bool(count)

    def release_children_claim(self, db: Session, *, execution_id: Any) -> bool:
        """Return an unconsumed children claim to WAITING_FOR_CHILDREN.

        Used when the resume could not be created: the row goes back to
        parked so the sweep retries it. A claim that already has a resume
        execution is left alone, or the sweep would start a second one.
        """
        count = (
            db.query(models.FlowExecution)
            .filter(
                models.FlowExecution.id == execution_id,
                models.FlowExecution.status == self.RESUMING_STATUS,
                models.FlowExecution.resume_execution_id.is_(None),
            )
            .update(
                {
                    models.FlowExecution.status: self.WAITING_FOR_CHILDREN_STATUS,
                    models.FlowExecution.orchestrator_worker_id: None,
                    models.FlowExecution.orchestrator_claimed_at: None,
                    models.FlowExecution.orchestrator_heartbeat_at: None,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return bool(count)

    def list_parked_on_children(
        self, db: Session, *, limit: int = 200
    ) -> List[FlowExecution]:
        """Every execution currently parked on the children it started."""
        return (
            db.query(FlowExecution)
            .filter(
                FlowExecution.status == self.WAITING_FOR_CHILDREN_STATUS,
                FlowExecution.stop_requested_at.is_(None),
            )
            .order_by(FlowExecution.park_requested_at.asc(), FlowExecution.id.asc())
            .limit(limit)
            .all()
        )

    # --- Stopping a parent, and the tree it is parked on (#689) ------------

    #: Statuses at which an execution can no longer change on its own. Same
    #: set ``flow_delegation_budget.TERMINAL_STATUSES`` uses, restated here so
    #: the CRUD layer does not import a service; a test asserts the two
    #: spellings agree. A cascading stop must never write over a row that
    #: already finished, however it finished.
    TERMINAL_EXECUTION_STATUSES = frozenset(
        {
            "SUCCEEDED",
            "FAILED",
            "STOPPED",
            "TIMEOUT",
            "TIMED_OUT",
            "ABORTED",
            "CANCELLED",
            "CANCELED",
        }
    )

    #: ``stop_source`` written on an execution stopped because the parent it
    #: was started by was stopped. Distinct from ``account_halt`` so the tree
    #: can say why a row changed: a kill switch and a parent's stop are not
    #: the same event to whoever is reading the run afterwards.
    STOP_SOURCE_PARENT_STOP = "parent_stop"

    def close_children_park_for_stop(
        self,
        db: Session,
        *,
        execution_id: Any,
        reason: str,
        now: Optional[datetime] = None,
        commit: bool = True,
    ) -> bool:
        """Close a park on children because an operator stopped the parent.

        One conditional UPDATE, and it is the whole race: it matches a row
        still sitting on ``WAITING_FOR_CHILDREN``, or a still-live row that
        has requested a children park but has not been confirmed yet. A
        child that finishes at the same instant either claims the park first
        (and this returns False, the caller then stops the resume it
        created) or claims nothing, because the row is already ``STOPPED``.
        Both orders leave exactly one outcome and no second resume.

        The park row is closed rather than left claimable: the expiry is
        cleared so no sweep looks at it again, while ``park_request_id`` and
        ``park_kind`` stay for the audit trail. ``stop_source`` stays NULL
        on this row: the operator stopped the parent, which is the same
        provenance as a plain stop. ``parent_stop`` is reserved for
        children this stop ends.
        """
        moment = now or datetime.now(timezone.utc)
        count = (
            db.query(models.FlowExecution)
            .filter(
                models.FlowExecution.id == execution_id,
                models.FlowExecution.resume_execution_id.is_(None),
                or_(
                    models.FlowExecution.status == self.WAITING_FOR_CHILDREN_STATUS,
                    and_(
                        models.FlowExecution.status == "RUNNING",
                        models.FlowExecution.park_kind == self.PARK_KIND_CHILDREN,
                        models.FlowExecution.parked_at.is_(None),
                    ),
                ),
            )
            .update(
                {
                    models.FlowExecution.status: "STOPPED",
                    models.FlowExecution.end_time: moment,
                    models.FlowExecution.error_message: reason,
                    models.FlowExecution.park_expires_at: None,
                    models.FlowExecution.stop_requested_at: func.coalesce(
                        models.FlowExecution.stop_requested_at, moment
                    ),
                    models.FlowExecution.stop_reason: reason[:500],
                    models.FlowExecution.orchestrator_worker_id: None,
                    models.FlowExecution.orchestrator_claimed_at: None,
                    models.FlowExecution.orchestrator_heartbeat_at: None,
                },
                synchronize_session=False,
            )
        )
        if commit:
            db.commit()
        return bool(count)

    def stop_for_parent_stop(
        self,
        db: Session,
        *,
        execution_id: Any,
        reason: str,
        now: Optional[datetime] = None,
        commit: bool = True,
    ) -> bool:
        """Stop one execution because the run that started it was stopped.

        Conditional on the row not being terminal already, which is what
        keeps a child that completed while the stop was in flight: it keeps
        its own terminal status, its result and its cost, and this returns
        False rather than overwriting any of them.

        The durable stop intent is written alongside the status so a runtime
        that is still alive is actually torn down: the orchestrator polls
        ``stop_requested_at`` on every loop and a runner reads it through the
        same path an account halt uses. ``stop_reason`` is what the execution
        tree shows for a row this stop changed.
        """
        moment = now or datetime.now(timezone.utc)
        count = (
            db.query(models.FlowExecution)
            .filter(
                models.FlowExecution.id == execution_id,
                models.FlowExecution.status.notin_(
                    sorted(self.TERMINAL_EXECUTION_STATUSES)
                ),
            )
            .update(
                {
                    models.FlowExecution.status: "STOPPED",
                    models.FlowExecution.end_time: moment,
                    models.FlowExecution.error_message: reason,
                    models.FlowExecution.park_expires_at: None,
                    models.FlowExecution.stop_requested_at: func.coalesce(
                        models.FlowExecution.stop_requested_at, moment
                    ),
                    models.FlowExecution.stop_reason: reason[:500],
                    models.FlowExecution.stop_source: self.STOP_SOURCE_PARENT_STOP,
                },
                synchronize_session=False,
            )
        )
        if commit:
            db.commit()
        return bool(count)

    def record_stop_coverage(
        self,
        db: Session,
        *,
        execution_id: Any,
        coverage: Dict[str, Any],
        commit: bool = True,
    ) -> bool:
        """Record on a stopped parent how far its tree got (#689).

        Written under the reserved ``_stop_coverage`` key of the execution's
        own trigger details, next to the other platform-authored blocks, so
        the record of a stopped tree is the execution row rather than a log
        line somebody has to find.
        """
        row = self.get(db, id=execution_id)
        if row is None:
            return False
        details = dict(row.trigger_event_details or {})
        details[STOP_COVERAGE_KEY] = coverage
        row.trigger_event_details = details
        db.add(row)
        if commit:
            db.commit()
        else:
            db.flush()
        return True

    def mark_park_resumed(
        self,
        db: Session,
        *,
        execution_id: Any,
        resume_execution_id: Any,
        commit: bool = True,
    ) -> bool:
        """Link a RESUMING park claim to the resume execution just flushed.

        After this write the claim is consumed: a later dispatch failure
        must not release it, and the sweep must not create a second resume.
        """
        count = (
            db.query(models.FlowExecution)
            .filter(
                models.FlowExecution.id == execution_id,
                models.FlowExecution.status == self.RESUMING_STATUS,
                models.FlowExecution.resume_execution_id.is_(None),
            )
            .update(
                {models.FlowExecution.resume_execution_id: resume_execution_id},
                synchronize_session=False,
            )
        )
        if commit:
            db.commit()
        return bool(count)

    def close_parked_parent_for_resume(
        self,
        db: Session,
        *,
        resume_execution_id: Any,
        status: str,
        end_time: Optional[datetime] = None,
        commit: bool = False,
    ) -> int:
        """Mark the parked parent terminal once its resume child finishes.

        Matches rows whose ``resume_execution_id`` is this child and whose
        status is still ``RESUMING``. Crash-stranded claims (RESUMING with
        no child) are left for ``reclaim_stale_resuming_claims``.
        """
        if status not in self.PARK_PARENT_CLOSE_STATUSES:
            return 0
        if resume_execution_id is None:
            return 0
        closed_at = end_time or datetime.now(timezone.utc)
        count = (
            db.query(models.FlowExecution)
            .filter(
                models.FlowExecution.resume_execution_id == resume_execution_id,
                models.FlowExecution.status == self.RESUMING_STATUS,
                models.FlowExecution.resume_execution_id.isnot(None),
            )
            .update(
                {
                    models.FlowExecution.status: status,
                    models.FlowExecution.end_time: closed_at,
                },
                synchronize_session=False,
            )
        )
        if commit:
            db.commit()
        return count

    def reclaim_stale_resuming_claims(
        self,
        db: Session,
        *,
        now: datetime,
        stale_after_seconds: Optional[int] = None,
    ) -> int:
        """Return stranded RESUMING claims whose lease has expired.

        A crash between ``claim_parked_for_resume`` and committing the resume
        execution leaves status RESUMING with no child. No other sweep looks
        at that status. Same stale window as orchestrator worker claims.
        Consumed claims (``resume_execution_id`` set) are left alone so a
        failed dispatch cannot double-run.

        Human parks only: a claim on a children park goes back to
        WAITING_FOR_CHILDREN instead, which is
        ``reclaim_stale_children_claims``. Rows written before ``park_kind``
        existed are human parks, so a NULL kind belongs here.
        """
        return self._reclaim_stale_claims(
            db,
            now=now,
            stale_after_seconds=stale_after_seconds,
            kind=self.PARK_KIND_HUMAN,
        )

    def reclaim_stale_children_claims(
        self,
        db: Session,
        *,
        now: datetime,
        stale_after_seconds: Optional[int] = None,
    ) -> int:
        """Return stranded children-park claims to WAITING_FOR_CHILDREN (#633).

        Same lease, same reasoning, different parked status: a parent whose
        resume was never created has to become claimable again by the next
        child completion or by the sweep.
        """
        return self._reclaim_stale_claims(
            db,
            now=now,
            stale_after_seconds=stale_after_seconds,
            kind=self.PARK_KIND_CHILDREN,
        )

    def _reclaim_stale_claims(
        self,
        db: Session,
        *,
        now: datetime,
        stale_after_seconds: Optional[int],
        kind: str,
    ) -> int:
        """One conditional UPDATE returning expired claims of one park kind."""
        from preloop.config import settings

        stale_after = (
            stale_after_seconds
            if stale_after_seconds is not None
            else int(settings.flow_execution_claim_stale_seconds)
        )
        stale_before = now - timedelta(seconds=max(1, stale_after))
        if kind == self.PARK_KIND_CHILDREN:
            kind_filter = models.FlowExecution.park_kind == self.PARK_KIND_CHILDREN
        else:
            kind_filter = or_(
                models.FlowExecution.park_kind.is_(None),
                models.FlowExecution.park_kind != self.PARK_KIND_CHILDREN,
            )
        count = (
            db.query(models.FlowExecution)
            .filter(
                models.FlowExecution.status == self.RESUMING_STATUS,
                models.FlowExecution.resume_execution_id.is_(None),
                or_(
                    models.FlowExecution.orchestrator_heartbeat_at.is_(None),
                    models.FlowExecution.orchestrator_heartbeat_at < stale_before,
                ),
                kind_filter,
            )
            .update(
                {
                    models.FlowExecution.status: self.parked_status_for_kind(kind),
                    models.FlowExecution.orchestrator_worker_id: None,
                    models.FlowExecution.orchestrator_claimed_at: None,
                    models.FlowExecution.orchestrator_heartbeat_at: None,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return int(count or 0)

    def list_parked_for_request(
        self, db: Session, *, approval_request_id: Any
    ) -> List[FlowExecution]:
        """Every execution parked on one approval request."""
        return (
            db.query(FlowExecution)
            .filter(
                FlowExecution.park_request_id == approval_request_id,
                FlowExecution.status == self.WAITING_FOR_HUMAN_STATUS,
            )
            .all()
        )

    def list_parked_expired(
        self, db: Session, *, now: datetime, limit: int = 50
    ) -> List[FlowExecution]:
        """Parked executions whose approval window has closed."""
        return (
            db.query(FlowExecution)
            .filter(
                FlowExecution.status == self.WAITING_FOR_HUMAN_STATUS,
                FlowExecution.park_expires_at.isnot(None),
                FlowExecution.park_expires_at <= now,
            )
            .order_by(FlowExecution.park_expires_at.asc())
            .limit(limit)
            .all()
        )

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
        # Halt the one assignment, not the runner: the same machine may be
        # running other executions that were not stopped.
        assignment = (
            db.query(models.FlowRunnerAssignment)
            .filter(models.FlowRunnerAssignment.execution_id == execution_id)
            .with_for_update()
            .first()
        )
        if assignment is not None:
            assignment.halt_requested = True
        elif str(execution.agent_session_reference or "").startswith("runner:queued:"):
            execution.status = "STOPPED"
            stopped_at = datetime.now(timezone.utc)
            execution.end_time = stopped_at
            self.confirm_stop(db, execution_id=execution_id, commit=False)
            self.close_parked_parent_for_resume(
                db,
                resume_execution_id=execution_id,
                status="STOPPED",
                end_time=stopped_at,
                commit=False,
            )
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

    @staticmethod
    def _admitted_predicate(stale_before: datetime) -> ColumnElement[bool]:
        """Executions that hold, or are about to hold, a runtime slot.

        An agent session means a container exists whatever the worker is
        doing. A live claim heartbeat means a worker is starting one. A claim
        whose heartbeat went stale is a dead worker and must not keep an
        account's slot occupied forever.
        """
        from sqlalchemy import and_

        return or_(
            models.FlowExecution.agent_session_reference.isnot(None),
            and_(
                models.FlowExecution.orchestrator_worker_id.isnot(None),
                models.FlowExecution.orchestrator_heartbeat_at.isnot(None),
                models.FlowExecution.orchestrator_heartbeat_at >= stale_before,
            ),
        )

    def count_admitted_by_account(
        self,
        db: Session,
        *,
        stale_after_seconds: int = 120,
        account_id: Optional[Any] = None,
        exclude_execution_id: Optional[Any] = None,
        hosted_only: bool = False,
    ) -> Dict[Any, int]:
        """How many executions each account currently has admitted.

        Parked runs (WAITING_FOR_HUMAN, WAITING_FOR_CHILDREN) are deliberately
        absent: they hold no container, no runner and no worker, so counting
        them would let one human decision, or one slow child, block an
        account's remaining slots for days.

        Args:
            db: Database session.
            stale_after_seconds: Seconds after which a claim heartbeat is dead.
            account_id: Count one account instead of every account.
            exclude_execution_id: Leave this execution out of the count.
            hosted_only: Count executions that need hosted compute and skip
                the ones a private runner already holds. The per-account cap
                bounds a shared pool; an account's own runners are bounded by
                their own concurrency, so counting them there would punish
                exactly the accounts that bring capacity.
        """
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import func

        from preloop.services.runner_service import runner_assigned_execution_clause

        stale_before = datetime.now(timezone.utc) - timedelta(
            seconds=max(1, stale_after_seconds)
        )
        query = (
            db.query(
                models.Flow.account_id,
                func.count(models.FlowExecution.id),
            )
            .join(models.Flow, models.Flow.id == models.FlowExecution.flow_id)
            .filter(
                models.FlowExecution.status.in_(self.ACTIVE_ORCHESTRATOR_STATUSES),
                self._admitted_predicate(stale_before),
            )
        )
        if hosted_only:
            query = query.filter(
                ~runner_assigned_execution_clause(
                    reference_column=models.FlowExecution.agent_session_reference,
                    runner_id_column=models.FlowExecution.runner_id,
                )
            )
        if account_id is not None:
            query = query.filter(models.Flow.account_id == account_id)
        if exclude_execution_id is not None:
            query = query.filter(models.FlowExecution.id != exclude_execution_id)
        return {
            row_account: int(count)
            for row_account, count in query.group_by(models.Flow.account_id).all()
        }

    @contextmanager
    def stale_claim_reaper_lease(
        self,
        db: Session,
        *,
        holder: str = "",
    ) -> Iterator[bool]:
        """Hold "one stale-claim reaper pass at a time", instance wide.

        Yields True to the single caller that took the lease and False to
        every other caller, which then skips its pass. Losing is not an
        error: the pass runs on a timer and the holder is doing the same
        work.

        A session-level ``pg_try_advisory_lock``, not a leased row: it is
        released by ``pg_advisory_unlock`` on the way out, and by Postgres
        itself if the holder's connection dies, so a crashed reaper cannot
        wedge every replica the way an expiring row lease would until its
        deadline passed. The lock is pinned to a dedicated checkout from
        the bind, not the Session's connection. ``record_redispatch``
        commits mid-pass; SQLAlchemy 2 then returns the Session checkout
        to the pool, so unlocking on ``db`` can land on a different
        connection, return false, and strand the lock until recycle.
        Unlock still rolls the Session back first so an aborted pass
        cannot raise ``PendingRollbackError`` on later Session use.
        Rollback failure does not skip the unlock. If unlock does not
        verifiably succeed, the dedicated checkout is invalidated so
        Postgres drops the lock now instead of at pool recycle.
        Non-Postgres dialects (single-process dev, SQLite tests) always
        win the lease: there is no second reaper to exclude.

        Args:
            db: Database session for the pass. The lock lives on a
                dedicated connection from the same bind.
            holder: Optional worker id, logged so "who is reaping?" has an
                answer.

        Yields:
            True when this caller may run the pass.
        """
        from sqlalchemy import text
        from sqlalchemy.engine import Engine

        from preloop.services.execution_reaper import STALE_CLAIM_REAPER_LOCK_KEY

        bind = db.get_bind()
        dialect = getattr(getattr(bind, "dialect", None), "name", None)
        if bind is None or dialect != "postgresql":
            yield True
            return

        engine = bind if isinstance(bind, Engine) else bind.engine
        with engine.connect() as lock_conn:
            acquired = bool(
                lock_conn.execute(
                    text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"),
                    {"key": STALE_CLAIM_REAPER_LOCK_KEY},
                ).scalar()
            )
            # Session-level lock survives commit. End autobegin so this
            # checkout is not idle-in-transaction across the pass.
            lock_conn.commit()
            if not acquired:
                logger.debug(
                    "Stale-claim reaper lease is held elsewhere; %s skips this pass",
                    holder or "this worker",
                )
                yield False
                return
            try:
                yield True
            finally:
                released = False
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001 - must not skip the unlock below
                    logger.warning(
                        "Rollback after the stale-claim reaper pass failed",
                        exc_info=True,
                    )
                try:
                    released = bool(
                        lock_conn.execute(
                            text(
                                "SELECT pg_advisory_unlock(hashtextextended(:key, 0))"
                            ),
                            {"key": STALE_CLAIM_REAPER_LOCK_KEY},
                        ).scalar()
                    )
                    lock_conn.commit()
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Failed to release the stale-claim reaper lease",
                        exc_info=True,
                    )
                if not released:
                    logger.error(
                        "Stale-claim reaper lease was not released; closing the "
                        "connection so Postgres drops the lock now, not at recycle"
                    )
                    lock_conn.invalidate()

    def record_redispatch(
        self,
        db: Session,
        *,
        execution_ids: Iterable[Any],
        now: Optional[datetime] = None,
    ) -> int:
        """Count a reaper re-publish against each execution.

        The counter is what the backoff grows on, and it lives on the row
        rather than in a worker process so every replica applies the same
        schedule to the same execution.

        Returns:
            How many rows were updated.
        """
        from sqlalchemy import func

        ids = [execution_id for execution_id in execution_ids]
        if not ids:
            return 0
        moment = now or datetime.now(timezone.utc)
        updated = (
            db.query(FlowExecution)
            .filter(FlowExecution.id.in_(ids))
            .update(
                {
                    FlowExecution.redispatch_count: func.coalesce(
                        FlowExecution.redispatch_count, 0
                    )
                    + 1,
                    FlowExecution.last_redispatch_at: moment,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return int(updated or 0)

    def get_queued_reason(self, db: Session, *, execution_id: Any) -> Optional[str]:
        """Why this execution has not been admitted yet, or None."""
        return (
            db.query(FlowExecution.queued_reason)
            .filter(FlowExecution.id == execution_id)
            .scalar()
        )

    def claim_execution(
        self,
        db: Session,
        *,
        execution_id: Any,
        worker_id: str,
        stale_after_seconds: int = 120,
        account_cap: Optional[int] = None,
        enforce_account_cap: bool = True,
        allow_same_worker: bool = True,
    ) -> Optional[FlowExecution]:
        """Atomically claim an active execution for a worker.

        Uses ``FOR UPDATE SKIP LOCKED`` so concurrent workers never double-claim.
        An execution is claimable when unclaimed, claimed by this worker, or the
        previous claim heartbeat is older than ``stale_after_seconds``.

        Admission is additionally bounded per account. A fresh PENDING
        execution whose account already has its cap admitted is NOT claimed:
        it stays PENDING with ``queued_reason`` set so the instance-wide
        worker pool cannot be monopolised by one account. The count and the
        claim happen under one transaction-scoped advisory lock keyed on the
        account, so two workers cannot both take the last slot.

        The cap bounds hosted compute only. An execution assigned to one of
        the account's private runners is bounded by that runner's own
        capacity (one job per runner today), so it is neither counted nor
        held back here.

        The cap applies to admission only. An execution that already has an
        agent session, or that this worker already owns, is always claimable:
        refusing it would leave a live container unmonitored, which is worse
        than being one over the cap for one run.

        Args:
            db: Database session.
            execution_id: Flow execution id.
            worker_id: Stable id for the claiming worker (pod name / hostname).
            stale_after_seconds: Seconds after last heartbeat before a claim is
                considered abandoned.
            account_cap: Override the resolved per-account cap (tests, callers
                that already know it).
            enforce_account_cap: Set False to skip the cap entirely.
            allow_same_worker: Allow reentry by the same worker identity. Durable
                triage disables this so duplicate local or broker callbacks
                cannot concurrently orchestrate the same execution.

        Returns:
            The claimed execution row, or ``None`` if another worker holds a
            fresh claim, the execution is not claimable, or the account is at
            its concurrency cap (``queued_reason`` says which).
        """
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import or_, text

        from preloop.services.execution_concurrency import (
            QUEUED_REASON_ACCOUNT_CAP,
            account_running_cap,
        )
        from preloop.services.runner_service import is_runner_assigned_reference

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
                    (FlowExecution.orchestrator_worker_id == worker_id)
                    if allow_same_worker
                    else False,
                    FlowExecution.orchestrator_heartbeat_at.is_(None),
                    FlowExecution.orchestrator_heartbeat_at < stale_before,
                ),
            )
            .with_for_update(skip_locked=True)
            .first()
        )
        if row is None:
            return None

        # A runner-assigned execution runs on the account's own compute. Its
        # bound is the runner's free capacity, applied when the job is leased,
        # not the shared hosted cap.
        is_runner_assigned = (
            is_runner_assigned_reference(row.agent_session_reference)
            or row.runner_id is not None
        )
        is_fresh_admission = (
            row.agent_session_reference is None
            and row.status == "PENDING"
            and row.orchestrator_worker_id != worker_id
        )
        if enforce_account_cap and is_fresh_admission and not is_runner_assigned:
            account_id = (
                db.query(models.Flow.account_id)
                .filter(models.Flow.id == row.flow_id)
                .scalar()
            )
        else:
            account_id = None
        if account_id is not None:
            # Serialize admission decisions for this account so two workers
            # cannot both take the last slot. Transaction scoped: every path
            # below commits, which releases it. Taken after the row lock on
            # purpose, so a worker never holds it while waiting for a row.
            # Postgres only; other dialects keep the pre-cap behaviour.
            if db.bind is not None and db.bind.dialect.name == "postgresql":
                db.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"flow_execution_account_cap:{account_id}"},
                )
            if account_cap is None:
                cap = account_running_cap(db.get(models.Account, account_id))
            else:
                cap = max(1, int(account_cap))
            admitted = self.count_admitted_by_account(
                db,
                stale_after_seconds=stale_after_seconds,
                account_id=account_id,
                exclude_execution_id=row.id,
                hosted_only=True,
            ).get(account_id, 0)
            if admitted >= cap:
                if row.queued_reason != QUEUED_REASON_ACCOUNT_CAP:
                    row.queued_reason = QUEUED_REASON_ACCOUNT_CAP
                    db.add(row)
                db.commit()
                logger.info(
                    "Not claiming execution %s: account %s already has %s/%s "
                    "admitted executions; it stays PENDING (%s)",
                    row.id,
                    account_id,
                    admitted,
                    cap,
                    QUEUED_REASON_ACCOUNT_CAP,
                )
                return None

        row.orchestrator_worker_id = worker_id
        row.orchestrator_claimed_at = now
        row.orchestrator_heartbeat_at = now
        row.queued_reason = None
        # A claim is progress, so the reaper's backoff for this execution
        # starts again from zero. Without this, a run that queued for an hour
        # and then died on its new owner would wait out a fifteen minute gap
        # before anyone adopted it.
        row.redispatch_count = 0
        row.last_redispatch_at = None
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
