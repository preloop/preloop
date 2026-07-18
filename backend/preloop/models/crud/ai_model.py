"""CRUD operations for AIModel model."""

import json
import uuid
from typing import Any, Dict, Optional, Sequence

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from preloop.models.models.ai_model import AIModel
from preloop.models.crud.secret_reference import crud_secret_reference
from preloop.services.secret_service import get_secret_service
from .base import CRUDBase


class CRUDAIModel(CRUDBase[AIModel]):
    """CRUD class for AIModel operations."""

    @staticmethod
    def _model_kind(ai_model: AIModel) -> str:
        return getattr(ai_model, "model_kind", "llm")

    @staticmethod
    def _normalize_model_kind_fields(obj_data: Dict[str, Any]) -> Dict[str, Any]:
        """Return a copy with service-kind stored in metadata (no schema migration)."""
        normalized = dict(obj_data)
        if "model_kind" not in normalized:
            return normalized
        model_kind = str(normalized.pop("model_kind") or "llm").strip().lower()
        if model_kind not in {"llm", "stt", "tts"}:
            raise ValueError("model_kind must be one of: llm, stt, tts")
        meta_data = normalized.get("meta_data")
        normalized_meta = dict(meta_data) if isinstance(meta_data, dict) else {}
        normalized_meta["service_kind"] = model_kind
        normalized["meta_data"] = normalized_meta
        return normalized

    @staticmethod
    def _apply_secret_reference_fields(
        db: Session,
        *,
        obj_data: Dict,
        account_id,
        secret_name: str,
        existing_secret_id=None,
    ) -> None:
        """Resolve incoming credential fields into a SecretReference."""
        api_key = obj_data.pop("api_key", None) if "api_key" in obj_data else None
        credential_type = obj_data.pop("credential_type", None)
        credential_payload = obj_data.pop("credential_payload", None)
        credentials_backend_type = obj_data.pop("credentials_backend_type", None)
        credentials_external_ref = obj_data.pop("credentials_external_ref", None)
        credentials_meta_data = obj_data.pop("credentials_meta_data", None)

        if api_key:
            secret_ref = get_secret_service().create_local_secret_reference(
                db,
                account_id=account_id,
                name=secret_name,
                secret_kind="ai_model_api_key",
                secret_value=api_key,
                existing_secret_id=existing_secret_id,
            )
            obj_data["credentials_secret_id"] = secret_ref.id
            obj_data["api_key"] = None
            return

        if credential_type or credential_payload is not None:
            payload = dict(credential_payload or {})
            payload["type"] = credential_type
            secret_ref = get_secret_service().create_local_secret_reference(
                db,
                account_id=account_id,
                name=secret_name,
                secret_kind="ai_model_credentials",
                secret_value=json.dumps(payload),
                existing_secret_id=existing_secret_id,
                meta_data={"credential_type": credential_type},
            )
            obj_data["credentials_secret_id"] = secret_ref.id
            obj_data["api_key"] = None
            return

        if (
            credentials_backend_type
            or credentials_external_ref
            or credentials_meta_data
        ):
            secret_ref = get_secret_service().create_external_secret_reference(
                db,
                account_id=account_id,
                name=secret_name,
                secret_kind="ai_model_api_key",
                backend_type=credentials_backend_type,
                external_ref=credentials_external_ref,
                meta_data=credentials_meta_data,
                existing_secret_id=existing_secret_id,
            )
            obj_data["credentials_secret_id"] = secret_ref.id
            obj_data["api_key"] = None

    def get_default_active_model(
        self,
        db: Session,
        *,
        account_id: Optional[str] = None,
        model_kind: str = "llm",
    ) -> Optional[AIModel]:
        """
        Get the default, active AIModel for a given account.

        If account_id is None, gets the system-wide default.
        If account_id is provided, returns account-specific default or falls back to system-wide default.

        Principal-bound OAuth models (Claude Code / Codex subscription
        credentials) are never returned: they only authorize their owner's
        interactive traffic and fail on server-side generation. When the
        flagged default is such a model — or nothing is flagged — the first
        BYOK/API-key-backed model of the same kind wins instead.

        Args:
            db: Active database session.
            account_id: Owning account identifier, or None for system-wide.
            model_kind: Service kind to resolve ("llm", "stt", or "tts").

        Returns:
            A model usable for server-side generation, or None when the
            account has no BYOK-backed model of that kind.
        """
        normalized_model_kind = model_kind.strip().lower()
        # Eager-load the credential secret: resolving credential_type per
        # candidate would otherwise issue one query per model.
        query = db.query(self.model).options(joinedload(self.model.credentials_secret))
        if account_id is not None:
            query = query.filter(
                or_(
                    self.model.account_id.is_(None), self.model.account_id == account_id
                )
            )
        else:
            query = query.filter(self.model.account_id.is_(None))

        candidates = [
            ai_model
            for ai_model in query.order_by(
                self.model.account_id, self.model.created_at
            ).all()
            if self._model_kind(ai_model) == normalized_model_kind
            and ai_model.supports_server_side_generation
        ]
        if not candidates:
            return None
        for ai_model in candidates:
            if ai_model.is_default:
                return ai_model
        return candidates[0]

    def create_with_account(
        self,
        db: Session,
        *,
        obj_in: Dict,
        account_id: Optional[str] = None,
    ) -> AIModel:
        """Create a new AIModel, assigning it to an account."""
        obj_data = self._normalize_model_kind_fields(dict(obj_in))
        if obj_in.get("is_default"):
            for existing_model in (
                db.query(self.model)
                .filter(self.model.account_id == account_id, self.model.is_default)
                .all()
            ):
                if self._model_kind(existing_model) == (
                    obj_data.get("meta_data") or {}
                ).get("service_kind", "llm"):
                    existing_model.is_default = False

        self._apply_secret_reference_fields(
            db,
            obj_data=obj_data,
            account_id=account_id,
            secret_name=f"AI Model Credential: {obj_data.get('name', 'Unnamed Model')}",
        )

        db_obj = self.model(**obj_data, account_id=account_id)
        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    def get_by_account(
        self, db: Session, *, account_id: uuid.UUID | str
    ) -> list[AIModel]:
        """Get all AIModels for a specific account."""
        return db.query(self.model).filter(self.model.account_id == account_id).all()

    def get_for_managed_agent_enrichment(
        self,
        db: Session,
        *,
        account_id: uuid.UUID | str,
        agent_ids: Sequence[str],
        gateway_aliases: Sequence[str] | None = None,
    ) -> list[AIModel]:
        """Load only AI models needed to enrich a page of managed agents.

        Prefers models tagged with ``meta_data.managed_agent_id`` for the given
        agents, and optionally models whose gateway alias matches a legacy
        configured alias. Avoids loading the full account model catalog when
        list pages only need a handful of agents.

        Args:
            db: Active database session.
            account_id: Owning account identifier.
            agent_ids: Managed agent ids that need model resolution.
            gateway_aliases: Optional gateway aliases for legacy alias→id match.

        Returns:
            Matching AI models for the account, or an empty list when no
            agent/alias filters are provided.
        """
        normalized_agent_ids = [
            str(agent_id).strip() for agent_id in agent_ids if str(agent_id).strip()
        ]
        normalized_aliases = [
            str(alias).strip()
            for alias in (gateway_aliases or [])
            if str(alias).strip()
        ]
        if not normalized_agent_ids and not normalized_aliases:
            return []

        clauses = []
        if normalized_agent_ids:
            clauses.append(
                self.model.meta_data["managed_agent_id"].astext.in_(
                    normalized_agent_ids
                )
            )
        if normalized_aliases:
            clauses.append(
                self.model.meta_data["gateway"]["model_alias"].astext.in_(
                    normalized_aliases
                )
            )
        return (
            db.query(self.model)
            .filter(self.model.account_id == account_id, or_(*clauses))
            .all()
        )

    def get_all_for_account(
        self, db: Session, *, account_id: uuid.UUID | str
    ) -> list[AIModel]:
        """Get all configured AIModels available to the account, including system defaults."""
        return (
            db.query(self.model)
            .filter(
                or_(
                    self.model.account_id == account_id, self.model.account_id.is_(None)
                )
            )
            .all()
        )

    def update(
        self,
        db: Session,
        *,
        db_obj: AIModel,
        obj_in: Dict,
    ) -> AIModel:
        """Update an AIModel. If setting a model as default, ensure others are not."""
        obj_data = self._normalize_model_kind_fields(dict(obj_in))
        target_model_kind = (obj_data.get("meta_data") or {}).get(
            "service_kind"
        ) or db_obj.model_kind
        if obj_in.get("is_default") and not db_obj.is_default:
            # Set all other models for this account to not be default
            for existing_model in (
                db.query(self.model)
                .filter(
                    self.model.account_id == db_obj.account_id,
                    self.model.id != db_obj.id,
                    self.model.is_default,
                )
                .all()
            ):
                if self._model_kind(existing_model) == target_model_kind:
                    existing_model.is_default = False

        self._apply_secret_reference_fields(
            db,
            obj_data=obj_data,
            account_id=db_obj.account_id,
            secret_name=f"AI Model Credential: {obj_data.get('name', db_obj.name)}",
            existing_secret_id=db_obj.credentials_secret_id,
        )

        return super().update(db, db_obj=db_obj, obj_in=obj_data)

    def remove(self, db: Session, *, id: uuid.UUID) -> Optional[AIModel]:
        """Delete an AIModel and any unreferenced credential secret."""
        obj = db.get(self.model, id)
        if obj is None:
            return None

        secret_id = obj.credentials_secret_id
        db.delete(obj)
        db.flush()

        if secret_id is not None:
            remaining_reference = (
                db.query(self.model.id)
                .filter(self.model.credentials_secret_id == secret_id)
                .first()
            )
            if remaining_reference is None:
                secret_ref = crud_secret_reference.get(db, id=secret_id)
                if secret_ref is not None:
                    db.delete(secret_ref)

        db.commit()
        return obj

    def default_model_exists(self, db: Session) -> bool:
        """Check if a system-wide default model exists."""
        return (
            db.query(self.model.id)
            .filter(self.model.is_default, self.model.account_id.is_(None))
            .first()
            is not None
        )


ai_model = CRUDAIModel(AIModel)
