"""CRUD operations for self-hosted flow runners."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import JSON, func, or_
from sqlalchemy.orm import Session

from preloop.models import models

from .base import CRUDBase

FlowRunner = models.FlowRunner

ONLINE_HEARTBEAT_TTL = timedelta(seconds=45)


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
        reported_status: Optional[str] = None,
    ) -> bool:
        """CAS readiness changes so an old socket cannot clear a replacement."""
        query = db.query(models.FlowRunner).filter(models.FlowRunner.id == runner_id)
        if expected_connection_id is not None:
            query = query.filter(
                models.FlowRunner.publication_capabilities["connection_id"].astext
                == expected_connection_id
            )
        if execution_id is not None:
            query = query.filter(models.FlowRunner.current_execution_id == execution_id)
        values: Dict[Any, Any] = {
            models.FlowRunner.publication_capabilities: capabilities
        }
        if clear_lease:
            values.update(
                {
                    models.FlowRunner.pending_job: None,
                    models.FlowRunner.current_execution_id: None,
                    models.FlowRunner.halt_requested: False,
                    models.FlowRunner.status: "offline" if offline else "online",
                }
            )
            if reported_status is not None:
                values[models.FlowRunner.reported_status] = reported_status
        elif offline:
            values[models.FlowRunner.status] = "offline"
        updated = query.update(values, synchronize_session=False)
        db.commit()
        return bool(updated)

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
        runner, execution, state = self._locked_publication(
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
    ) -> tuple[models.FlowRunner, models.FlowExecution, Dict[str, Any]]:
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
        if (
            runner is None
            or runner.current_execution_id != execution_id
            or runner.halt_requested
        ):
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
        leased = (runner.pending_job or {}).get("_publication")
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
        return runner, execution, state

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
        runner, execution, state = self._locked_publication(
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
        runner.pending_job = {**(runner.pending_job or {}), "_publication": updated}
        db.add(execution)
        db.add(runner)
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
        runner = (
            db.query(models.FlowRunner)
            .filter(models.FlowRunner.id == runner_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
        if runner is None or runner.current_execution_id != execution_id:
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
            runner.pending_job = {**(runner.pending_job or {}), "_publication": state}
            db.add(execution)
            db.add(runner)
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
        """Runners whose id, name, or labels match the pool string."""
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
        if not pool or pool_l == "auto":
            return rows
        if pool_l == "server":
            return []
        return [row for row in rows if runner_matches_pool(row, pool)]

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

    def claim_idle(self, db: Session, *, runner_id: UUID) -> Optional[FlowRunner]:
        """Lock one idle runner so concurrent leases cannot double-claim it.

        ``SKIP LOCKED`` lets the caller try the next match when another
        worker already holds this row.
        """
        return (
            db.query(FlowRunner)
            .filter(
                FlowRunner.id == runner_id,
                FlowRunner.status == "online",
                # JSONB stores an assigned Python None as JSON null by default.
                # Both representations mean there is no pending lease, including
                # rows cleared by the runner completion/error handlers.
                or_(
                    FlowRunner.pending_job.is_(None),
                    FlowRunner.pending_job == JSON.NULL,
                ),
            )
            .with_for_update(skip_locked=True)
            .first()
        )

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
