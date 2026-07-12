"""CRUD operations for provider billing connections and snapshots."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..models.provider_billing import (
    ProviderBillingConnection,
    ProviderBillingSnapshot,
)
from .base import CRUDBase


def snapshot_dedup_key(
    *,
    provider: str,
    granularity: str,
    bucket_start: datetime,
    model: Optional[str],
    line_item: Optional[str],
    provider_api_key_id: Optional[str],
    project_or_workspace_id: Optional[str],
    service_tier: Optional[str],
) -> str:
    """Deterministic identity for one provider billing bucket."""
    parts = "|".join(
        str(part or "")
        for part in (
            provider,
            granularity,
            bucket_start.isoformat(),
            model,
            line_item,
            provider_api_key_id,
            project_or_workspace_id,
            service_tier,
        )
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


class CRUDProviderBillingConnection(CRUDBase[ProviderBillingConnection]):
    """CRUD operations for provider billing connections."""

    def list_for_account(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        active_only: bool = False,
    ) -> List[ProviderBillingConnection]:
        """List an account's provider billing connections."""
        query = db.query(ProviderBillingConnection).filter(
            ProviderBillingConnection.account_id == account_id
        )
        if active_only:
            query = query.filter(ProviderBillingConnection.is_active.is_(True))
        return query.order_by(ProviderBillingConnection.provider).all()

    def list_active(self, db: Session) -> List[ProviderBillingConnection]:
        """List all active connections across accounts (for the sync job)."""
        return (
            db.query(ProviderBillingConnection)
            .filter(ProviderBillingConnection.is_active.is_(True))
            .all()
        )

    def get_for_account(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        connection_id: Union[uuid.UUID, str],
    ) -> Optional[ProviderBillingConnection]:
        """Fetch one connection scoped to an account."""
        return (
            db.query(ProviderBillingConnection)
            .filter(
                ProviderBillingConnection.id == connection_id,
                ProviderBillingConnection.account_id == account_id,
            )
            .first()
        )

    def mark_synced(
        self,
        db: Session,
        *,
        connection: ProviderBillingConnection,
        synced_at: datetime,
        error: Optional[str] = None,
    ) -> ProviderBillingConnection:
        """Record the outcome of a sync attempt."""
        if error is None:
            connection.last_synced_at = synced_at
        connection.last_error = error
        db.add(connection)
        db.commit()
        db.refresh(connection)
        return connection


class CRUDProviderBillingSnapshot(CRUDBase[ProviderBillingSnapshot]):
    """CRUD operations for provider billing snapshots."""

    def upsert_snapshots(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        rows: List[Dict[str, Any]],
    ) -> int:
        """Idempotently insert/update snapshot rows in one batched statement.

        Each row dict must contain the snapshot columns except ``account_id``
        and ``dedup_key`` (computed here). Duplicate ``dedup_key`` values
        within ``rows`` are collapsed last-wins before the upsert.

        Returns:
            Number of logical rows written.
        """
        if not rows:
            return 0

        values_by_dedup: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            dedup_key = snapshot_dedup_key(
                provider=row["provider"],
                granularity=row.get("granularity", "1d"),
                bucket_start=row["bucket_start"],
                model=row.get("model"),
                line_item=row.get("line_item"),
                provider_api_key_id=row.get("provider_api_key_id"),
                project_or_workspace_id=row.get("project_or_workspace_id"),
                service_tier=row.get("service_tier"),
            )
            values_by_dedup[dedup_key] = {
                "id": uuid.uuid4(),
                "account_id": account_id,
                "dedup_key": dedup_key,
                "granularity": row.get("granularity", "1d"),
                "currency": (row.get("currency") or "USD").upper(),
                **{
                    key: row.get(key)
                    for key in (
                        "provider",
                        "bucket_start",
                        "bucket_end",
                        "model",
                        "line_item",
                        "provider_api_key_id",
                        "project_or_workspace_id",
                        "service_tier",
                        "cost_amount",
                        "uncached_input_tokens",
                        "cached_input_tokens",
                        "cache_creation_tokens",
                        "output_tokens",
                        "raw",
                        "fetched_at",
                    )
                },
            }

        values_list = list(values_by_dedup.values())
        statement = pg_insert(ProviderBillingSnapshot).values(values_list)
        statement = statement.on_conflict_do_update(
            constraint="uq_provider_billing_snapshot_dedup",
            set_={
                "cost_amount": statement.excluded.cost_amount,
                "currency": statement.excluded.currency,
                "uncached_input_tokens": statement.excluded.uncached_input_tokens,
                "cached_input_tokens": statement.excluded.cached_input_tokens,
                "cache_creation_tokens": statement.excluded.cache_creation_tokens,
                "output_tokens": statement.excluded.output_tokens,
                "bucket_end": statement.excluded.bucket_end,
                "raw": statement.excluded.raw,
                "fetched_at": statement.excluded.fetched_at,
                "updated_at": func.now(),
            },
        )
        db.execute(statement)
        db.commit()
        return len(values_list)

    def aggregate_actuals_by_provider_day(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        start: datetime,
        end: datetime,
        provider: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Aggregate provider-reported cost/tokens per provider and day."""
        query = db.query(
            ProviderBillingSnapshot.provider.label("provider"),
            func.date_trunc("day", ProviderBillingSnapshot.bucket_start).label("day"),
            func.coalesce(func.sum(ProviderBillingSnapshot.cost_amount), 0.0).label(
                "cost_amount"
            ),
            func.coalesce(
                func.sum(ProviderBillingSnapshot.uncached_input_tokens), 0
            ).label("uncached_input_tokens"),
            func.coalesce(
                func.sum(ProviderBillingSnapshot.cached_input_tokens), 0
            ).label("cached_input_tokens"),
            func.coalesce(
                func.sum(ProviderBillingSnapshot.cache_creation_tokens), 0
            ).label("cache_creation_tokens"),
            func.coalesce(func.sum(ProviderBillingSnapshot.output_tokens), 0).label(
                "output_tokens"
            ),
        ).filter(
            ProviderBillingSnapshot.account_id == account_id,
            ProviderBillingSnapshot.bucket_start >= start,
            ProviderBillingSnapshot.bucket_start < end,
        )
        if provider:
            query = query.filter(ProviderBillingSnapshot.provider == provider)
        rows = query.group_by("provider", "day").order_by("provider", "day").all()
        return [
            {
                "provider": row.provider,
                "day": row.day,
                "cost_amount": float(row.cost_amount or 0.0),
                "uncached_input_tokens": int(row.uncached_input_tokens or 0),
                "cached_input_tokens": int(row.cached_input_tokens or 0),
                "cache_creation_tokens": int(row.cache_creation_tokens or 0),
                "output_tokens": int(row.output_tokens or 0),
            }
            for row in rows
        ]
