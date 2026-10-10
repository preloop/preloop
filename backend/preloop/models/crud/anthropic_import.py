"""CRUD for the Anthropic usage import (#1413).

The connection row lives in ``anthropic_import_connection``. Imported data
lives in ``provider_billing_snapshot`` with ``provider='anthropic_cc'`` (Claude
Code Analytics) and ``usage_source='imported'``; the read helpers here only
ever select those rows, so gateway usage, budgets and ingestion quota never
see them.

Identity reads (gateway subject and member by email) are read only: nothing
here creates users or subjects.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Union

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Query, Session

from ..models.ai_model import AIModel
from ..models.anthropic_import import AnthropicImportConnection, AnthropicUserMapping
from ..models.gateway_subject import GatewaySubject
from ..models.provider_billing import ProviderBillingSnapshot
from ..models.user import User
from .base import CRUDBase
from .provider_billing import (
    IMPORTED_USAGE_SOURCE,
    CRUDProviderBillingSnapshot,
    snapshot_dedup_key,
)

#: ``provider`` value on every Claude Code Analytics snapshot row.
ANTHROPIC_CC_PROVIDER = "anthropic_cc"
#: Snapshot ``line_item`` written by the Claude Code Analytics import.
LINE_ITEM_CLAUDE_CODE_ANALYTICS = "claude_code_analytics"
#: Prefix on ``user_login`` for API key actors.
API_KEY_ACTOR_PREFIX = "key:"


def canonical_actor(actor: str) -> str:
    """Canonical actor: trimmed; emails lowercased; ``key:`` names kept as is.

    Raises:
        ValueError: The actor is empty once trimmed.
    """
    value = (actor or "").strip()
    if value.lower().startswith(API_KEY_ACTOR_PREFIX):
        name = value[len(API_KEY_ACTOR_PREFIX) :].strip()
        if not name:
            raise ValueError("API key actor must name a key")
        return f"{API_KEY_ACTOR_PREFIX}{name}"
    if not value:
        raise ValueError("Actor must not be empty")
    return value.lower()


class CRUDAnthropicImportConnection(CRUDBase[AnthropicImportConnection]):
    """CRUD operations for the per-account Anthropic import connection."""

    def get_for_account(
        self, db: Session, *, account_id: Union[uuid.UUID, str]
    ) -> Optional[AnthropicImportConnection]:
        """Return the account's connection, if one exists."""
        return (
            db.query(AnthropicImportConnection)
            .filter(AnthropicImportConnection.account_id == account_id)
            .first()
        )

    def list_active(self, db: Session) -> List[AnthropicImportConnection]:
        """List every active connection (for the daily sync job)."""
        return (
            db.query(AnthropicImportConnection)
            .filter(AnthropicImportConnection.is_active.is_(True))
            .all()
        )

    def record_sync(
        self,
        db: Session,
        *,
        connection: AnthropicImportConnection,
        synced_at: datetime,
        synced_day: Optional[date] = None,
        error: Optional[str] = None,
        warning: Optional[str] = None,
    ) -> AnthropicImportConnection:
        """Persist the outcome of one sync attempt.

        A failed attempt keeps ``last_synced_at`` and the previous warning;
        ``synced_day`` is only advanced when given.
        """
        if error is None:
            connection.last_synced_at = synced_at
            connection.last_warning = warning
        connection.last_error = error
        if synced_day is not None:
            connection.last_synced_day = synced_day
        db.add(connection)
        db.commit()
        db.refresh(connection)
        return connection

    @staticmethod
    def list_anthropic_upstream_models(
        db: Session, *, account_id: Union[uuid.UUID, str]
    ) -> List[AIModel]:
        """The account's own Anthropic models (the gateway's upstream keys)."""
        return (
            db.query(AIModel)
            .filter(
                AIModel.account_id == account_id,
                func.lower(AIModel.provider_name) == "anthropic",
            )
            .all()
        )


class CRUDAnthropicUsage(CRUDProviderBillingSnapshot):
    """Writes and reads of Anthropic imported rows in the snapshot table."""

    def _rows(
        self, db: Session, *, account_id: Union[uuid.UUID, str], provider: str
    ) -> Any:
        return db.query(ProviderBillingSnapshot).filter(
            ProviderBillingSnapshot.account_id == account_id,
            ProviderBillingSnapshot.provider == provider,
            ProviderBillingSnapshot.usage_source == IMPORTED_USAGE_SOURCE,
        )

    def replace_day_rows(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        provider: str,
        bucket_start: datetime,
        line_items: Iterable[str],
        rows: List[Dict[str, Any]],
    ) -> int:
        """Make ``rows`` the complete set of one day's rows for ``line_items``.

        Stale rows of the same day are deleted and the rest upserted on the
        snapshot dedup key, in one transaction, so re-running a day
        overwrites and never adds.

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
        stale = self._rows(db, account_id=account_id, provider=provider).filter(
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

    def list_rows(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        provider: str,
        start: datetime,
        end: datetime,
    ) -> List[ProviderBillingSnapshot]:
        """Imported rows of one provider in ``[start, end)``, oldest first."""
        return (
            self._rows(db, account_id=account_id, provider=provider)
            .filter(
                ProviderBillingSnapshot.bucket_start >= start,
                ProviderBillingSnapshot.bucket_start < end,
            )
            .order_by(
                ProviderBillingSnapshot.bucket_start,
                ProviderBillingSnapshot.user_login,
                ProviderBillingSnapshot.model,
            )
            .all()
        )

    @staticmethod
    def subjects_by_email(
        db: Session, *, account_id: Union[uuid.UUID, str], emails: Iterable[str]
    ) -> Dict[str, GatewaySubject]:
        """Existing gateway subjects of the account keyed by lowercased email.

        Read only. When several subjects share an email the most recently
        seen one wins.
        """
        wanted = sorted({email.lower() for email in emails if email})
        if not wanted:
            return {}
        rows = db.execute(
            select(GatewaySubject)
            .where(
                GatewaySubject.account_id == account_id,
                func.lower(GatewaySubject.email).in_(wanted),
            )
            .order_by(GatewaySubject.last_seen_at.asc())
        ).scalars()
        return {subject.email.lower(): subject for subject in rows if subject.email}


class CRUDAnthropicUserMapping(CRUDBase[AnthropicUserMapping]):
    """Operator-written mappings from imported actors to Preloop users."""

    def _for_connection(
        self, db: Session, connection: AnthropicImportConnection
    ) -> Query[AnthropicUserMapping]:
        return db.query(AnthropicUserMapping).filter(
            AnthropicUserMapping.account_id == connection.account_id,
        )

    def list_for_connection(
        self, db: Session, *, connection: AnthropicImportConnection
    ) -> List[AnthropicUserMapping]:
        """Every mapping of the account, by actor."""
        return (
            self._for_connection(db, connection)
            .order_by(AnthropicUserMapping.actor)
            .all()
        )

    def get_for_actor(
        self, db: Session, *, connection: AnthropicImportConnection, actor: str
    ) -> Optional[AnthropicUserMapping]:
        """The mapping for one actor, or None."""
        return (
            self._for_connection(db, connection)
            .filter(AnthropicUserMapping.actor == canonical_actor(actor))
            .first()
        )

    def upsert(
        self,
        db: Session,
        *,
        connection: AnthropicImportConnection,
        actor: str,
        user_id: uuid.UUID,
    ) -> AnthropicUserMapping:
        """Write the mapping for one actor, replacing a previous target.

        The caller validates the user (same account, active) first.
        """
        canonical = canonical_actor(actor)
        statement = (
            pg_insert(AnthropicUserMapping)
            .values(
                id=uuid.uuid4(),
                account_id=connection.account_id,
                connection_id=connection.id,
                actor=canonical,
                user_id=user_id,
            )
            .on_conflict_do_update(
                constraint="uq_anthropic_user_mapping_actor",
                set_={
                    "connection_id": connection.id,
                    "user_id": user_id,
                    "updated_at": func.now(),
                },
            )
        )
        db.execute(statement)
        db.commit()
        mapping = self.get_for_actor(db, connection=connection, actor=canonical)
        assert mapping is not None  # the statement above guarantees the row
        db.refresh(mapping)
        return mapping

    def delete_for_actor(
        self, db: Session, *, connection: AnthropicImportConnection, actor: str
    ) -> int:
        """Remove one actor's mapping. Returns the rows removed (0 or 1)."""
        removed = (
            self._for_connection(db, connection)
            .filter(AnthropicUserMapping.actor == canonical_actor(actor))
            .delete(synchronize_session=False)
        )
        db.commit()
        return int(removed)

    def resolve_user_ids(
        self, db: Session, *, connection: AnthropicImportConnection
    ) -> Dict[str, uuid.UUID]:
        """Actor to user id for mappings whose user is active in the account."""
        rows = (
            self._for_connection(db, connection)
            .join(User, User.id == AnthropicUserMapping.user_id)
            .filter(
                User.account_id == connection.account_id,
                User.is_active.is_(True),
            )
            .with_entities(AnthropicUserMapping.actor, AnthropicUserMapping.user_id)
            .all()
        )
        return {row.actor: row.user_id for row in rows}


crud_anthropic_import_connection = CRUDAnthropicImportConnection(
    AnthropicImportConnection
)
crud_anthropic_usage = CRUDAnthropicUsage(ProviderBillingSnapshot)
crud_anthropic_user_mapping = CRUDAnthropicUserMapping(AnthropicUserMapping)
