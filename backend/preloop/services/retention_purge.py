"""The scheduled purge that makes a retention setting mean something.

A retention policy nobody enforces is a paragraph in a trust centre. This is
the job that enforces it, and it is written defensively because it is the only
code in the product whose purpose is to delete a customer's records.

Four properties it has to have, in order of how much they matter:

**It never touches a held record.** Every statement carries the legal hold
predicate. A hold that a purge could race past is not a hold. The predicate is
added centrally by :func:`_hold_filters_for_model` from the flag on the model,
so a class cannot carry a cutoff without carrying the hold check; a class whose
model has no flag has to say so in :data:`HOLD_EXEMPT_CLASSES` and explain why.
Statements that write a model outside :data:`RECORD_CLASSES` (the legacy
evidence-column drop on ``FlowExecution``) unpack the same helper.

**It is off by default.** ``RETENTION_PURGE_ENABLED`` is false. An operator
upgrading to this release does not discover afterwards that a background job
started deleting audit history. ``RETENTION_PURGE_DRY_RUN`` counts and audits
what would go without deleting it.

**It is bounded.** Batches of ``RETENTION_PURGE_BATCH_SIZE`` rows, each its
own transaction, at most ``RETENTION_PURGE_MAX_BATCHES`` per class per
account per pass, the whole pass under a wall-clock budget, and only inside
the off-peak UTC window. A backlog is drained across passes. It runs off the
request path in a background task, the same shape as
:class:`preloop.services.session_optimization_jobs.OptimizationJobSweeper`.

**It takes the copies with the original.** Derived rows that quote a record
go in the same transaction as the record, not on a later sweep: today that is
the session search corpus, wired per class through
:func:`preloop.services.session_search_retention.delete_derived_chunks` and
counted separately as ``derived_deleted``. A deletion that leaves a searchable
copy behind is not a deletion, and one that is only eventually true leaves a
window in which search answers from a record the account was told was gone.

**It audits itself.** Retention decisions are themselves records: one
``audit_log`` row per account and record class that actually deleted
something, with the cutoff, the counts and the setting that produced them.

The record classes and the floor live in
:mod:`preloop.services.retention_policy`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud import crud_audit_log
from preloop.models.crud.history_policy import lock_account_for_retention
from preloop.models.crud.legal_hold import execution_in_account
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.approval_request import ApprovalRequest
from preloop.models.models.audit_log import AuditLog
from preloop.models.models.flow_artifact import FlowArtifact
from preloop.models.models.flow_execution import FlowExecution
from preloop.models.models.runtime_session import RuntimeSession
from preloop.models.models.runtime_session_artifact import RuntimeSessionArtifact
from preloop.services.analytics_history import storage_history_days
from preloop.services.retention_policy import (
    CLASS_APPROVALS,
    CLASS_AUDIT,
    CLASS_EVIDENCE,
    CLASS_RUNTIME_SESSIONS,
    CLASS_USAGE,
    RECORD_CLASSES,
    resolve_retention,
)
from preloop.services.service_roles import (
    background_passes_allowed,
    current_service_role,
)
from preloop.services.session_search_retention import delete_derived_chunks

logger = logging.getLogger(__name__)

AUDIT_ACTION_PURGE = "retention_purge"
AUDIT_ACTION_PREVIEW = "retention_purge_preview"

#: Approval requests still waiting on a human are never purged, whatever the
#: retention says. A parked execution is waiting on that row; deleting it
#: would strand the run, and a six month old pending approval is a bug to fix
#: rather than a record to shred.
_UNRESOLVED_APPROVAL_STATUS = "pending"


@dataclass
class ClassResult:
    """What the purge did to one record class of one account."""

    record_class: str
    retention_days: int
    cutoff: datetime
    deleted: int = 0
    batches: int = 0
    #: Rows removed from tables derived from this class in the same pass: the
    #: session search corpus today. Counted separately from ``deleted``, which
    #: is the count of records of the class itself, so a retention report says
    #: how many sessions went without inflating it by their chunk count.
    derived_deleted: int = 0
    #: Dependent rows the database removes with this class (FK cascade), keyed
    #: by table name so the purge record names them. ``runtime_session_artifact``
    #: is the count of artifact rows a runtime-session batch will take with it.
    cascade_counts: dict[str, int] = field(default_factory=dict)
    #: True when the class still had matching rows when the pass stopped.
    more_remaining: bool = False
    applied_cutoffs: list[dict[str, Any]] = field(default_factory=list)

    def as_details(self) -> dict[str, Any]:
        """Audit-row shape."""
        return {
            "record_class": self.record_class,
            "retention_days": self.retention_days,
            "applied_cutoffs": list(self.applied_cutoffs),
            "cutoff": self.cutoff.isoformat(),
            "deleted": self.deleted,
            "derived_deleted": self.derived_deleted,
            "batches": self.batches,
            "more_remaining": self.more_remaining,
            **self.cascade_counts,
        }


@dataclass
class PurgeResult:
    """Totals for one pass across every account it reached."""

    accounts: int = 0
    deleted: int = 0
    classes: dict[str, int] = field(default_factory=dict)
    #: True when the pass ended on its time or batch budget, not on empty.
    budget_exhausted: bool = False
    skipped_reason: Optional[str] = None

    def record(self, result: ClassResult) -> None:
        """Fold one class result into the totals."""
        self.deleted += result.deleted
        self.classes[result.record_class] = (
            self.classes.get(result.record_class, 0) + result.deleted
        )

    def as_dict(self) -> dict[str, Any]:
        """Log/telemetry shape."""
        return {
            "accounts": self.accounts,
            "deleted": self.deleted,
            "classes": dict(self.classes),
            "budget_exhausted": self.budget_exhausted,
            "skipped_reason": self.skipped_reason,
        }


def parse_window(raw: Any) -> Optional[tuple[int, int]]:
    """Parse ``"start-end"`` UTC hours. ``None`` means any hour.

    A malformed window is treated as "any hour" and logged rather than
    silently pinning the job to a window nobody meant, because the failure
    mode of the alternative is a purge that never runs and a retention
    promise that is quietly untrue.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        start_text, end_text = text.split("-", 1)
        start, end = int(start_text), int(end_text)
    except (ValueError, AttributeError):
        logger.warning("Ignoring unparseable RETENTION_PURGE_WINDOW_UTC %r", raw)
        return None
    if not (0 <= start <= 23 and 0 <= end <= 24) or start == end:
        logger.warning("Ignoring out-of-range RETENTION_PURGE_WINDOW_UTC %r", raw)
        return None
    return start, end


def in_window(now: datetime, window: Optional[tuple[int, int]]) -> bool:
    """True when ``now`` is inside the half-open off-peak window."""
    if window is None:
        return True
    start, end = window
    hour = now.astimezone(UTC).hour
    if start < end:
        return start <= hour < end
    # Wraps midnight, for example 22-4.
    return hour >= start or hour < end


def _audit_cutoff_filters(cutoff: datetime):
    return (AuditLog.timestamp < cutoff,)


def _approval_cutoff_filters(cutoff: datetime):
    return (
        ApprovalRequest.requested_at < cutoff,
        ApprovalRequest.status != _UNRESOLVED_APPROVAL_STATUS,
    )


def _evidence_cutoff_filters(cutoff: datetime):
    return (
        FlowArtifact.created_at < cutoff,
        FlowArtifact.kind == "evidence",
    )


def _runtime_session_cutoff_filters(cutoff: datetime):
    # A session that never ended is dated by when it started. Using ended_at
    # alone would make an abandoned session immortal.
    return (
        func.greatest(
            RuntimeSession.ended_at,
            RuntimeSession.last_activity_at,
            RuntimeSession.started_at,
        )
        < cutoff,
    )


def _usage_cutoff_filters(cutoff: datetime):
    return (ApiUsage.timestamp < cutoff,)


_CLASS_MODELS: dict[str, Any] = {
    CLASS_AUDIT: AuditLog,
    CLASS_APPROVALS: ApprovalRequest,
    CLASS_EVIDENCE: FlowArtifact,
    CLASS_RUNTIME_SESSIONS: RuntimeSession,
    CLASS_USAGE: ApiUsage,
}

_CLASS_FILTERS = {
    CLASS_AUDIT: _audit_cutoff_filters,
    CLASS_APPROVALS: _approval_cutoff_filters,
    CLASS_EVIDENCE: _evidence_cutoff_filters,
    CLASS_RUNTIME_SESSIONS: _runtime_session_cutoff_filters,
    CLASS_USAGE: _usage_cutoff_filters,
}

#: Classes whose model carries no legal hold flag, and why. A class may only
#: be here on purpose: :func:`class_filters` adds the hold predicate to every
#: class whose model has the column, and the invariant test reads this mapping,
#: so a new purgeable class without hold coverage has to be written down here
#: rather than omitted by accident. That is the failure this mapping exists to
#: prevent: runtime sessions were purged with no hold check for months because
#: nothing made the omission visible (issue #650).
HOLD_EXEMPT_CLASSES: dict[str, str] = {
    CLASS_AUDIT: (
        "audit rows are the record of the hold itself and of the purge that "
        "removed them; they are chained and floored rather than held, and a "
        "hold on one resource does not freeze the whole account's log"
    ),
    CLASS_USAGE: (
        "usage rows carry no hold flag yet; holds over usage are a separate "
        "gap, deliberately out of scope here"
    ),
}

#: Models the purge writes that are not :data:`RECORD_CLASSES`, plus rows a
#: cascade removes with a class the purge does delete. They still go through
#: :func:`_hold_filters_for_model` so a hold cannot be skipped the way runtime
#: sessions were (issue #650).
#:
#: ``FlowExecution`` itself is never deleted; only leftover ``evidence_archive``
#: bytes are cleared.
#:
#: ``RuntimeSessionArtifact`` is deleted by the ``runtime_session`` foreign-key
#: cascade and kept by the session's hold flag. Listing it here makes a model
#: that carries ``legal_hold`` visible to the invariant test.
HOLD_SIDE_MODELS: tuple[Any, ...] = (FlowExecution, RuntimeSessionArtifact)


def _hold_filters_for_model(model: Any) -> list[Any]:
    """The legal hold predicate for one model, empty when it has no flag.

    Always a sequence of clauses so callers unpack the same type whether the
    model is held or has no flag. Shared by every RECORD_CLASS and by side
    statements such as the legacy evidence-column drop.
    """
    clauses: list[Any] = []
    flag = getattr(model, "legal_hold", None)
    if flag is not None:
        clauses.append(flag.is_(False))
    return clauses


def _hold_filters(record_class: str) -> list[Any]:
    """The legal hold predicate for one class, empty when it is exempt.

    Always a sequence of clauses so callers unpack the same type whether the
    class is held or exempt. Read off the model rather than repeated per
    class: the predicate cannot then be present on three classes and silently
    missing on the fourth, which is exactly how a held runtime session was
    purgeable.
    """
    return _hold_filters_for_model(_CLASS_MODELS[record_class])


def class_filters(record_class: str, cutoff: datetime):
    """Every predicate the purge applies to one class: cutoff plus hold."""
    return (*_CLASS_FILTERS[record_class](cutoff), *_hold_filters(record_class))


def _candidate_ids_stmt(
    record_class: str, *, account_id: Any, cutoff: datetime, limit: int
):
    """Ids of the next batch to remove for one class, account scoped."""
    model = _CLASS_MODELS[record_class]
    return (
        select(model.id)
        .where(model.account_id == account_id, *class_filters(record_class, cutoff))
        .order_by(model.id)
        .limit(limit)
    )


def count_purgeable(
    db: Session, *, account_id: Any, record_class: str, cutoff: datetime
) -> int:
    """How many rows the purge would remove. Used by dry runs and tests."""
    model = _CLASS_MODELS[record_class]
    return int(
        db.execute(
            select(func.count())
            .select_from(model)
            .where(
                model.account_id == account_id,
                *class_filters(record_class, cutoff),
            )
        ).scalar_one()
    )


def _expire_receipts_for_artifacts(
    db: Session, *, account_id: Any, artifact_ids: Sequence[Any]
) -> int:
    """Retire the execution receipts that name packs this batch removes.

    ``inspect_evidence`` resolves a terminal receipt through its stored
    ``artifact_id``. Deleting the row underneath and leaving the receipt as it
    was would answer ``failed`` / ``artifact_scope_mismatch``, which reads like
    corruption. It was retention, so the receipt says ``expired`` and carries
    the reason.
    """
    if not artifact_ids:
        return 0
    execution_ids = list(
        db.execute(
            select(FlowArtifact.execution_id).where(
                FlowArtifact.id.in_(list(artifact_ids)),
                FlowArtifact.account_id == account_id,
            )
        )
        .scalars()
        .all()
    )
    if not execution_ids:
        return 0
    wanted = {str(value) for value in artifact_ids}
    rows = (
        db.execute(
            select(FlowExecution).where(
                execution_in_account(account_id),
                FlowExecution.id.in_(execution_ids),
            )
        )
        .scalars()
        .all()
    )
    stamped = 0
    for execution in rows:
        receipt = execution.evidence_receipt
        if not isinstance(receipt, dict):
            continue
        if str(receipt.get("artifact_id") or "") not in wanted:
            continue
        updated = dict(receipt)
        updated["status"] = "expired"
        updated["error"] = "retention_purged"
        updated["legal_hold"] = False
        execution.evidence_receipt = updated
        db.add(execution)
        stamped += 1
    return stamped


def _drop_legacy_evidence_columns(
    db: Session, *, account_id: Any, cutoff: datetime
) -> int:
    """Clear pre-artifact evidence blobs on executions past the cutoff.

    Legacy captures live in ``flow_execution.evidence_archive`` rather than in
    a ``flow_artifact`` row, so the evidence class would otherwise purge the
    modern packs and leave the old bytes forever. Held executions are skipped.
    The execution row itself stays: it is a run record, not an evidence record.
    """
    result = db.execute(
        update(FlowExecution)
        .where(
            execution_in_account(account_id),
            FlowExecution.created_at < cutoff,
            *_hold_filters_for_model(FlowExecution),
            FlowExecution.evidence_archive.isnot(None),
        )
        .values(evidence_archive=None)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


def purge_class(
    db: Session,
    *,
    account: Account,
    record_class: str,
    now: datetime,
    batch_size: int,
    max_batches: int,
    dry_run: bool,
    deadline: Optional[float] = None,
) -> ClassResult:
    """Delete one record class for one account, in bounded batches."""
    account_id = account.id

    def effective_policy(current_account: Account) -> tuple[int, datetime]:
        setting = resolve_retention(
            current_account.meta_data, record_class=record_class
        )
        retained_days = setting.days
        if record_class in (CLASS_RUNTIME_SESSIONS, CLASS_USAGE):
            history_days = storage_history_days(db, account=current_account)
            if history_days is None:
                return -1, datetime.min.replace(tzinfo=UTC)
            retained_days = max(retained_days, history_days)
        return retained_days, now - timedelta(days=retained_days)

    retained_days, cutoff = effective_policy(account)
    result = ClassResult(
        record_class=record_class, retention_days=retained_days, cutoff=cutoff
    )
    model = _CLASS_MODELS[record_class]
    if dry_run:
        if retained_days == -1:
            return result
        result.deleted = count_purgeable(
            db, account_id=account_id, record_class=record_class, cutoff=cutoff
        )
        return result
    pruned_seq = 0
    for _ in range(max_batches):
        if deadline is not None and time.monotonic() >= deadline:
            result.more_remaining = True
            break
        # Each commit ends the prior lock. Re-read and re-lock before the
        # next batch so an intervening longer promise takes effect now.
        locked_account = lock_account_for_retention(db, account_id=account_id)
        if locked_account is None:
            db.commit()
            result.more_remaining = True
            break
        retained_days, cutoff = effective_policy(locked_account)
        result.retention_days = retained_days
        result.cutoff = cutoff
        if retained_days == -1:
            db.commit()
            break
        ids = list(
            db.execute(
                _candidate_ids_stmt(
                    record_class,
                    account_id=account_id,
                    cutoff=cutoff,
                    limit=batch_size,
                )
            )
            .scalars()
            .all()
        )
        if not ids:
            if record_class == CLASS_USAGE:
                # Held sessions keep gateway chunks after their usage rows go.
                # Those usage ids never appear in a later batch, so an empty
                # usage pass still reclaims released-hold orphans.
                result.derived_deleted += delete_derived_chunks(
                    db,
                    record_class=record_class,
                    ids=[],
                    account_id=account_id,
                )
            db.commit()
            break
        if record_class == CLASS_RUNTIME_SESSIONS:
            # Count before the DELETE. The artifact rows leave with the session
            # through ON DELETE CASCADE, so the purge record has to name them
            # here or the audit row only says how many sessions went.
            prior = result.cascade_counts.get("runtime_session_artifact", 0)
            result.cascade_counts["runtime_session_artifact"] = (
                prior + _count_cascading_session_artifacts(db, ids)
            )
        if record_class == CLASS_EVIDENCE:
            _expire_receipts_for_artifacts(db, account_id=account_id, artifact_ids=ids)
        if record_class == CLASS_AUDIT:
            # Read the chain positions before the rows go. Deleting the oldest
            # sealed rows is this job doing its job, and the chain verifier has
            # to be told so, or the next verification reports the purge as
            # tampering (issue #558).
            pruned_seq = max(pruned_seq, _max_chain_seq(db, ids))
        # Derived copies go in the same transaction as the rows they quote.
        # A later sweep would leave a window in which a search still answers
        # from a record the purge already deleted, and a purge that is only
        # eventually true is not a deletion (issue #655).
        result.derived_deleted += delete_derived_chunks(
            db, record_class=record_class, ids=ids, account_id=account_id
        )
        deleted = db.execute(
            delete(model)
            .where(model.id.in_(ids))
            .execution_options(synchronize_session=False)
        )
        # One transaction per batch. A long-running DELETE over a retention
        # backlog would hold locks on tables the request path writes to.
        db.commit()
        result.deleted += int(deleted.rowcount or 0)
        result.batches += 1
        result.applied_cutoffs.append(
            {"retention_days": retained_days, "cutoff": cutoff.isoformat()}
        )
        if len(ids) < batch_size:
            break
    else:
        result.more_remaining = True
    if record_class == CLASS_EVIDENCE:
        locked_account = lock_account_for_retention(db, account_id=account_id)
        if locked_account is None:
            db.commit()
            result.more_remaining = True
            return result
        _, cutoff = effective_policy(locked_account)
        cleared = _drop_legacy_evidence_columns(
            db, account_id=account_id, cutoff=cutoff
        )
        if cleared:
            db.commit()
            result.deleted += cleared
        else:
            db.commit()
    if pruned_seq:
        _raise_chain_floor(db, account_id=account_id, up_to_seq=pruned_seq, now=now)
    return result


def _count_cascading_session_artifacts(db: Session, session_ids: Sequence[Any]) -> int:
    """Artifact rows the ``runtime_session`` delete will cascade.

    Args:
        db: Database session.
        session_ids: Runtime session ids in the current purge batch.

    Returns:
        Matching ``runtime_session_artifact`` rows.
    """
    if not session_ids:
        return 0
    return int(
        db.execute(
            select(func.count())
            .select_from(RuntimeSessionArtifact)
            .where(RuntimeSessionArtifact.runtime_session_id.in_(list(session_ids)))
        ).scalar_one()
    )


def _max_chain_seq(db: Session, ids: Sequence[Any]) -> int:
    """Highest chain position among the audit rows about to be deleted."""
    value = db.execute(
        select(func.max(AuditLog.chain_seq)).where(AuditLog.id.in_(ids))
    ).scalar()
    return int(value or 0)


def _raise_chain_floor(
    db: Session, *, account_id: Any, up_to_seq: int, now: datetime
) -> None:
    """Record the purged prefix on the account's chain state.

    Failure here is logged, not raised: the rows are already gone and the
    purge succeeded. The cost of the failure is a verification that reports a
    gap at the bottom of the range until the next purge pass raises the floor.
    """
    try:
        from preloop.services.audit_chain import note_pruned

        note_pruned(db, account_id=account_id, up_to_seq=up_to_seq, now=now)
        db.commit()
    except Exception:
        db.rollback()
        logger.error(
            "Could not raise the audit chain floor for account %s to seq %s",
            account_id,
            up_to_seq,
            exc_info=True,
        )


def purge_account(
    db: Session,
    *,
    account: Account,
    now: datetime,
    batch_size: int,
    max_batches: int,
    dry_run: bool,
    deadline: Optional[float] = None,
    record_classes: Sequence[str] = RECORD_CLASSES,
) -> list[ClassResult]:
    """Run every record class for one account and audit what was removed."""
    results: list[ClassResult] = []
    for record_class in record_classes:
        try:
            result = purge_class(
                db,
                account=account,
                record_class=record_class,
                now=now,
                batch_size=batch_size,
                max_batches=max_batches,
                dry_run=dry_run,
                deadline=deadline,
            )
        except Exception:
            # One bad class must not stop the others, and must not take the
            # sweeper down. The failure is loud in the log and the class is
            # retried on the next pass.
            db.rollback()
            logger.error(
                "Retention purge failed for account %s class %s",
                account.id,
                record_class,
                exc_info=True,
            )
            continue
        results.append(result)
        if result.deleted:
            try:
                crud_audit_log.log_action(
                    db,
                    account_id=account.id,
                    user_id=None,
                    action=AUDIT_ACTION_PREVIEW if dry_run else AUDIT_ACTION_PURGE,
                    resource_type="retention",
                    resource_id=record_class,
                    status="success",
                    details=result.as_details(),
                )
            except Exception:
                db.rollback()
                logger.error(
                    "Retention purge audit row failed for account %s class %s",
                    account.id,
                    record_class,
                    exc_info=True,
                )
    return results


def run_retention_purge(
    db: Session,
    *,
    now: Optional[datetime] = None,
    account_ids: Optional[Sequence[Any]] = None,
    ignore_window: bool = False,
    ignore_enabled: bool = False,
) -> PurgeResult:
    """One bounded pass over every active account.

    Returns totals rather than raising: the caller is a background loop, and
    an exception per pass would be noise on top of the per class logging that
    already happened.
    """
    stamp = now or datetime.now(UTC)
    summary = PurgeResult()
    if not (ignore_enabled or settings.retention_purge_enabled):
        summary.skipped_reason = "disabled"
        return summary
    window = parse_window(settings.retention_purge_window_utc)
    if not ignore_window and not in_window(stamp, window):
        summary.skipped_reason = "outside_window"
        return summary
    dry_run = bool(settings.retention_purge_dry_run)
    batch_size = max(1, int(settings.retention_purge_batch_size))
    max_batches = max(1, int(settings.retention_purge_max_batches))
    deadline = time.monotonic() + max(1, int(settings.retention_purge_max_seconds))

    stmt = select(Account).where(Account.is_active.is_(True)).order_by(Account.id)
    if account_ids:
        stmt = stmt.where(Account.id.in_(list(account_ids)))
    accounts = list(db.execute(stmt).scalars().all())
    for account in accounts:
        if time.monotonic() >= deadline:
            summary.budget_exhausted = True
            break
        summary.accounts += 1
        for result in purge_account(
            db,
            account=account,
            now=stamp,
            batch_size=batch_size,
            max_batches=max_batches,
            dry_run=dry_run,
            deadline=deadline,
        ):
            summary.record(result)
            if result.more_remaining:
                summary.budget_exhausted = True
    if summary.deleted:
        logger.info(
            "Retention purge pass: %s",
            summary.as_dict(),
        )
    return summary


class RetentionPurgeSweeper:
    """Periodic asyncio purge pass, modeled on the optimization job sweeper.

    Started from the app lifespan on the API role only. The DB work is
    synchronous CRUD, so each pass runs in a thread and the event loop stays
    responsive. The pass itself decides whether it is inside the off-peak
    window, so the interval can stay short without the purge running at noon.
    """

    def __init__(self, check_interval_seconds: Optional[int] = None) -> None:
        self.check_interval = int(
            check_interval_seconds
            if check_interval_seconds is not None
            else settings.retention_purge_interval_seconds
        )
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the purge background task."""
        if self._running:
            logger.warning("Retention purge sweeper is already running")
            return
        if not background_passes_allowed():
            # See preloop.services.service_roles: a pod that relays model
            # responses does not also sweep every account.
            logger.info(
                "Retention purge sweeper not started for %s role.",
                current_service_role(),
            )
            return
        self._running = True
        self._task = asyncio.create_task(self._sweep_loop())
        logger.info(
            "Retention purge sweeper started (check_interval=%ss, window=%s)",
            self.check_interval,
            settings.retention_purge_window_utc or "any",
        )

    async def stop(self) -> None:
        """Stop the purge background task."""
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # Expected when stop() cancels the sweep loop task.
                pass

    async def _sweep_loop(self) -> None:
        """Wait one interval first, then purge on every tick.

        Unlike the optimization sweeper this does not run immediately on
        startup: nothing is being recovered, and a delete pass firing during
        every deploy would tie data loss to restart timing.
        """
        while self._running:
            try:
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break
            try:
                await asyncio.to_thread(self._sweep_once)
            except Exception:
                logger.error("Error in retention purge pass", exc_info=True)

    @staticmethod
    def _sweep_once() -> None:
        """One synchronous pass with its own session."""
        db = next(get_db_session())
        try:
            run_retention_purge(db)
        finally:
            db.close()


_sweeper_instance: Optional[RetentionPurgeSweeper] = None


def get_retention_purge_sweeper() -> RetentionPurgeSweeper:
    """Get or create the global retention purge sweeper."""
    global _sweeper_instance
    if _sweeper_instance is None:
        _sweeper_instance = RetentionPurgeSweeper()
    return _sweeper_instance
