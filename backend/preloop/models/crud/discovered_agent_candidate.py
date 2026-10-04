"""CRUD for opt-in discovery reports and the account discovery salt."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import delete, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..models.discovered_agent_candidate import (
    CANDIDATE_STATUS_IGNORED,
    CANDIDATE_STATUS_NEW,
    CANDIDATE_STATUS_ONBOARDED,
    AccountDiscoverySalt,
    DiscoveredAgentCandidate,
)
from .base import CRUDBase

#: Candidates not reported for this long are deleted.
CANDIDATE_RETENTION_DAYS = 90


def utc_now_naive() -> datetime:
    """Current UTC time without tzinfo, matching the naive timestamp columns."""
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class ReportedCandidate:
    """One candidate as the API layer validated it."""

    agent_kind: str
    config_path_hash: str
    agent_version: Optional[str] = None
    mcp_server_count: int = 0
    enrolled: bool = False


@dataclass(frozen=True)
class UpsertOutcome:
    """Result of recording one reported candidate."""

    candidate: DiscoveredAgentCandidate
    created: bool


class CRUDAccountDiscoverySalt(CRUDBase[AccountDiscoverySalt]):
    """Issue and read the per-account discovery salt."""

    def get_or_create(self, db: Session, *, account_id: Any) -> str:
        """Return the account's salt, creating it on first use.

        Concurrent first calls are safe: the insert is a no-op on conflict and
        every caller then reads the one row that won.

        Args:
            db: Active session. Commits when it creates the salt.
            account_id: Owning account.

        Returns:
            The salt as 64 hex characters.
        """
        existing = db.scalar(
            select(self.model.salt).where(self.model.account_id == account_id)
        )
        if existing:
            return existing
        db.execute(
            pg_insert(self.model)
            .values(
                id=uuid.uuid4(),
                account_id=account_id,
                salt=secrets.token_hex(32),
            )
            .on_conflict_do_nothing(index_elements=["account_id"])
        )
        db.commit()
        salt = db.scalar(
            select(self.model.salt).where(self.model.account_id == account_id)
        )
        if not salt:
            raise RuntimeError("Discovery salt was not persisted")
        return salt


class CRUDDiscoveredAgentCandidate(CRUDBase[DiscoveredAgentCandidate]):
    """Record, list, link and purge discovered agent candidates."""

    def record_report(
        self,
        db: Session,
        *,
        account_id: Any,
        workstation_fingerprint: str,
        os_family: Optional[str],
        cli_version: Optional[str],
        candidates: Iterable[ReportedCandidate],
        now: Optional[datetime] = None,
    ) -> list[UpsertOutcome]:
        """Insert new candidates and bump ``last_seen_at`` on known ones.

        A re-report changes nothing but ``last_seen_at``: status, managed
        agent link and the first report's details stay as they were. The
        ``xmax = 0`` test tells a fresh insert from a conflict update in the
        same statement, so two reports racing for one key still produce
        exactly one ``created`` outcome.

        Args:
            db: Active session. The caller commits.
            account_id: Owning account.
            workstation_fingerprint: Salted workstation hash.
            os_family: OS family reported by the CLI.
            cli_version: CLI version reported.
            candidates: Validated candidates.
            now: Override for the report time (tests).

        Returns:
            One outcome per distinct candidate key, in input order.
        """
        seen_at = now or utc_now_naive()
        outcomes: list[UpsertOutcome] = []
        seen_keys: set[tuple[str, str]] = set()
        for item in candidates:
            key = (item.agent_kind, item.config_path_hash)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            stmt = (
                pg_insert(self.model)
                .values(
                    id=uuid.uuid4(),
                    account_id=account_id,
                    workstation_fingerprint=workstation_fingerprint,
                    agent_kind=item.agent_kind,
                    config_path_hash=item.config_path_hash,
                    agent_version=item.agent_version,
                    mcp_server_count=item.mcp_server_count,
                    reported_enrolled=item.enrolled,
                    os_family=os_family,
                    cli_version=cli_version,
                    status=CANDIDATE_STATUS_NEW,
                    first_seen_at=seen_at,
                    last_seen_at=seen_at,
                )
                .on_conflict_do_update(
                    constraint="uq_discovered_agent_candidate_key",
                    set_={
                        "last_seen_at": func.greatest(
                            self.model.last_seen_at,
                            pg_insert(self.model).excluded.last_seen_at,
                        )
                    },
                )
                .returning(
                    self.model.id,
                    (literal_column("xmax") == 0).label("inserted"),
                )
            )
            row = db.execute(stmt).one()
            candidate = db.get(self.model, row.id)
            if candidate is not None:
                outcomes.append(
                    UpsertOutcome(candidate=candidate, created=bool(row.inserted))
                )
        return outcomes

    def list_for_account(
        self,
        db: Session,
        *,
        account_id: Any,
        statuses: Optional[Sequence[str]] = None,
        limit: int = 500,
    ) -> list[DiscoveredAgentCandidate]:
        """Candidates for one account, most recently seen first."""
        stmt = select(self.model).where(self.model.account_id == account_id)
        if statuses:
            stmt = stmt.where(self.model.status.in_(list(statuses)))
        stmt = stmt.order_by(self.model.last_seen_at.desc()).limit(limit)
        return list(db.scalars(stmt).all())

    def get_for_account(
        self, db: Session, *, account_id: Any, candidate_id: Any
    ) -> Optional[DiscoveredAgentCandidate]:
        """One candidate, only if it belongs to ``account_id``."""
        return db.scalar(
            select(self.model).where(
                self.model.account_id == account_id,
                self.model.id == candidate_id,
            )
        )

    def set_status(
        self,
        db: Session,
        *,
        account_id: Any,
        candidate_id: Any,
        status: str,
    ) -> Optional[DiscoveredAgentCandidate]:
        """Mark a candidate ignored, or return an ignored one to ``new``.

        ``onboarded`` is only set by enrollment linking, so it is not
        accepted here.
        """
        if status not in (CANDIDATE_STATUS_NEW, CANDIDATE_STATUS_IGNORED):
            raise ValueError(f"Unsupported candidate status: {status}")
        candidate = self.get_for_account(
            db, account_id=account_id, candidate_id=candidate_id
        )
        if candidate is None:
            return None
        candidate.status = status
        db.commit()
        db.refresh(candidate)
        return candidate

    def link_onboarded(
        self,
        db: Session,
        *,
        account_id: Any,
        workstation_fingerprint: str,
        agent_kind: str,
        managed_agent_id: Any,
        config_path_hash: Optional[str] = None,
    ) -> int:
        """Point matching candidates at a managed agent and mark them onboarded.

        Matches on workstation and kind, and on the config path hash when the
        caller has one, so one workstation running two installs of a kind
        links only the one that was enrolled.

        Returns:
            Number of candidates linked. The caller commits.
        """
        stmt = (
            update(self.model)
            .where(
                self.model.account_id == account_id,
                self.model.workstation_fingerprint == workstation_fingerprint,
                self.model.agent_kind == agent_kind,
            )
            .values(
                status=CANDIDATE_STATUS_ONBOARDED,
                managed_agent_id=managed_agent_id,
            )
        )
        if config_path_hash:
            stmt = stmt.where(self.model.config_path_hash == config_path_hash)
        result = db.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)

    def purge_stale(
        self,
        db: Session,
        *,
        now: Optional[datetime] = None,
        retention_days: int = CANDIDATE_RETENTION_DAYS,
        account_id: Optional[Any] = None,
    ) -> int:
        """Delete candidates whose ``last_seen_at`` is older than the window.

        Args:
            db: Active session. Commits.
            now: Reference time (tests).
            retention_days: Window length.
            account_id: Limit to one account; all accounts when None.

        Returns:
            Rows deleted.
        """
        cutoff = (now or utc_now_naive()) - timedelta(days=retention_days)
        stmt = delete(self.model).where(self.model.last_seen_at < cutoff)
        if account_id is not None:
            stmt = stmt.where(self.model.account_id == account_id)
        result = db.execute(stmt)
        db.commit()
        return int(getattr(result, "rowcount", 0) or 0)


__all__ = [
    "CANDIDATE_RETENTION_DAYS",
    "CANDIDATE_STATUS_IGNORED",
    "CANDIDATE_STATUS_NEW",
    "CANDIDATE_STATUS_ONBOARDED",
    "CRUDAccountDiscoverySalt",
    "CRUDDiscoveredAgentCandidate",
    "ReportedCandidate",
    "UpsertOutcome",
    "utc_now_naive",
]
