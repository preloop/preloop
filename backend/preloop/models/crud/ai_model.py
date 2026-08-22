"""CRUD operations for AIModel model."""

import copy
import json
import logging
import uuid
from typing import Any, Dict, Optional, Sequence

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from preloop.models.models.ai_model import AIModel
from preloop.models.crud.secret_reference import crud_secret_reference
from preloop.services.secret_service import get_secret_service
from .base import CRUDBase

logger = logging.getLogger(__name__)


def _effective_gateway_alias_from_fields(
    provider_name: Optional[str],
    model_identifier: Optional[str],
    meta_data: Optional[Dict],
) -> Optional[str]:
    """Compute the alias a would-be model row answers to on the gateway.

    Mirrors ``model_runtime_resolver.effective_gateway_alias`` but works on
    raw field values so create/update payloads can be validated before a row
    exists. ``None`` when the row is not gateway-enabled.
    """
    gateway = meta_data.get("gateway") if isinstance(meta_data, dict) else None
    if not isinstance(gateway, dict) or not gateway.get("enabled"):
        return None
    alias = gateway.get("model_alias")
    if isinstance(alias, str) and alias.strip():
        return alias.strip()
    provider = (provider_name or "openai").strip().lower()
    identifier = (model_identifier or "").strip()
    return f"{provider}/{identifier}" if identifier else provider


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
    def _validate_qwen_api_endpoint(
        obj_data: Dict[str, Any],
        existing: Optional[AIModel] = None,
    ) -> None:
        """Reject a Qwen chat endpoint outside DashScope / Model Studio."""
        provider = (
            str(
                obj_data.get("provider_name")
                or (existing.provider_name if existing is not None else "")
                or ""
            )
            .strip()
            .lower()
        )
        if provider != "qwen":
            return
        if "api_endpoint" in obj_data:
            endpoint = obj_data.get("api_endpoint")
        elif existing is not None:
            endpoint = existing.api_endpoint
        else:
            return
        if not isinstance(endpoint, str) or not endpoint.strip():
            return
        from preloop.services.ai_model_provider import validate_qwen_endpoint

        validate_qwen_endpoint(endpoint)

    @staticmethod
    def _apply_secret_reference_fields(
        db: Session,
        *,
        obj_data: Dict,
        account_id,
        secret_name: str,
        existing_secret_id=None,
    ) -> None:
        """Resolve incoming credential fields into a SecretReference.

        When ``obj_data`` already carries a ``credentials_secret_id`` and no new
        credential material, the existing secret is reused as-is. This is what lets
        several models share one provider key. The secret is verified to belong to
        ``account_id`` first, so a caller cannot attach another account's key.

        Raises:
            ValueError: If the referenced secret does not exist or belongs to a
                different account.
        """
        api_key = obj_data.pop("api_key", None) if "api_key" in obj_data else None
        credential_type = obj_data.pop("credential_type", None)
        credential_payload = obj_data.pop("credential_payload", None)
        credentials_backend_type = obj_data.pop("credentials_backend_type", None)
        credentials_external_ref = obj_data.pop("credentials_external_ref", None)
        credentials_meta_data = obj_data.pop("credentials_meta_data", None)

        reuse_secret_id = obj_data.get("credentials_secret_id")
        has_new_credential_material = bool(
            api_key
            or credential_type
            or credential_payload is not None
            or credentials_backend_type
            or credentials_external_ref
            or credentials_meta_data
        )
        if reuse_secret_id is not None and not has_new_credential_material:
            secret_ref = crud_secret_reference.get(db, id=reuse_secret_id)
            if secret_ref is None:
                raise ValueError("Referenced credential secret does not exist")
            if str(secret_ref.account_id) != str(account_id):
                raise ValueError(
                    "Referenced credential secret belongs to a different account"
                )
            obj_data["api_key"] = None
            return

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

    def _enforce_unique_gateway_alias(
        self,
        db: Session,
        *,
        obj_data: Dict,
        provider_name: Optional[str],
        model_identifier: Optional[str],
        account_id,
        exclude_id: Optional[uuid.UUID] = None,
    ) -> None:
        """Keep gateway aliases unique per account at write time.

        Two bindings answering to the same alias make routing and usage
        attribution ambiguous (an alias resolves to exactly one
        ``ai_model_id``). Explicit user writes that would collide are
        rejected; agent-onboarding imports (rows carrying
        ``meta_data.managed_by``) are auto-suffixed instead — an import must
        never fail onboarding, but it must never silently take over a user's
        alias either.

        This is deliberately an application-level read-then-write check with
        no backing DB constraint, so two writes racing each other can both
        pass and land a collision. A partial unique index cannot express the
        *effective* alias — it is a conditional over
        ``meta_data->'gateway'->>'model_alias'`` and the computed
        ``provider/model_identifier`` default (an indexed expression would
        need a backfilled generated column) — and creating one would abort
        ``alembic upgrade`` on accounts holding legacy user-created
        collisions, which the audit migration intentionally reports but
        never rewrites. The residual race is tolerated because the runtime
        resolver keeps colliding aliases deterministic (user-created wins,
        else stable inventory order) and every multi-match is logged and
        surfaced via ``X-Preloop-Warning``, so a raced-in collision is
        visible instead of silently misrouting.

        Args:
            db: Active session.
            obj_data: Normalized column values about to be written. Mutated
                in place (deep-copied ``meta_data``) when auto-suffixing.
            provider_name: Effective provider for default-alias computation.
            model_identifier: Effective identifier for default-alias
                computation.
            account_id: Owning account; ``None`` (system rows) is exempt.
            exclude_id: Row being updated, excluded from the taken-alias set.

        Raises:
            ValueError: When a non-import write would create a collision.
        """
        if account_id is None:
            return
        meta_data = obj_data.get("meta_data")
        alias = _effective_gateway_alias_from_fields(
            provider_name, model_identifier, meta_data
        )
        if not alias:
            return

        from preloop.services.model_runtime_resolver import effective_gateway_alias

        # NOTE: read-then-write without a DB-level uniqueness guard; see the
        # docstring for why no partial unique index backs this and why the
        # concurrent-write window is acceptable.
        taken: set[str] = set()
        for existing in (
            db.query(self.model).filter(self.model.account_id == account_id).all()
        ):
            if exclude_id is not None and existing.id == exclude_id:
                continue
            existing_alias = effective_gateway_alias(existing)
            if existing_alias:
                taken.add(existing_alias)
        if alias not in taken:
            return

        managed_by = (
            str((meta_data or {}).get("managed_by") or "").strip()
            if isinstance(meta_data, dict)
            else ""
        )
        if not managed_by:
            raise ValueError(
                f"Gateway alias '{alias}' is already used by another AI model "
                "in this account. Choose a different alias (or remove the "
                "other binding) so gateway routing and usage attribution stay "
                "unambiguous."
            )

        suffix = 2
        while f"{alias}-{suffix}" in taken:
            suffix += 1
        suffixed = f"{alias}-{suffix}"
        new_meta = copy.deepcopy(meta_data)
        new_meta.setdefault("gateway", {})["model_alias"] = suffixed
        obj_data["meta_data"] = new_meta
        logger.warning(
            "gateway_alias_collision_autosuffix account=%s managed_by=%r "
            "requested_alias=%r assigned_alias=%r",
            account_id,
            managed_by,
            alias,
            suffixed,
        )

    def create_with_account(
        self,
        db: Session,
        *,
        obj_in: Dict,
        account_id: Optional[str] = None,
        commit: bool = True,
    ) -> AIModel:
        """Create a new AIModel, assigning it to an account.

        Args:
            db: Database session.
            obj_in: Column values for the new row.
            account_id: Owning account.
            commit: When False, flush only so callers can batch several
                writes into one atomic transaction (e.g. under a savepoint)
                and commit themselves.
        """
        obj_data = self._normalize_model_kind_fields(dict(obj_in))
        self._validate_qwen_api_endpoint(obj_data)
        self._enforce_unique_gateway_alias(
            db,
            obj_data=obj_data,
            provider_name=obj_data.get("provider_name"),
            model_identifier=obj_data.get("model_identifier"),
            account_id=account_id,
        )
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
        if commit:
            db.commit()
        else:
            db.flush()
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
        """Get all configured AIModels available to the account, including system defaults.

        Results are ordered deterministically: account-owned models before system
        defaults, then oldest-first by ``created_at``, with ``id`` as a final
        tiebreak. Model resolution and therefore pricing depend on this ordering
        being stable across requests, so it must not be removed.
        """
        return (
            db.query(self.model)
            .filter(
                or_(
                    self.model.account_id == account_id, self.model.account_id.is_(None)
                )
            )
            .order_by(
                self.model.account_id.is_(None).asc(),
                self.model.created_at.asc(),
                self.model.id.asc(),
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
        self._validate_qwen_api_endpoint(obj_data, existing=db_obj)

        # Enforce alias uniqueness only when this update changes the effective
        # gateway alias; pre-existing rows (including legacy collisions being
        # cleaned up) must remain updatable for unrelated fields.
        from preloop.services.model_runtime_resolver import effective_gateway_alias

        merged_provider = obj_data.get("provider_name", db_obj.provider_name)
        merged_identifier = obj_data.get("model_identifier", db_obj.model_identifier)
        merged_meta = (
            obj_data["meta_data"] if "meta_data" in obj_data else db_obj.meta_data
        )
        new_alias = _effective_gateway_alias_from_fields(
            merged_provider, merged_identifier, merged_meta
        )
        if new_alias and new_alias != effective_gateway_alias(db_obj):
            check_data = {"meta_data": merged_meta}
            self._enforce_unique_gateway_alias(
                db,
                obj_data=check_data,
                provider_name=merged_provider,
                model_identifier=merged_identifier,
                account_id=db_obj.account_id,
                exclude_id=db_obj.id,
            )
            if check_data["meta_data"] is not merged_meta:
                # Import row was auto-suffixed; persist the rewritten alias.
                obj_data["meta_data"] = check_data["meta_data"]

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
