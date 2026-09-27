"""CRUD for the GitHub Copilot usage import.

The connection row lives in ``copilot_import_connection``. Imported data lives
in ``provider_billing_snapshot`` with ``provider='copilot'`` and
``usage_source='imported'``; the read helpers here only ever select those
rows, so gateway usage, budgets and ingestion quota never see them.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Union

from sqlalchemy import Float, and_, cast, func
from sqlalchemy.orm import Session

from ..models.copilot_import import CopilotImportConnection
from ..models.provider_billing import ProviderBillingSnapshot
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

        Returns:
            The refreshed connection.
        """
        if error is None:
            connection.last_synced_at = synced_at
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

    def _copilot_rows(self, db: Session, *, account_id: Union[uuid.UUID, str]) -> Any:
        return db.query(ProviderBillingSnapshot).filter(
            ProviderBillingSnapshot.account_id == account_id,
            ProviderBillingSnapshot.provider == COPILOT_PROVIDER,
            ProviderBillingSnapshot.usage_source == IMPORTED_USAGE_SOURCE,
        )

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
    ) -> List[Dict[str, Any]]:
        """Sum premium-request spend per (user, model) over a window.

        Organization aggregate rows come back with ``user_login=None``.

        Returns:
            Dicts with ``user_login``, ``model``, ``net_amount``,
            ``net_quantity`` and ``days``.
        """
        net_quantity = cast(ProviderBillingSnapshot.raw["netQuantity"].astext, Float)
        rows = (
            db.query(
                ProviderBillingSnapshot.user_login.label("user_login"),
                ProviderBillingSnapshot.model.label("model"),
                func.sum(ProviderBillingSnapshot.cost_amount).label("net_amount"),
                func.sum(net_quantity).label("net_quantity"),
                func.count(func.distinct(ProviderBillingSnapshot.bucket_start)).label(
                    "days"
                ),
            )
            .filter(
                ProviderBillingSnapshot.account_id == account_id,
                ProviderBillingSnapshot.provider == COPILOT_PROVIDER,
                ProviderBillingSnapshot.usage_source == IMPORTED_USAGE_SOURCE,
                ProviderBillingSnapshot.line_item == LINE_ITEM_PREMIUM_REQUEST,
                ProviderBillingSnapshot.bucket_start >= start,
                ProviderBillingSnapshot.bucket_start < end,
            )
            .group_by(ProviderBillingSnapshot.user_login, ProviderBillingSnapshot.model)
            .all()
        )
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
    ) -> List[ProviderBillingSnapshot]:
        """List Copilot rows of one line item in a window, oldest first."""
        return (
            self._copilot_rows(db, account_id=account_id)
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
    ) -> Dict[str, Any]:
        """Return the newest seat summary and seat rows before ``before``.

        Returns:
            ``{"summary": row or None, "seats": [rows]}`` for the latest day
            that has a seat summary.
        """
        summary = (
            self._copilot_rows(db, account_id=account_id)
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
            self._copilot_rows(db, account_id=account_id)
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


crud_copilot_import_connection = CRUDCopilotImportConnection(CopilotImportConnection)
crud_copilot_usage = CRUDCopilotUsage(ProviderBillingSnapshot)
