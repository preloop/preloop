"""CRUD for the GitHub Copilot usage import.

The connection row lives in ``copilot_import_connection``. Imported data lives
in ``provider_billing_snapshot`` with ``provider='copilot'`` and
``usage_source='imported'``; the read helpers here only ever select those
rows, so gateway usage, budgets and ingestion quota never see them.

``copilot_user_mapping`` (#1061) maps canonical GitHub logins of the
connected organization to Preloop users of the same account. Every read
here is filtered by the connection's current organization and joins the user
row, so a mapping written for an earlier organization, for a user who has
since been deactivated, or (through a corrupt row) for another account's
user never contributes.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Union

from sqlalchemy import Float, and_, cast, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Query, Session

from ..models.copilot_import import CopilotImportConnection, CopilotUserMapping
from ..models.provider_billing import ProviderBillingSnapshot
from ..models.user import User
from .base import CRUDBase
from .provider_billing import (
    IMPORTED_USAGE_SOURCE,
    CRUDProviderBillingSnapshot,
    snapshot_dedup_key,
)

#: ``provider`` value on every Copilot snapshot row.
COPILOT_PROVIDER = "copilot"
#: Snapshot ``line_item`` values written by the import.
LINE_ITEM_PREMIUM_REQUEST = "premium_request"
LINE_ITEM_SEAT = "seat"
LINE_ITEM_SEAT_SUMMARY = "seat_summary"
LINE_ITEM_USAGE_METRICS = "usage_metrics"


def canonical_github_login(login: str) -> str:
    """The form a GitHub login is stored and compared in: trimmed, lowercase.

    GitHub treats logins case-insensitively, and the import stores whatever
    spelling GitHub returned, so both sides are canonicalised before they
    meet.

    Args:
        login: Login as typed or as imported.

    Returns:
        The canonical login.

    Raises:
        ValueError: The login is empty once trimmed.
    """
    canonical = (login or "").strip().lower()
    if not canonical:
        raise ValueError("GitHub login must not be empty")
    return canonical


class CRUDCopilotImportConnection(CRUDBase[CopilotImportConnection]):
    """CRUD operations for the per-account Copilot import connection."""

    def get_for_account(
        self, db: Session, *, account_id: Union[uuid.UUID, str]
    ) -> Optional[CopilotImportConnection]:
        """Return the account's connection, if one exists."""
        return (
            db.query(CopilotImportConnection)
            .filter(CopilotImportConnection.account_id == account_id)
            .first()
        )

    def list_active(self, db: Session) -> List[CopilotImportConnection]:
        """List every active connection (for the daily sync job)."""
        return (
            db.query(CopilotImportConnection)
            .filter(CopilotImportConnection.is_active.is_(True))
            .all()
        )

    def get_active_for_account(
        self, db: Session, *, account_id: Union[uuid.UUID, str]
    ) -> Optional[CopilotImportConnection]:
        """The account's connection when it exists and is active, else None.

        A paused or missing connection contributes nothing to spend alerts,
        so callers that evaluate imported spend read through this.
        """
        connection = self.get_for_account(db, account_id=account_id)
        if connection is None or not connection.is_active:
            return None
        return connection

    def list_active_account_ids(self, db: Session) -> List[uuid.UUID]:
        """Accounts with an active connection, in a stable order."""
        rows = (
            db.query(CopilotImportConnection.account_id)
            .filter(CopilotImportConnection.is_active.is_(True))
            .order_by(CopilotImportConnection.account_id)
            .all()
        )
        return [row.account_id for row in rows]

    def record_sync(
        self,
        db: Session,
        *,
        connection: CopilotImportConnection,
        synced_at: datetime,
        synced_day: Optional[date] = None,
        error: Optional[str] = None,
        per_user_billing_status: Optional[str] = None,
        per_user_billing_reason: Optional[str] = None,
        metrics_status: Optional[str] = None,
        metrics_reason: Optional[str] = None,
        warning: Optional[str] = None,
    ) -> CopilotImportConnection:
        """Persist the outcome of one sync attempt.

        Args:
            db: Database session.
            connection: Connection that was synced.
            synced_at: When the attempt finished.
            synced_day: Report day fully imported; left unchanged when None.
            error: Failure message; None clears a previous error.
            per_user_billing_status: ``available`` or ``unavailable``; left
                unchanged when None.
            per_user_billing_reason: Why per-user billing is unavailable.
            metrics_status: ``available`` or ``unavailable``; left unchanged
                when None.
            metrics_reason: Why the usage-metrics report is unavailable.
            warning: Non-fatal problem from a successful sync; None clears
                it. Left unchanged when the sync failed.

        Returns:
            The refreshed connection.
        """
        if error is None:
            connection.last_synced_at = synced_at
            connection.last_warning = warning
        connection.last_error = error
        if synced_day is not None:
            connection.last_synced_day = synced_day
        if per_user_billing_status is not None:
            connection.per_user_billing_status = per_user_billing_status
            connection.per_user_billing_reason = per_user_billing_reason
        if metrics_status is not None:
            connection.metrics_status = metrics_status
            connection.metrics_reason = metrics_reason
        db.add(connection)
        db.commit()
        db.refresh(connection)
        return connection


class CRUDCopilotUsage(CRUDProviderBillingSnapshot):
    """Writes and reads of Copilot rows in ``provider_billing_snapshot``."""

    def _copilot_rows(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        organization: Optional[str] = None,
    ) -> Any:
        query = db.query(ProviderBillingSnapshot).filter(
            ProviderBillingSnapshot.account_id == account_id,
            ProviderBillingSnapshot.provider == COPILOT_PROVIDER,
            ProviderBillingSnapshot.usage_source == IMPORTED_USAGE_SOURCE,
        )
        if organization is not None:
            query = query.filter(
                ProviderBillingSnapshot.project_or_workspace_id == organization
            )
        return query

    def replace_day_rows(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        bucket_start: datetime,
        line_items: Iterable[str],
        rows: List[Dict[str, Any]],
    ) -> int:
        """Make ``rows`` the complete set of rows for one day and line items.

        Rows for the same day and line items that are not in ``rows`` are
        deleted and the rest are upserted, in one transaction. A second sync
        for the same day therefore neither duplicates rows nor leaves a stale
        organization aggregate beside newly readable per-user rows (or the
        reverse), which would double count the day.

        Args:
            db: Database session.
            account_id: Owning account.
            bucket_start: UTC midnight of the report day.
            line_items: Line items this batch owns for the day.
            rows: Snapshot rows (``provider`` must be ``copilot``).

        Returns:
            Number of rows written.
        """
        keep = {
            snapshot_dedup_key(
                provider=row["provider"],
                granularity=row.get("granularity", "1d"),
                bucket_start=row["bucket_start"],
                model=row.get("model"),
                line_item=row.get("line_item"),
                provider_api_key_id=row.get("provider_api_key_id"),
                project_or_workspace_id=row.get("project_or_workspace_id"),
                service_tier=row.get("service_tier"),
                user_login=row.get("user_login"),
            )
            for row in rows
        }
        stale = self._copilot_rows(db, account_id=account_id).filter(
            ProviderBillingSnapshot.bucket_start == bucket_start,
            ProviderBillingSnapshot.line_item.in_(list(line_items)),
        )
        if keep:
            stale = stale.filter(ProviderBillingSnapshot.dedup_key.notin_(keep))
        stale.delete(synchronize_session=False)
        written = self.upsert_snapshots(
            db, account_id=account_id, rows=rows, commit=False
        )
        db.commit()
        return written

    def premium_request_totals(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        start: datetime,
        end: datetime,
        organization: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Sum premium-request spend per (user, model) over a window.

        Organization aggregate rows come back with ``user_login=None``.
        ``organization`` restricts the sum to rows imported for that
        organization, so switching the connection to another organization
        never mixes the two.

        Returns:
            Dicts with ``user_login``, ``model``, ``net_amount``,
            ``net_quantity`` and ``days``.
        """
        net_quantity = cast(ProviderBillingSnapshot.raw["netQuantity"].astext, Float)
        query = db.query(
            ProviderBillingSnapshot.user_login.label("user_login"),
            ProviderBillingSnapshot.model.label("model"),
            func.sum(ProviderBillingSnapshot.cost_amount).label("net_amount"),
            func.sum(net_quantity).label("net_quantity"),
            func.count(func.distinct(ProviderBillingSnapshot.bucket_start)).label(
                "days"
            ),
        ).filter(
            ProviderBillingSnapshot.account_id == account_id,
            ProviderBillingSnapshot.provider == COPILOT_PROVIDER,
            ProviderBillingSnapshot.usage_source == IMPORTED_USAGE_SOURCE,
            ProviderBillingSnapshot.line_item == LINE_ITEM_PREMIUM_REQUEST,
            ProviderBillingSnapshot.bucket_start >= start,
            ProviderBillingSnapshot.bucket_start < end,
        )
        if organization is not None:
            query = query.filter(
                ProviderBillingSnapshot.project_or_workspace_id == organization
            )
        rows = query.group_by(
            ProviderBillingSnapshot.user_login, ProviderBillingSnapshot.model
        ).all()
        return [
            {
                "user_login": row.user_login,
                "model": row.model,
                "net_amount": (
                    float(row.net_amount) if row.net_amount is not None else None
                ),
                "net_quantity": (
                    float(row.net_quantity) if row.net_quantity is not None else None
                ),
                "days": int(row.days or 0),
            }
            for row in rows
        ]

    def list_rows(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        line_item: str,
        start: datetime,
        end: datetime,
        organization: Optional[str] = None,
    ) -> List[ProviderBillingSnapshot]:
        """List Copilot rows of one line item in a window, oldest first.

        ``organization`` restricts the rows to that organization's imports.
        """
        return (
            self._copilot_rows(db, account_id=account_id, organization=organization)
            .filter(
                ProviderBillingSnapshot.line_item == line_item,
                ProviderBillingSnapshot.bucket_start >= start,
                ProviderBillingSnapshot.bucket_start < end,
            )
            .order_by(
                ProviderBillingSnapshot.bucket_start,
                ProviderBillingSnapshot.user_login,
            )
            .all()
        )

    def latest_seat_snapshot(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        before: datetime,
        organization: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return the newest seat summary and seat rows before ``before``.

        ``organization`` restricts the snapshot to that organization's imports.

        Returns:
            ``{"summary": row or None, "seats": [rows]}`` for the latest day
            that has a seat summary.
        """
        summary = (
            self._copilot_rows(db, account_id=account_id, organization=organization)
            .filter(
                ProviderBillingSnapshot.line_item == LINE_ITEM_SEAT_SUMMARY,
                ProviderBillingSnapshot.bucket_start < before,
            )
            .order_by(ProviderBillingSnapshot.bucket_start.desc())
            .first()
        )
        if summary is None:
            return {"summary": None, "seats": []}
        seats = (
            self._copilot_rows(db, account_id=account_id, organization=organization)
            .filter(
                and_(
                    ProviderBillingSnapshot.line_item == LINE_ITEM_SEAT,
                    ProviderBillingSnapshot.bucket_start == summary.bucket_start,
                )
            )
            .order_by(ProviderBillingSnapshot.user_login)
            .all()
        )
        return {"summary": summary, "seats": seats}


class CRUDCopilotUserMapping(CRUDBase[CopilotUserMapping]):
    """Operator-written mappings from GitHub logins to Preloop users.

    Every method takes the connection, not a bare account id, so the
    organization filter can never be forgotten: a mapping belongs to the
    account, the connection and the organization it was written for.
    """

    def _for_connection(
        self, db: Session, connection: CopilotImportConnection
    ) -> Query[CopilotUserMapping]:
        return db.query(CopilotUserMapping).filter(
            CopilotUserMapping.account_id == connection.account_id,
            CopilotUserMapping.connection_id == connection.id,
            CopilotUserMapping.organization == connection.organization,
        )

    def list_for_connection(
        self, db: Session, *, connection: CopilotImportConnection
    ) -> List[CopilotUserMapping]:
        """Mappings for the connection's current organization, by login."""
        return (
            self._for_connection(db, connection)
            .order_by(CopilotUserMapping.github_login)
            .all()
        )

    def get_for_login(
        self,
        db: Session,
        *,
        connection: CopilotImportConnection,
        github_login: str,
    ) -> Optional[CopilotUserMapping]:
        """The mapping for one canonical login, or None."""
        return (
            self._for_connection(db, connection)
            .filter(
                CopilotUserMapping.github_login == canonical_github_login(github_login)
            )
            .first()
        )

    def upsert(
        self,
        db: Session,
        *,
        connection: CopilotImportConnection,
        github_login: str,
        user_id: uuid.UUID,
        commit: bool = True,
    ) -> CopilotUserMapping:
        """Write the mapping for one login, replacing a previous target.

        ``INSERT ... ON CONFLICT DO UPDATE`` on the login's unique key, so
        the same login written twice (in any letter case) is one row whose
        user is the last one written.

        The caller validates the user (same account, active) before calling;
        this method only persists.

        Args:
            db: Database session.
            connection: The account's Copilot connection.
            github_login: Login in any spelling; stored canonical.
            user_id: Preloop user of the same account.
            commit: Commit when True, flush otherwise.

        Returns:
            The stored mapping.
        """
        login = canonical_github_login(github_login)
        statement = (
            pg_insert(CopilotUserMapping)
            .values(
                id=uuid.uuid4(),
                account_id=connection.account_id,
                connection_id=connection.id,
                organization=connection.organization,
                github_login=login,
                user_id=user_id,
            )
            .on_conflict_do_update(
                constraint="uq_copilot_user_mapping_login",
                set_={
                    "connection_id": connection.id,
                    "user_id": user_id,
                    "updated_at": func.now(),
                },
            )
        )
        db.execute(statement)
        if commit:
            db.commit()
        else:
            db.flush()
        mapping = self.get_for_login(db, connection=connection, github_login=login)
        assert mapping is not None  # the statement above guarantees the row
        db.refresh(mapping)
        return mapping

    def delete_for_login(
        self,
        db: Session,
        *,
        connection: CopilotImportConnection,
        github_login: str,
        commit: bool = True,
    ) -> int:
        """Remove the mapping for one login. Returns the rows removed (0 or 1)."""
        removed = (
            self._for_connection(db, connection)
            .filter(
                CopilotUserMapping.github_login == canonical_github_login(github_login)
            )
            .delete(synchronize_session=False)
        )
        if commit:
            db.commit()
        return int(removed)

    def resolve_user_ids(
        self, db: Session, *, connection: CopilotImportConnection
    ) -> Dict[str, uuid.UUID]:
        """Canonical login to user id for every mapping that may contribute.

        Joins the user row and keeps only active users of the connection's
        account, so a deactivated user, or a row that (through corruption)
        points at another account's user, resolves to nothing rather than to
        spend attributed to the wrong person.
        """
        rows = (
            self._for_connection(db, connection)
            .join(User, User.id == CopilotUserMapping.user_id)
            .filter(
                User.account_id == connection.account_id,
                User.is_active.is_(True),
            )
            .with_entities(CopilotUserMapping.github_login, CopilotUserMapping.user_id)
            .all()
        )
        return {row.github_login: row.user_id for row in rows}


crud_copilot_import_connection = CRUDCopilotImportConnection(CopilotImportConnection)
crud_copilot_usage = CRUDCopilotUsage(ProviderBillingSnapshot)
crud_copilot_user_mapping = CRUDCopilotUserMapping(CopilotUserMapping)
