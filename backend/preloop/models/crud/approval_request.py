"""CRUD operations for ApprovalRequest model."""

from datetime import datetime, timezone
from typing import Any, Optional, Union
from uuid import UUID
from sqlalchemy import Text, and_, cast, func, or_
from sqlalchemy.orm import Query, Session, selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from preloop.models import models

from ..models.approval_request import ApprovalRequest
from .base import CRUDBase

NOTE_PENDING_APPROVAL_LIMIT = 3


class NoteApprovalLimitError(ValueError):
    """The sender already has enough live approvals queued for this target."""


def ensure_note_approval_capacity(
    db: Session,
    *,
    account_id: str,
    author_agent_id: Any,
    note_facts: dict[str, Any],
) -> None:
    """Bound pending requests without spending the note delivery allowance.

    The caller must hold the account halt lock until the new request commits,
    so concurrent creators cannot all pass the capacity check.
    """
    target_key = (
        "note_target_runtime_session_id"
        if note_facts.get("note_target_runtime_session_id")
        else "note_target_managed_agent_id"
    )
    target_id = note_facts.get(target_key)
    if not author_agent_id or not target_id:
        raise ValueError("Note approvals require a resolved sender and target")
    pending = (
        db.query(models.ApprovalRequest.id)
        .filter(
            models.ApprovalRequest.account_id == account_id,
            models.ApprovalRequest.managed_agent_id == author_agent_id,
            models.ApprovalRequest.tool_name == "send_note",
            models.ApprovalRequest.status == "pending",
            or_(
                models.ApprovalRequest.expires_at.is_(None),
                models.ApprovalRequest.expires_at > datetime.utcnow(),
            ),
            models.ApprovalRequest.rule_context["note_facts"][target_key].as_string()
            == str(target_id),
        )
        .limit(NOTE_PENDING_APPROVAL_LIMIT)
        .all()
    )
    if len(pending) >= NOTE_PENDING_APPROVAL_LIMIT:
        raise NoteApprovalLimitError(
            "Resolve an existing note approval for this target before asking again."
        )


def _escape_like(text: str) -> str:
    """Escape LIKE wildcards so a search for ``50%`` matches literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class CRUDApprovalRequest(CRUDBase[ApprovalRequest]):
    """CRUD operations for ApprovalRequest model."""

    def detach_loaded_result(
        self, db: Union[Session, AsyncSession], request: ApprovalRequest
    ) -> ApprovalRequest:
        """Preserve loaded scalar values before a read transaction rolls back."""
        db.expunge(request)
        return request

    def expire_stale_pending(
        self,
        db: Session,
        *,
        account_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        now: Optional[datetime] = None,
        commit: bool = True,
    ) -> int:
        """Mark pending approval requests past their expiry as expired."""
        from .account_halt import crud_account_halt

        now = now or datetime.utcnow()
        query = db.query(self.model.account_id).filter(
            self.model.status == "pending",
            self.model.expires_at <= now,
        )
        if account_id:
            query = query.filter(self.model.account_id == account_id)
        if execution_id:
            query = query.filter(self.model.execution_id == execution_id)
        accounts = sorted({row[0] for row in query.all()}, key=str)
        expired = 0
        for owner in accounts:
            crud_account_halt.lock_account(db, account_id=owner)
            halted = crud_account_halt.get_for_scope(
                db, account_id=owner, scope="tools"
            )
            pending = db.query(self.model).filter(
                self.model.account_id == owner,
                self.model.status == "pending",
                self.model.expires_at <= now,
            )
            if execution_id:
                pending = pending.filter(self.model.execution_id == execution_id)
            if halted is not None and halted.is_active:
                # A halt cannot revive approvals whose deadline already passed.
                pending = pending.filter(
                    self.model.expires_at < halted.activated_at.replace(tzinfo=None)
                )
            # The sealed original is deleted on every terminal status,
            # including this bulk expiry. update_approval_request does the
            # same for a single row; a query.update() would leave the
            # ciphertext on the expired row.
            from sqlalchemy.orm.attributes import flag_modified

            from preloop.services.sensitive_data.reference import (
                strip_sealed_original,
            )

            from .runtime_session_turn_state import release_approval_wait

            pending_rows = pending.all()
            for row in pending_rows:
                cleaned, removed = strip_sealed_original(
                    getattr(row, "tool_args", None)
                )
                if removed:
                    row.tool_args = cleaned
                    flag_modified(row, "tool_args")
            expired += pending.update(
                {"status": "expired", "resolved_at": self.model.expires_at},
                synchronize_session="fetch",
            )
            release_approval_wait(
                db, [getattr(row, "runtime_session_id", None) for row in pending_rows]
            )
        if commit:
            db.commit()
        return int(expired)

    def restore_halted_deadlines(
        self,
        db: Session,
        *,
        account_id: Union[UUID, str],
        activated_at: datetime,
        deactivated_at: datetime,
    ) -> int:
        """Restore remaining timeout once when tools resume, even without polls.

        Caller holds the account lock. Pending requests created during the halt
        retain their full original timeout; already-expired requests stay expired.
        """
        from sqlalchemy import func

        start = activated_at.astimezone(timezone.utc).replace(tzinfo=None)
        end = deactivated_at.astimezone(timezone.utc).replace(tzinfo=None)
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.status == "pending",
                self.model.expires_at >= start,
                self.model.requested_at <= end,
            )
            .update(
                {
                    self.model.expires_at: self.model.expires_at
                    + (end - func.greatest(self.model.requested_at, start))
                },
                synchronize_session=False,
            )
        )

    def get_by_token(
        self,
        db: Session,
        *,
        token: str,
    ) -> Optional[ApprovalRequest]:
        """Get approval request by approval token."""
        return db.query(self.model).filter(self.model.approval_token == token).first()

    def get_by_id_and_token(
        self,
        db: Session,
        *,
        request_id: str,
        token: str,
    ) -> Optional[ApprovalRequest]:
        """Get approval request by ID and token (for public token-based access).

        Args:
            db: Database session
            request_id: Approval request ID
            token: Approval token

        Returns:
            Approval request if found and token matches, None otherwise
        """
        return (
            db.query(self.model)
            .options(selectinload(self.model.approval_workflow))
            .filter(
                self.model.id == request_id,
                self.model.approval_token == token,
            )
            .first()
        )

    def get_multi_by_execution(
        self,
        db: Session,
        *,
        execution_id: str,
        account_id: Optional[str] = None,
        status: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[ApprovalRequest]:
        """Get approval requests for a specific execution."""
        query = db.query(self.model).filter(self.model.execution_id == execution_id)

        if account_id:
            query = query.filter(self.model.account_id == account_id)

        if status:
            query = query.filter(self.model.status == status)

        return (
            query.order_by(self.model.requested_at.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def _account_query(
        self,
        db: Session,
        *,
        account_id: str,
        execution_id: Optional[str] = None,
        status: Optional[str] = None,
        runtime_session_id: Optional[Union[UUID, str]] = None,
        tool_name: Optional[str] = None,
        search: Optional[str] = None,
    ) -> "Query[ApprovalRequest]":
        """The account's requests with the list filters applied, unordered."""
        query = db.query(self.model).filter(self.model.account_id == account_id)

        if execution_id:
            query = query.filter(self.model.execution_id == execution_id)

        if runtime_session_id:
            query = query.filter(
                self.model.runtime_session_id == str(runtime_session_id)
            )

        if status:
            query = query.filter(self.model.status == status)

        if tool_name:
            query = query.filter(self.model.tool_name == tool_name)

        if search and search.strip():
            # The console's search box: tool, summary, agent, execution,
            # reasoning and the arguments, as the browser used to match them.
            pattern = f"%{_escape_like(search.strip())}%"
            query = query.filter(
                or_(
                    self.model.tool_name.ilike(pattern, escape="\\"),
                    self.model.summary.ilike(pattern, escape="\\"),
                    self.model.managed_agent_name.ilike(pattern, escape="\\"),
                    self.model.execution_id.ilike(pattern, escape="\\"),
                    self.model.agent_reasoning.ilike(pattern, escape="\\"),
                    cast(self.model.tool_args, Text).ilike(pattern, escape="\\"),
                )
            )
        return query

    def get_multi_by_account(
        self,
        db: Session,
        *,
        account_id: str,
        execution_id: Optional[str] = None,
        status: Optional[str] = None,
        runtime_session_id: Optional[Union[UUID, str]] = None,
        tool_name: Optional[str] = None,
        search: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[ApprovalRequest]:
        """Get approval requests for an account with optional filters.

        ``runtime_session_id`` narrows to one agent conversation, which is what
        a live session view needs: without it the only way to answer "what is
        THIS session waiting on" is to page the whole account and filter in
        the browser. It is ANDed with ``account_id`` (never replacing it), so
        naming another account's session returns nothing rather than that
        account's approvals.

        Expiry is handled by explicit sweep callers via ``expire_stale_pending``;
        keeping this method read-only avoids hidden UPDATE+COMMIT work on list
        endpoints.
        """
        query = self._account_query(
            db,
            account_id=account_id,
            execution_id=execution_id,
            status=status,
            runtime_session_id=runtime_session_id,
            tool_name=tool_name,
            search=search,
        )
        return (
            query.order_by(self.model.requested_at.desc(), self.model.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count_by_account(
        self,
        db: Session,
        *,
        account_id: str,
        execution_id: Optional[str] = None,
        status: Optional[str] = None,
        runtime_session_id: Optional[Union[UUID, str]] = None,
        tool_name: Optional[str] = None,
        search: Optional[str] = None,
    ) -> int:
        """How many rows ``get_multi_by_account`` pages over, same filters."""
        query = self._account_query(
            db,
            account_id=account_id,
            execution_id=execution_id,
            status=status,
            runtime_session_id=runtime_session_id,
            tool_name=tool_name,
            search=search,
        )
        return int(
            query.with_entities(func.count(self.model.id)).order_by(None).scalar() or 0
        )

    def stats_by_account(self, db: Session, *, account_id: str) -> dict[str, Any]:
        """Counts, human approval rate inputs and mean response for the strip.

        One aggregate over the account's requests instead of the browser
        counting whatever page it happened to load.
        """
        model = self.model
        human = and_(model.auto_approved_reason.is_(None), ~model.decided_by_ai)
        decided = model.status.in_(["approved", "declined"])
        row = (
            db.query(
                func.count(model.id),
                func.count(model.id).filter(model.status == "pending"),
                func.count(model.id).filter(model.status == "approved"),
                func.count(model.id).filter(model.status == "declined"),
                func.count(model.id).filter(model.status == "expired"),
                func.count(model.id).filter(model.status == "cancelled"),
                func.count(model.id).filter(model.status == "approved", human),
                func.count(model.id).filter(model.status == "declined", human),
                func.count(model.id).filter(model.auto_approved_reason.isnot(None)),
                func.avg(
                    func.extract("epoch", model.resolved_at - model.requested_at)
                ).filter(decided, model.resolved_at.isnot(None)),
            )
            .filter(model.account_id == account_id)
            .one()
        )
        tools = [
            name
            for (name,) in db.query(model.tool_name)
            .filter(model.account_id == account_id)
            .distinct()
            .order_by(model.tool_name)
            .limit(500)
        ]
        avg_seconds = row[9]
        return {
            "total": row[0],
            "pending": row[1],
            "approved": row[2],
            "declined": row[3],
            "expired": row[4],
            "cancelled": row[5],
            "human_approved": row[6],
            "human_declined": row[7],
            "auto_approved_by_bypass": row[8],
            "avg_response_minutes": (
                round(float(avg_seconds) / 60) if avg_seconds is not None else None
            ),
            "tools": tools,
        }

    #: Written on requests cancelled because the execution that asked can
    #: no longer receive an answer.
    EXECUTION_ENDED_CANCEL_REASON = (
        "Cancelled because the execution ended before the approval was answered."
    )

    def cancel_pending_for_execution(
        self,
        db: Session,
        *,
        execution_id: str,
        reason: str = EXECUTION_ENDED_CANCEL_REASON,
        commit: bool = True,
    ) -> int:
        """Cancel pending requests whose execution can no longer be resumed.

        A terminal run (failed, cancelled, timed out) must not leave a
        question on the console that a human can answer into nothing. Only
        ``pending`` rows for this execution change. Already decided requests
        stay as they are.

        Returns:
            How many requests were cancelled.
        """
        from .runtime_session_turn_state import release_approval_wait

        now = datetime.utcnow()
        session_ids = [
            row[0]
            for row in db.query(self.model.runtime_session_id)
            .filter(
                self.model.execution_id == str(execution_id),
                self.model.status == "pending",
                self.model.runtime_session_id.isnot(None),
            )
            .all()
        ]
        cancelled = (
            db.query(self.model)
            .filter(
                self.model.execution_id == str(execution_id),
                self.model.status == "pending",
            )
            .update(
                {
                    "status": "cancelled",
                    "resolved_at": now,
                    "approver_comment": reason,
                },
                synchronize_session="fetch",
            )
        )
        release_approval_wait(db, session_ids)
        if commit:
            db.commit()
        return int(cancelled)


# Create instance
crud_approval_request = CRUDApprovalRequest(ApprovalRequest)


# Async helper functions
async def get_approval_request_async(
    db: Session, request_id: UUID
) -> Optional[ApprovalRequest]:
    """Async: Retrieve an approval request by its ID.

    Eagerly loads the approval_workflow relationship to avoid lazy loading
    issues in async context.

    Args:
        db: The async database session.
        request_id: The ID of the approval request.

    Returns:
        The approval request object if found, otherwise None.
    """
    result = await db.execute(
        select(ApprovalRequest)
        .where(ApprovalRequest.id == request_id)
        .options(selectinload(ApprovalRequest.approval_workflow))
    )
    return result.scalar_one_or_none()


async def get_approval_request_for_update_async(
    db: Session, request_id: UUID
) -> Optional[ApprovalRequest]:
    """Async: Retrieve an approval request by its ID with row-level locking.

    Uses SELECT ... FOR UPDATE to prevent concurrent modifications.
    This should be used when updating the responses field to avoid
    lost updates from concurrent votes.

    Eagerly loads the approval_workflow relationship to avoid lazy loading
    issues in async context.

    Args:
        db: The async database session.
        request_id: The ID of the approval request.

    Returns:
        The approval request object if found, otherwise None.
    """
    # Always lock account before approval, matching halt/deadline transitions.
    account_id = await db.scalar(
        select(models.ApprovalRequest.account_id).where(
            models.ApprovalRequest.id == request_id
        )
    )
    if account_id is None:
        return None
    from .account_halt import crud_account_halt

    await db.run_sync(
        lambda session: crud_account_halt.lock_account(session, account_id=account_id)
    )
    result = await db.execute(
        select(ApprovalRequest)
        .where(ApprovalRequest.id == request_id)
        .options(selectinload(ApprovalRequest.approval_workflow))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()
