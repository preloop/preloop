"""CRUD operations for self-hosted flow runners."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models.flow_runner import FlowRunner
from .base import CRUDBase

ONLINE_HEARTBEAT_TTL = timedelta(seconds=45)


class CRUDFlowRunner(CRUDBase[FlowRunner]):
    """CRUD helpers for FlowRunner."""

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
        if not pool:
            return rows
        return [row for row in rows if runner_matches_pool(row, pool)]


def runner_matches_pool(row: FlowRunner, pool: str) -> bool:
    """True when the runner id, name, or a label equals the pool string."""
    pool_l = (pool or "").strip().lower()
    if not pool_l:
        return True
    labels = [str(label).lower() for label in (row.labels or [])]
    return (
        str(row.id).lower() == pool_l
        or (row.name or "").lower() == pool_l
        or pool_l in labels
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


crud_flow_runner = CRUDFlowRunner(FlowRunner)
