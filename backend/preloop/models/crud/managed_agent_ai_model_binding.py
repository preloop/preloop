"""CRUD operations for managed-agent model bindings."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
import uuid

from sqlalchemy.orm import Session, joinedload

from ..models.ai_model import AIModel
from ..models.managed_agent_ai_model_binding import (
    AUTOREGISTER_CONFIG_KEY_PREFIX,
    ManagedAgentAIModelBinding,
)
from .base import CRUDBase


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CRUDManagedAgentAIModelBinding(CRUDBase[ManagedAgentAIModelBinding]):
    """CRUD helpers for explicit managed-agent model bindings."""

    def list_for_fleet_map(
        self,
        db: Session,
        *,
        account_id: str,
        agent_ids: list[str],
        limit: int,
    ) -> list[ManagedAgentAIModelBinding]:
        """Load a bounded binding window without fetching models or secrets.

        Args:
            db: Active database session.
            account_id: Owning tenant.
            agent_ids: Already visible agents in this tenant.
            limit: Source window including a truncation sentinel.

        Returns:
            Bindings, including inactive slots to suppress legacy fallback.
        """
        if not agent_ids:
            return []
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.managed_agent_id.in_(agent_ids),
            )
            .order_by(self.model.id)
            .limit(limit)
            .all()
        )

    def model_bound_to_other_agent(
        self, db: Session, *, account_id: str, ai_model_id: str, agent_id: str
    ) -> bool:
        """Tell whether a model is bound to an agent other than ``agent_id``.

        Inactive bindings count: they still name a slot of another agent.

        Args:
            db: Database session.
            account_id: Owning account.
            ai_model_id: AI model id.
            agent_id: The agent whose own bindings are ignored.

        Returns:
            True when another managed agent has a binding to the model.
        """
        return (
            db.query(self.model.id)
            .filter(
                self.model.account_id == account_id,
                self.model.ai_model_id == ai_model_id,
                self.model.managed_agent_id != agent_id,
            )
            .first()
            is not None
        )

    def list_for_agent(
        self,
        db: Session,
        *,
        account_id: str,
        agent_id: str,
        include_inactive: bool = False,
    ) -> list[ManagedAgentAIModelBinding]:
        """Return bindings for one managed agent."""
        return self.list_for_agents(
            db,
            account_id=account_id,
            agent_ids=[str(agent_id)],
            include_inactive=include_inactive,
        ).get(str(agent_id), [])

    def list_for_agents(
        self,
        db: Session,
        *,
        account_id: str,
        agent_ids: list[str],
        include_inactive: bool = False,
    ) -> dict[str, list[ManagedAgentAIModelBinding]]:
        """Return bindings for many managed agents in one query.

        Args:
            db: Active database session.
            account_id: Owning account identifier.
            agent_ids: Managed agent ids to load bindings for.
            include_inactive: When True, include inactive bindings.

        Returns:
            Mapping from agent id string to that agent's bindings, ordered the
            same way as :meth:`list_for_agent`.
        """
        normalized_ids = [str(agent_id) for agent_id in agent_ids]
        if not normalized_ids:
            return {}

        query = (
            db.query(self.model)
            .options(joinedload(self.model.ai_model))
            .filter(
                self.model.account_id == account_id,
                self.model.managed_agent_id.in_(normalized_ids),
            )
        )
        if not include_inactive:
            query = query.filter(self.model.status != "inactive")
        rows = query.order_by(
            self.model.managed_agent_id.asc(),
            self.model.is_primary.desc(),
            self.model.config_key.asc(),
            self.model.gateway_alias.asc(),
            self.model.last_seen_at.desc(),
        ).all()

        result: dict[str, list[ManagedAgentAIModelBinding]] = {
            agent_id: [] for agent_id in normalized_ids
        }
        for row in rows:
            result.setdefault(str(row.managed_agent_id), []).append(row)
        return result

    def replace_for_agent(
        self,
        db: Session,
        *,
        account_id: str,
        agent_id: str,
        bindings: list[dict[str, Any]],
        commit: bool = True,
    ) -> list[ManagedAgentAIModelBinding]:
        """Replace the active binding set for one managed agent.

        Slots named in ``bindings`` are created or refreshed; every other slot
        is deactivated, with one carve-out. Bindings the model gateway created
        on its own (``config_key`` under :data:`AUTOREGISTER_CONFIG_KEY_PREFIX`)
        stay active when their model shares a credential secret with a model
        in the new set: they are the same subscription-OAuth lineage the
        caller just (re)confirmed, the CLI cannot enumerate them, and dropping
        them turned every re-onboard into a 400 on the agent's next request
        for a model it had picked in its own picker. Autoregistered siblings
        on a secret the new set no longer references are deactivated like any
        other stale slot.

        Args:
            db: Active database session.
            account_id: Owning account identifier.
            agent_id: Managed agent whose bindings are replaced.
            bindings: Desired slots; items missing ``config_key``,
                ``gateway_alias`` or ``ai_model_id`` are ignored.
            commit: Commit when True, otherwise flush only.

        Returns:
            The agent's active bindings after the replacement.
        """
        existing_rows = (
            db.query(self.model)
            .options(joinedload(self.model.ai_model))
            .filter(
                self.model.account_id == account_id,
                self.model.managed_agent_id == agent_id,
            )
            .all()
        )
        existing_by_key = {
            (row.config_key, row.gateway_alias): row for row in existing_rows
        }
        seen_keys: set[tuple[str, str]] = set()
        now = _utc_now()

        for index, item in enumerate(bindings):
            config_key = str(item.get("config_key") or "").strip()
            gateway_alias = str(item.get("gateway_alias") or "").strip()
            ai_model_id = str(item.get("ai_model_id") or "").strip()
            if not config_key or not gateway_alias or not ai_model_id:
                continue

            key = (config_key, gateway_alias)
            seen_keys.add(key)
            row = existing_by_key.get(key)
            if row is None:
                row = self.model(
                    account_id=account_id,
                    managed_agent_id=agent_id,
                    ai_model_id=ai_model_id,
                    binding_type=str(item.get("binding_type") or "configured"),
                    config_key=config_key,
                    gateway_alias=gateway_alias,
                    is_primary=bool(item.get("is_primary", index == 0)),
                    status=str(item.get("status") or "gateway_ready"),
                    first_seen_at=now,
                    last_seen_at=now,
                )
                db.add(row)
                existing_by_key[key] = row
                continue

            row.ai_model_id = ai_model_id
            row.binding_type = str(item.get("binding_type") or row.binding_type)
            row.is_primary = bool(item.get("is_primary", False))
            row.status = str(item.get("status") or row.status)
            row.last_seen_at = now
            db.add(row)

        retained_secret_ids = self._credential_secret_ids(
            db,
            ai_model_ids=[
                str(item.get("ai_model_id") or "").strip()
                for item in bindings
                if str(item.get("ai_model_id") or "").strip()
            ],
        )
        for key, row in existing_by_key.items():
            if key in seen_keys:
                continue
            if self._is_same_lineage_autoregistered(row, retained_secret_ids):
                row.is_primary = False
                db.add(row)
                continue
            row.status = "inactive"
            row.is_primary = False
            db.add(row)

        if commit:
            db.commit()
        else:
            db.flush()

        return self.list_for_agent(
            db,
            account_id=account_id,
            agent_id=agent_id,
            include_inactive=False,
        )

    @staticmethod
    def _credential_secret_ids(db: Session, *, ai_model_ids: list[str]) -> set[str]:
        """Credential secret ids behind the given AI model ids (None dropped)."""
        if not ai_model_ids:
            return set()
        rows = (
            db.query(AIModel.credentials_secret_id)
            .filter(AIModel.id.in_(ai_model_ids))
            .all()
        )
        return {str(secret_id) for (secret_id,) in rows if secret_id is not None}

    @staticmethod
    def _is_same_lineage_autoregistered(
        row: ManagedAgentAIModelBinding, retained_secret_ids: set[str]
    ) -> bool:
        """True for a gateway-autoregistered slot on a retained credential secret."""
        if row.status == "inactive":
            return False
        if not str(row.config_key or "").startswith(AUTOREGISTER_CONFIG_KEY_PREFIX):
            return False
        ai_model = row.ai_model
        secret_id = getattr(ai_model, "credentials_secret_id", None)
        return secret_id is not None and str(secret_id) in retained_secret_ids

    def ensure_active_binding(
        self,
        db: Session,
        *,
        account_id: str,
        agent_id: str,
        ai_model_id: str,
        gateway_alias: str,
        config_key: str,
        commit: bool = True,
    ) -> ManagedAgentAIModelBinding:
        """Make sure one agent holds an active binding to one AI model.

        Reuses the slot ``(config_key, gateway_alias)`` when it exists, else
        any existing row for the same model (whatever its slot), else creates
        a new row. An inactive row is reactivated in place, so a binding the
        CLI deactivated on re-onboard is not duplicated and the unique slot
        constraint is never tripped. Never promotes a row to primary.

        Args:
            db: Active database session.
            account_id: Owning account identifier.
            agent_id: Managed agent receiving the binding.
            ai_model_id: AI model to bind.
            gateway_alias: Alias the agent addresses the model by.
            config_key: Slot to use when no row exists yet.
            commit: Commit when True, otherwise flush only.

        Returns:
            The active binding row.
        """
        now = _utc_now()
        candidates = (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.managed_agent_id == agent_id,
            )
            .filter(
                (
                    (self.model.config_key == config_key)
                    & (self.model.gateway_alias == gateway_alias)
                )
                | (self.model.ai_model_id == ai_model_id)
            )
            .order_by(self.model.first_seen_at.asc())
            .all()
        )
        row = next(
            (
                c
                for c in candidates
                if c.config_key == config_key and c.gateway_alias == gateway_alias
            ),
            None,
        )
        if row is None:
            row = next(
                (
                    c
                    for c in candidates
                    if str(c.ai_model_id) == str(ai_model_id) and c.status != "inactive"
                ),
                None,
            ) or next(
                (c for c in candidates if str(c.ai_model_id) == str(ai_model_id)),
                None,
            )
        if row is None:
            row = self.model(
                account_id=account_id,
                managed_agent_id=agent_id,
                ai_model_id=ai_model_id,
                binding_type="configured",
                config_key=config_key,
                gateway_alias=gateway_alias,
                is_primary=False,
                status="gateway_ready",
                first_seen_at=now,
                last_seen_at=now,
            )
        else:
            row.ai_model_id = uuid.UUID(str(ai_model_id))
            if row.status == "inactive":
                row.status = "gateway_ready"
            row.last_seen_at = now
        db.add(row)
        if commit:
            db.commit()
        else:
            db.flush()
        return row

    def list_for_user_models(
        self, db: Session, *, account_id: Any, user_id: Any, model_ids: list[str]
    ) -> list[ManagedAgentAIModelBinding]:
        """Load active bindings on the caller's active agents in one query.

        Args:
            db: Database session.
            account_id: Calling account.
            user_id: Calling user.
            model_ids: Registrations already authorized for reading.

        Returns:
            Active binding rows, without loading models or credential secrets.
        """
        from preloop.models import models

        if not model_ids:
            return []
        return (
            db.query(self.model)
            .join(
                models.ManagedAgent,
                self.model.managed_agent_id == models.ManagedAgent.id,
            )
            .filter(
                self.model.account_id == account_id,
                self.model.ai_model_id.in_(model_ids),
                self.model.status != "inactive",
                models.ManagedAgent.account_id == account_id,
                models.ManagedAgent.owner_user_id == user_id,
                models.ManagedAgent.lifecycle_state == "active",
            )
            .all()
        )

    def subscription_owners_for_models(
        self, db: Session, *, account_id: Any, ai_models: list[AIModel]
    ) -> dict[str, set[str]]:
        """Resolve owners for all requested credential lineages in one query.

        Args:
            db: Database session.
            account_id: Account that owns the models.
            ai_models: Subscription models to inspect.

        Returns:
            Model ids mapped to distinct owners; an empty string denotes an
            ownerless agent. Siblings sharing a secret have the same owners.
        """
        from sqlalchemy import or_
        from preloop.models import models

        if not ai_models:
            return {}
        secret_ids = {
            model.credentials_secret_id
            for model in ai_models
            if model.credentials_secret_id is not None
        }
        standalone_ids = [
            model.id for model in ai_models if model.credentials_secret_id is None
        ]
        rows = (
            db.query(
                AIModel.id,
                AIModel.credentials_secret_id,
                models.ManagedAgent.owner_user_id,
            )
            .join(self.model, self.model.ai_model_id == AIModel.id)
            .join(
                models.ManagedAgent,
                models.ManagedAgent.id == self.model.managed_agent_id,
            )
            .filter(
                AIModel.account_id == account_id,
                self.model.account_id == account_id,
                models.ManagedAgent.account_id == account_id,
                self.model.status != "inactive",
                models.ManagedAgent.lifecycle_state == "active",
                or_(
                    AIModel.credentials_secret_id.in_(secret_ids),
                    AIModel.id.in_(standalone_ids),
                ),
            )
            .distinct()
            .all()
        )
        owners: dict[tuple[str, str], set[str]] = {}
        for model_id, secret_id, owner_id in rows:
            key = (
                ("secret", str(secret_id))
                if secret_id is not None
                else ("model", str(model_id))
            )
            owners.setdefault(key, set()).add(
                str(owner_id) if owner_id is not None else ""
            )
        return {
            str(model.id): owners.get(
                ("secret", str(model.credentials_secret_id))
                if model.credentials_secret_id is not None
                else ("model", str(model.id)),
                set(),
            )
            for model in ai_models
        }

    def subscription_owner_user_ids(
        self, db: Session, *, account_id: str, ai_model: AIModel
    ) -> set[str]:
        """Owners of the active agents bound to one subscription lineage.

        A subscription lineage is every model row on the same credential
        secret (the gateway registers siblings on the onboarded secret), or
        the row alone when it has no secret. Only active bindings of agents
        in ``lifecycle_state == "active"`` count.

        Args:
            db: Active database session.
            account_id: Account that owns the model.
            ai_model: Subscription-OAuth model row.

        Returns:
            Distinct ``owner_user_id`` strings; ``""`` stands for an agent
            with no recorded owner so callers can treat it as ambiguous.
        """
        return self.subscription_owners_for_models(
            db, account_id=account_id, ai_models=[ai_model]
        )[str(ai_model.id)]


crud_managed_agent_ai_model_binding = CRUDManagedAgentAIModelBinding(
    ManagedAgentAIModelBinding
)
