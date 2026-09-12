"""CRUD operations for account-scoped model pricing overrides."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import inspect, or_
from sqlalchemy.orm import Session

from ..models.model_price_override import ModelPriceOverride
from .base import CRUDBase


class CRUDModelPriceOverride(CRUDBase[ModelPriceOverride]):
    """CRUD helper for model price override lookup and management."""

    def table_exists(self, db: Session) -> bool:
        """Inspect using the caller's connection, without a second pool checkout."""
        return inspect(db.connection()).has_table(self.model.__tablename__)

    def list_for_account(
        self,
        db: Session,
        *,
        account_id: uuid.UUID | str,
        model_alias: Optional[str] = None,
        active_only: bool = False,
    ) -> list[ModelPriceOverride]:
        """List pricing overrides for an account."""
        query = db.query(self.model).filter(self.model.account_id == account_id)
        if model_alias:
            query = query.filter(self.model.model_alias == model_alias)
        if active_only:
            query = query.filter(self.model.is_active.is_(True))
        return query.order_by(self.model.created_at.desc()).all()

    def get_active_for_model(
        self,
        db: Session,
        *,
        account_id: uuid.UUID | str,
        model_alias: Optional[str],
        provider_name: Optional[str] = None,
        ai_model_id: Optional[uuid.UUID | str] = None,
        at: Optional[datetime] = None,
    ) -> Optional[ModelPriceOverride]:
        """Return the best active override for a model at a point in time."""
        if not model_alias and not ai_model_id:
            return None

        observed_at = at or datetime.now(timezone.utc)
        query = db.query(self.model).filter(
            self.model.account_id == account_id,
            self.model.is_active.is_(True),
            or_(
                self.model.effective_from.is_(None),
                self.model.effective_from <= observed_at,
            ),
            or_(
                self.model.effective_until.is_(None),
                self.model.effective_until > observed_at,
            ),
        )
        if ai_model_id:
            query = query.filter(
                or_(
                    self.model.ai_model_id == ai_model_id,
                    self.model.ai_model_id.is_(None),
                )
            )
        if model_alias:
            query = query.filter(self.model.model_alias == model_alias)
        if provider_name:
            normalized_provider = provider_name.strip().lower()
            query = query.filter(
                or_(
                    self.model.provider_name.is_(None),
                    self.model.provider_name == normalized_provider,
                    self.model.provider_name == provider_name,
                )
            )

        return (
            query.order_by(
                self.model.ai_model_id.is_(None),
                self.model.effective_from.desc().nullslast(),
                self.model.created_at.desc(),
            )
            .limit(1)
            .first()
        )

    def create_for_account(
        self,
        db: Session,
        *,
        account_id: uuid.UUID | str,
        obj_in: dict[str, Any],
    ) -> ModelPriceOverride:
        """Create an account-owned pricing override."""
        data = dict(obj_in)
        data["account_id"] = account_id
        if data.get("provider_name"):
            data["provider_name"] = str(data["provider_name"]).strip().lower()
        if data.get("currency"):
            data["currency"] = str(data["currency"]).strip().upper()
        return self.create(db, obj_in=data)


crud_model_price_override = CRUDModelPriceOverride(ModelPriceOverride)
