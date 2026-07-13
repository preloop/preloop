"""CRUD operations for managed-agent model bindings."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session, joinedload

from ..models.managed_agent_ai_model_binding import ManagedAgentAIModelBinding
from .base import CRUDBase


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CRUDManagedAgentAIModelBinding(CRUDBase[ManagedAgentAIModelBinding]):
    """CRUD helpers for explicit managed-agent model bindings."""

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
        """Replace the active binding set for one managed agent."""
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

        for key, row in existing_by_key.items():
            if key in seen_keys:
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


crud_managed_agent_ai_model_binding = CRUDManagedAgentAIModelBinding(
    ManagedAgentAIModelBinding
)
