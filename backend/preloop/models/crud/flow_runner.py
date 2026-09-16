"""CRUD operations for self-hosted flow runners."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from preloop.models import models

from .base import CRUDBase

FlowRunner = models.FlowRunner
FlowRunnerAssignment = models.FlowRunnerAssignment

ONLINE_HEARTBEAT_TTL = timedelta(seconds=45)


def runner_capacity(runner: models.FlowRunner) -> int:
    """Slots this runner may fill: the owner's ceiling, lowered by the process."""
    return int(getattr(runner, "capacity", models.DEFAULT_RUNNER_CONCURRENCY))


class CRUDFlowRunner(CRUDBase[FlowRunner]):
    """CRUD helpers for FlowRunner."""

    def get_fresh(self, db: Session, *, runner_id: UUID) -> Optional[models.FlowRunner]:
        """Reload committed runner state after a compare-and-swap update."""
        return (
            db.query(models.FlowRunner)
            .filter(models.FlowRunner.id == runner_id)
            .populate_existing()
            .first()
        )

    def set_publication_capabilities(
        self,
        db: Session,
        *,
        runner_id: UUID,
        capabilities: Dict[str, Any],
        expected_connection_id: Optional[str] = None,
        offline: bool = False,
        clear_lease: bool = False,
        execution_id: Optional[UUID] = None,
        commit: bool = True,
    ) -> bool:
        """CAS readiness changes so an old socket cannot clear a replacement.

        ``clear_lease`` releases one slot: the assignment named by
        ``execution_id``, or every assignment when no execution is named (an
        unregister). Freeing a slot makes the runner ``online`` again unless
        it is going ``offline``; a runner that still holds work stays busy.

        Args:
            db: Database session.
            runner_id: Runner to update.
            capabilities: Publication capability snapshot to store.
            expected_connection_id: Only act while this socket is the live one.
            offline: Mark the runner offline.
            clear_lease: Release the named assignment (or all of them).
            execution_id: Which assignment ``clear_lease`` releases.
            commit: Commit, or only flush for a caller that owns the transaction.

        Returns:
            True when the compare-and-swap matched and the update applied.
        """
        query = db.query(models.FlowRunner).filter(models.FlowRunner.id == runner_id)
        if expected_connection_id is not None:
            query = query.filter(
                models.FlowRunner.publication_capabilities["connection_id"].astext
                == expected_connection_id
            )
        if clear_lease and execution_id is not None:
            # The lease being cleared must still be this runner's, or an old
            # socket could release a slot a replacement already refilled.
            query = query.filter(
                models.FlowRunner.assignments.any(
                    models.FlowRunnerAssignment.execution_id == execution_id
                )
            )
        values: Dict[Any, Any] = {
            models.FlowRunner.publication_capabilities: capabilities
        }
        if clear_lease:
            values[models.FlowRunner.status] = "offline" if offline else "online"
        elif offline:
            values[models.FlowRunner.status] = "offline"
        updated = query.update(values, synchronize_session=False)
        if updated and clear_lease:
            assignments = db.query(models.FlowRunnerAssignment).filter(
                models.FlowRunnerAssignment.runner_id == runner_id
            )
            if execution_id is not None:
                assignments = assignments.filter(
                    models.FlowRunnerAssignment.execution_id == execution_id
                )
            assignments.delete(synchronize_session=False)
            db.expire_all()
            if not offline:
                self._sync_busy_status(db, runner_id=runner_id)
        if commit:
            db.commit()
        else:
            db.flush()
        return bool(updated)

    def _sync_busy_status(self, db: Session, *, runner_id: UUID) -> None:
        """Set ``busy``/``online`` from free slots, never from "has a job".

        The count comes from a query rather than the loaded relationship: the
        caller has usually just inserted or deleted an assignment, and a
        cached collection would answer about the previous state.
        """
        runner = db.get(models.FlowRunner, runner_id)
        if runner is None or runner.status == "offline":
            return
        used = (
            db.query(func.count(models.FlowRunnerAssignment.id))
            .filter(models.FlowRunnerAssignment.runner_id == runner_id)
            .scalar()
            or 0
        )
        runner.status = "busy" if int(used) >= runner_capacity(runner) else "online"
        db.add(runner)
        db.expire(runner, ["assignments"])

    def bind_publication_lease(
        self,
        db: Session,
        *,
        runner_id: UUID,
        execution_id: UUID,
        account_id: UUID,
        nonce: str,
    ) -> None:
        """Bind the runtime owner in the same transaction that exposes its job."""
        execution = (
            db.query(models.FlowExecution)
            .join(models.Flow, models.Flow.id == models.FlowExecution.flow_id)
            .filter(
                models.FlowExecution.id == execution_id,
                models.Flow.account_id == account_id,
            )
            .populate_existing()
            .with_for_update(of=models.FlowExecution)
            .first()
        )
        state = (
            (execution.result or {}).get("_private_publication") if execution else None
        )
        if (
            not isinstance(state, dict)
            or state.get("nonce") != nonce
            or state.get("phase") != "agent"
            or state.get("runner_id") not in {None, str(runner_id)}
        ):
            raise ValueError("Private publication lease binding is stale")
        execution.runner_id = runner_id
        execution.result = {
            **(execution.result or {}),
            "_private_publication": {**state, "runner_id": str(runner_id)},
        }
        db.add(execution)

    def save_publication_policy(
        self,
        db: Session,
        *,
        execution_id: UUID,
        account_id: UUID,
        state: Dict[str, Any],
    ) -> None:
        """Persist a secret-free controller snapshot once before leasing."""
        execution = (
            db.query(models.FlowExecution)
            .join(models.Flow, models.Flow.id == models.FlowExecution.flow_id)
            .filter(
                models.FlowExecution.id == execution_id,
                models.Flow.account_id == account_id,
            )
            .populate_existing()
            .with_for_update(of=models.FlowExecution)
            .first()
        )
        if execution is None:
            raise ValueError("Publication execution is not owned by this account")
        prior = (execution.result or {}).get("_private_publication")
        if prior:
            if prior.get("nonce") != state["nonce"]:
                raise ValueError("Publication policy is already bound")
            db.commit()
            return
        if execution.status not in {"PENDING", "STARTING", "RUNNING", "INITIALIZING"}:
            raise ValueError("Publication execution is no longer active")
        execution.result = {
            **(execution.result or {}),
            "_private_publication": deepcopy(state),
        }
        db.add(execution)
        db.commit()

    def publication_state(
        self,
        db: Session,
        *,
        runner_id: UUID,
        account_id: UUID,
        execution_id: UUID,
        nonce: str,
    ) -> Dict[str, Any]:
        """Read the live owner-bound publication state without retaining locks."""
        _, _, state = self._locked_publication(
            db,
            runner_id=runner_id,
            account_id=account_id,
            execution_id=execution_id,
            nonce=nonce,
        )
        result = deepcopy(state)
        db.commit()
        return result

    def _locked_publication(
        self,
        db: Session,
        *,
        runner_id: UUID,
        account_id: UUID,
        execution_id: UUID,
        nonce: str,
    ) -> tuple[models.FlowRunnerAssignment, models.FlowExecution, Dict[str, Any]]:
        """Lock and validate the current lease and account-owned execution."""
        runner = (
            db.query(models.FlowRunner)
            .filter(
                models.FlowRunner.id == runner_id,
                models.FlowRunner.account_id == account_id,
            )
            .populate_existing()
            .with_for_update()
            .first()
        )
        assignment = runner.assignment_for(execution_id) if runner is not None else None
        if runner is None or assignment is None or assignment.halt_requested:
            raise ValueError("Publication lease is stale or cancelled")
        execution = (
            db.query(models.FlowExecution)
            .join(models.Flow, models.Flow.id == models.FlowExecution.flow_id)
            .filter(
                models.FlowExecution.id == execution_id,
                models.Flow.account_id == account_id,
            )
            .populate_existing()
            .with_for_update(of=models.FlowExecution)
            .first()
        )
        if execution is None or execution.status not in {
            "PENDING",
            "STARTING",
            "RUNNING",
            "INITIALIZING",
        }:
            raise ValueError("Publication execution is no longer active")
        if execution.runner_id != runner_id:
            raise ValueError("Publication execution belongs to another runtime lease")
        state = (execution.result or {}).get("_private_publication")
        leased = (assignment.pending_job or {}).get("_publication")
        if (
            not isinstance(state, dict)
            or not isinstance(leased, dict)
            or state.get("nonce") != nonce
            or leased.get("nonce") != nonce
            or state.get("runner_id") not in {None, str(runner_id)}
            or state.get("policy", {}).get("account_id") != str(account_id)
            or state.get("policy", {}).get("execution_id") != str(execution_id)
        ):
            raise ValueError("Publication binding does not match the current lease")
        if (runner.publication_capabilities or {}).get("helper_ready") is not True:
            raise ValueError("Private publication helper is no longer ready")
        if state.get("connection_id") not in {
            None,
            (runner.publication_capabilities or {}).get("connection_id"),
        }:
            raise ValueError("Publication connection was replaced")
        if state.get("deadline", 0) <= datetime.now(timezone.utc).timestamp():
            raise ValueError("Publication deadline expired")
        return assignment, execution, state

    def transition_publication(
        self,
        db: Session,
        *,
        runner_id: UUID,
        account_id: UUID,
        execution_id: UUID,
        nonce: str,
        expected: Dict[str, Any],
        updated: Dict[str, Any],
        receipt: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Compare and consume a phase atomically before any writer is minted."""
        assignment, execution, state = self._locked_publication(
            db,
            runner_id=runner_id,
            account_id=account_id,
            execution_id=execution_id,
            nonce=nonce,
        )
        if state != expected:
            raise ValueError("Publication phase was already consumed")
        updated = {**deepcopy(updated), "runner_id": str(runner_id)}
        execution.result = {**(execution.result or {}), "_private_publication": updated}
        if receipt is not None:
            execution.result["trusted_publication"] = deepcopy(receipt)
        assignment.pending_job = {
            **(assignment.pending_job or {}),
            "_publication": updated,
        }
        db.add(execution)
        db.add(assignment)
        db.commit()
        return deepcopy(updated)

    def abandon_publication(
        self,
        db: Session,
        *,
        runner_id: UUID,
        execution_id: UUID,
        nonce: str,
    ) -> None:
        """Invalidate an interrupted controller phase without deleting recovery."""
        assignment = (
            db.query(models.FlowRunnerAssignment)
            .filter(
                models.FlowRunnerAssignment.runner_id == runner_id,
                models.FlowRunnerAssignment.execution_id == execution_id,
            )
            .populate_existing()
            .with_for_update()
            .first()
        )
        if assignment is None:
            db.commit()
            return
        execution = (
            db.query(models.FlowExecution)
            .filter(models.FlowExecution.id == execution_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
        state = (
            (execution.result or {}).get("_private_publication") if execution else None
        )
        if (
            isinstance(state, dict)
            and state.get("nonce") == nonce
            and state.get("phase") != "complete"
        ):
            state = {**state, "phase": "failed"}
            execution.result = {
                **(execution.result or {}),
                "_private_publication": state,
            }
            assignment.pending_job = {
                **(assignment.pending_job or {}),
                "_publication": state,
            }
            db.add(execution)
            db.add(assignment)
        db.commit()

    def get_by_token_hash(
        self, db: Session, *, token_hash: str
    ) -> Optional[FlowRunner]:
        return db.query(FlowRunner).filter(FlowRunner.token_hash == token_hash).first()

    def list_for_account(
        self,
        db: Session,
        *,
        account_id: UUID,
        skip: int = 0,
        limit: int = 100,
    ) -> List[FlowRunner]:
        return (
            db.query(FlowRunner)
            .filter(FlowRunner.account_id == account_id)
            .order_by(FlowRunner.last_heartbeat.desc().nullslast())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def find_matching(
        self,
        db: Session,
        *,
        account_id: UUID,
        pool: str,
        online_only: bool = True,
    ) -> List[FlowRunner]:
        """Runners whose id, name, or labels match the pool string.

        Ordered by free slots, most first, so a dispatcher that walks the
        list fills the emptiest machine before doubling up on a busy one.
        """
        pool = (pool or "").strip()
        query = db.query(FlowRunner).filter(FlowRunner.account_id == account_id)
        if online_only:
            cutoff = datetime.now(timezone.utc) - ONLINE_HEARTBEAT_TTL
            query = query.filter(
                FlowRunner.status.in_(("online", "busy")),
                FlowRunner.last_heartbeat.isnot(None),
                FlowRunner.last_heartbeat >= cutoff,
            )
        rows = query.all()
        pool_l = pool.lower()
        if pool_l == "server":
            return []
        if pool and pool_l != "auto":
            rows = [row for row in rows if runner_matches_pool(row, pool)]
        # Most free slots first, so work spreads over the machines instead of
        # stacking on whichever runner happens to be listed first. A runner
        # with no free slot is busy; it stays in the list so the caller can
        # still see it, but it sorts last and fails ``claim_free_slot``.
        rows.sort(key=lambda row: (-row.free_slots, str(row.id)))
        return rows

    def get_by_ids(self, db: Session, *, ids: List[UUID]) -> List[FlowRunner]:
        """Load many runners in one query.

        Empty ``ids`` returns an empty list. Duplicate ids are queried once.

        Args:
            db: Database session.
            ids: Runner primary keys to load.

        Returns:
            Matching rows (order not guaranteed).
        """
        unique = list(dict.fromkeys(ids))
        if not unique:
            return []
        return db.query(FlowRunner).filter(FlowRunner.id.in_(unique)).all()

    def claim_free_slot(self, db: Session, *, runner_id: UUID) -> Optional[FlowRunner]:
        """Lock one runner that still has a free slot.

        The runner row is the lock for its own slots: counting assignments
        under ``FOR UPDATE`` on the parent is what stops two dispatchers from
        handing out the same last slot. ``SKIP LOCKED`` lets the caller move
        on to the next match instead of queueing behind another worker.

        Args:
            db: Database session.
            runner_id: Runner to lock.

        Returns:
            The locked runner when it is online with at least one free slot,
            otherwise None.
        """
        runner = (
            db.query(FlowRunner)
            .filter(
                FlowRunner.id == runner_id,
                FlowRunner.status.in_(("online", "busy")),
            )
            .populate_existing()
            .with_for_update(skip_locked=True)
            .first()
        )
        if runner is None:
            return None
        used = (
            db.query(func.count(FlowRunnerAssignment.id))
            .filter(FlowRunnerAssignment.runner_id == runner_id)
            .scalar()
            or 0
        )
        if int(used) >= runner_capacity(runner):
            return None
        return runner

    def create_assignment(
        self,
        db: Session,
        *,
        runner_id: UUID,
        execution_id: UUID,
        pending_job: Optional[Dict[str, Any]] = None,
        commit: bool = True,
    ) -> models.FlowRunnerAssignment:
        """Fill one slot with one execution.

        Args:
            db: Database session.
            runner_id: Runner taking the work.
            execution_id: Execution being leased.
            pending_job: Payload the runner receives on delivery.
            commit: Commit, or only flush for a caller that owns the transaction.

        Returns:
            The stored assignment.
        """
        assignment = models.FlowRunnerAssignment(
            runner_id=runner_id,
            execution_id=execution_id,
            pending_job=pending_job,
            assigned_at=datetime.now(timezone.utc),
        )
        db.add(assignment)
        db.flush()
        self._sync_busy_status(db, runner_id=runner_id)
        if commit:
            db.commit()
            db.refresh(assignment)
        return assignment

    def get_assignment(
        self, db: Session, *, runner_id: UUID, execution_id: UUID
    ) -> Optional[models.FlowRunnerAssignment]:
        """One runner's assignment for one execution, or None."""
        return (
            db.query(FlowRunnerAssignment)
            .filter(
                FlowRunnerAssignment.runner_id == runner_id,
                FlowRunnerAssignment.execution_id == execution_id,
            )
            .first()
        )

    def get_assignment_by_execution(
        self, db: Session, *, execution_id: UUID
    ) -> Optional[models.FlowRunnerAssignment]:
        """Whichever runner holds this execution, if any."""
        return (
            db.query(FlowRunnerAssignment)
            .filter(FlowRunnerAssignment.execution_id == execution_id)
            .first()
        )

    def list_assignments(
        self, db: Session, *, runner_id: UUID
    ) -> List[models.FlowRunnerAssignment]:
        """Every execution this runner currently holds, oldest first."""
        return (
            db.query(FlowRunnerAssignment)
            .filter(FlowRunnerAssignment.runner_id == runner_id)
            .order_by(FlowRunnerAssignment.assigned_at)
            .all()
        )

    def release_assignment(
        self,
        db: Session,
        *,
        runner_id: UUID,
        execution_id: Optional[UUID] = None,
        commit: bool = True,
    ) -> int:
        """Free one slot, or every slot when no execution is named.

        Args:
            db: Database session.
            runner_id: Runner holding the work.
            execution_id: Execution to release; None releases all of them.
            commit: Commit, or only flush for a caller that owns the transaction.

        Returns:
            How many assignments were removed.
        """
        query = db.query(FlowRunnerAssignment).filter(
            FlowRunnerAssignment.runner_id == runner_id
        )
        if execution_id is not None:
            query = query.filter(FlowRunnerAssignment.execution_id == execution_id)
        removed = query.delete(synchronize_session=False)
        db.expire_all()
        if removed:
            self._sync_busy_status(db, runner_id=runner_id)
        if commit:
            db.commit()
        else:
            db.flush()
        return int(removed)

    def request_halt(self, db: Session, *, runner_id: UUID, execution_id: UUID) -> bool:
        """Ask the runner to stop one of its jobs.

        Halt is per assignment, not per runner: stopping one execution must
        not interrupt the other jobs sharing the machine.

        Args:
            db: Database session.
            runner_id: Runner holding the work.
            execution_id: Execution to halt.

        Returns:
            True when an assignment was marked.
        """
        updated = (
            db.query(FlowRunnerAssignment)
            .filter(
                FlowRunnerAssignment.runner_id == runner_id,
                FlowRunnerAssignment.execution_id == execution_id,
            )
            .update(
                {FlowRunnerAssignment.halt_requested: True},
                synchronize_session=False,
            )
        )
        db.commit()
        return bool(updated)

    def set_reported_status(
        self,
        db: Session,
        *,
        runner_id: UUID,
        execution_id: UUID,
        status: Optional[str],
        commit: bool = True,
    ) -> bool:
        """Store what the runner says one of its jobs is doing."""
        updated = (
            db.query(FlowRunnerAssignment)
            .filter(
                FlowRunnerAssignment.runner_id == runner_id,
                FlowRunnerAssignment.execution_id == execution_id,
            )
            .update(
                {FlowRunnerAssignment.reported_status: status},
                synchronize_session=False,
            )
        )
        if commit:
            db.commit()
        else:
            db.flush()
        return bool(updated)

    def set_concurrency(
        self,
        db: Session,
        *,
        runner: FlowRunner,
        concurrency: int,
    ) -> FlowRunner:
        """Set the owner's slot ceiling, clamped to the supported range.

        Args:
            db: Database session.
            runner: Runner to edit.
            concurrency: Requested ceiling.

        Returns:
            The refreshed runner.
        """
        runner.concurrency = max(
            1, min(models.MAX_RUNNER_CONCURRENCY, int(concurrency))
        )
        db.add(runner)
        db.commit()
        db.refresh(runner)
        if runner.status != "offline":
            self._sync_busy_status(db, runner_id=runner.id)
            db.commit()
            db.refresh(runner)
        return runner

    def set_reported_concurrency(
        self,
        db: Session,
        *,
        runner: FlowRunner,
        reported: Optional[int],
        commit: bool = True,
    ) -> FlowRunner:
        """Record what a connected runner process says it can run at once."""
        if reported is None:
            runner.reported_concurrency = None
        else:
            runner.reported_concurrency = max(
                1, min(models.MAX_RUNNER_CONCURRENCY, int(reported))
            )
        db.add(runner)
        if commit:
            db.commit()
            db.refresh(runner)
        else:
            db.flush()
        return runner

    def counts_for_instance(self, db: Session, *, instance_id: UUID) -> Dict[str, Any]:
        cutoff = datetime.now(timezone.utc) - ONLINE_HEARTBEAT_TTL
        total = (
            db.query(func.count(FlowRunner.id))
            .filter(FlowRunner.instance_id == instance_id)
            .scalar()
            or 0
        )
        online = (
            db.query(func.count(FlowRunner.id))
            .filter(
                FlowRunner.instance_id == instance_id,
                FlowRunner.status.in_(("online", "busy")),
                FlowRunner.last_heartbeat.isnot(None),
                FlowRunner.last_heartbeat >= cutoff,
            )
            .scalar()
            or 0
        )
        last = (
            db.query(func.max(FlowRunner.last_heartbeat))
            .filter(FlowRunner.instance_id == instance_id)
            .scalar()
        )
        return {
            "runner_count": int(total),
            "online_runner_count": int(online),
            "last_runner_heartbeat": last.isoformat() if last else None,
        }

    def counts_for_account(self, db: Session, *, account_id: UUID) -> Dict[str, Any]:
        cutoff = datetime.now(timezone.utc) - ONLINE_HEARTBEAT_TTL
        total = (
            db.query(func.count(FlowRunner.id))
            .filter(FlowRunner.account_id == account_id)
            .scalar()
            or 0
        )
        online = (
            db.query(func.count(FlowRunner.id))
            .filter(
                FlowRunner.account_id == account_id,
                FlowRunner.status.in_(("online", "busy")),
                FlowRunner.last_heartbeat.isnot(None),
                FlowRunner.last_heartbeat >= cutoff,
            )
            .scalar()
            or 0
        )
        last = (
            db.query(func.max(FlowRunner.last_heartbeat))
            .filter(FlowRunner.account_id == account_id)
            .scalar()
        )
        return {
            "runner_count": int(total),
            "online_runner_count": int(online),
            "last_runner_heartbeat": last.isoformat() if last else None,
        }

    def touch_heartbeat(
        self,
        db: Session,
        runner: FlowRunner,
        *,
        status: Optional[str] = None,
    ) -> FlowRunner:
        runner.last_heartbeat = datetime.now(timezone.utc)
        if status:
            runner.status = status
        elif runner.status == "offline":
            runner.status = "online"
        db.add(runner)
        db.commit()
        db.refresh(runner)
        return runner


def runner_matches_pool(row: FlowRunner, pool: str) -> bool:
    """True when the runner id, name, or a label equals the pool string."""
    pool_l = (pool or "").strip().lower()
    if not pool_l or pool_l == "auto":
        return True
    if pool_l == "server":
        return False
    labels = [str(label).lower() for label in (row.labels or [])]
    return (
        str(row.id).lower() == pool_l
        or (row.name or "").lower() == pool_l
        or pool_l in labels
    )


crud_flow_runner = CRUDFlowRunner(FlowRunner)
