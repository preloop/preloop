"""Transactional sharing intents, recipients, snapshots and safe H3 reads."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.models.hierarchy import descendants
from preloop.schemas.access_rule import AccessRuleDefinition
from preloop.schemas.resource_share import ResourceShareDefinition, SharedResourceRead

RESOURCE_MODELS = {
    "ai_model": models.AIModel,
    "mcp_server": models.MCPServer,
    "managed_agent": models.ManagedAgent,
}
ShareSelector = Callable[
    [list[dict[str, Any]], dict[str, Any], dict[str, Any], UUID | None], bool
]
_selector: ShareSelector | None = None


def sharing_enabled() -> bool:
    """Whether the enterprise sharing materializer is installed."""
    return _selector is not None


class ShareConflictError(ValueError):
    """Invalid account-owned sharing intent; never includes a credential."""


def _id(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def policy_section(db: Session, account_id: UUID) -> list[dict[str, Any]]:
    """Return only the owning account's live, explicit sharing intents."""
    shares = db.scalars(
        select(models.ResourceShare)
        .where(
            models.ResourceShare.owner_account_id == account_id,
            models.ResourceShare.revoked_at.is_(None),
            models.ResourceShare.is_automatic.is_(False),
        )
        .order_by(models.ResourceShare.id)
    ).all()
    return [
        ResourceShareDefinition(
            id=row.id,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            target_mode=row.target_mode,
            selected_account_ids=row.selected_account_ids,
            access_rule_id=row.access_rule_id,
            require_approval=row.require_approval,
        ).model_dump(mode="json")
        for row in shares
    ]


def _audit(
    db: Session,
    owner: UUID,
    affected: Iterable[UUID],
    actor: UUID | None,
    operation: str,
    details: dict[str, Any],
) -> None:
    from preloop.models.crud.access_rule import _record_change

    for account_id in sorted({owner, *affected}, key=str):
        if db.get(models.Account, account_id) is None:
            continue
        # An owning actor may not have a user row in the recipient. The mirror
        # records actor id as data, never as another tenant's membership FK.
        user_id = actor if account_id == owner else None
        _record_change(
            db,
            account_id,
            user_id,
            operation,
            {
                "config_type": "resource_shares",
                "owner_account_id": str(owner),
                "actor_user_id": str(actor) if actor else None,
                **details,
            },
            resource_type="resource_share",
        )


class CRUDResourceShare:
    def owned_account(
        self, db: Session, *, account_id: Any, current_account_id: Any
    ) -> models.Account:
        """Require the caller's current account to be the sharing owner."""
        if _id(account_id) != _id(current_account_id):
            raise ShareConflictError("account not found")
        account = db.get(models.Account, _id(account_id))
        if account is None:
            raise ShareConflictError("account not found")
        return account

    def list(self, db: Session, *, owner_account_id: Any) -> list[dict[str, Any]]:
        """List active intents and materialized target IDs, without resource secrets."""
        owner = _id(owner_account_id)
        rows = db.scalars(
            select(models.ResourceShare)
            .where(
                models.ResourceShare.owner_account_id == owner,
                models.ResourceShare.revoked_at.is_(None),
            )
            .order_by(models.ResourceShare.created_at, models.ResourceShare.id)
        ).all()
        recipients = db.execute(
            select(
                models.ResourceShareRecipient.share_id,
                models.ResourceShareRecipient.recipient_account_id,
            ).where(models.ResourceShareRecipient.owner_account_id == owner)
        ).all()
        return [
            {
                "id": str(row.id),
                "resource_type": row.resource_type,
                "resource_id": str(row.resource_id),
                "target_mode": row.target_mode,
                "access_rule_id": str(row.access_rule_id)
                if row.access_rule_id
                else None,
                "selected_account_ids": [str(key) for key in row.selected_account_ids],
                "recipient_account_ids": sorted(
                    str(key) for share_id, key in recipients if share_id == row.id
                ),
                "is_automatic": row.is_automatic,
                "require_approval": row.require_approval,
            }
            for row in rows
        ]

    def create(
        self,
        db: Session,
        *,
        owner_account_id: Any,
        user_id: Any,
        definition: ResourceShareDefinition,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Create and materialize an account-owned intent in one transaction."""
        owner = _id(owner_account_id)
        account = db.scalar(
            select(models.Account).where(models.Account.id == owner).with_for_update()
        )
        if account is None:
            raise ShareConflictError("account not found")
        resource = db.get(
            RESOURCE_MODELS[definition.resource_type], definition.resource_id
        )
        if resource is None or resource.account_id != owner:
            raise ShareConflictError("resource not found in owning account")
        target_ids = set(definition.selected_account_ids)
        children = descendants(db, account)
        if not target_ids.issubset({row.id for row in children}):
            raise ShareConflictError(
                "selected accounts must be descendants of the owner"
            )
        if definition.access_rule_id:
            rule = db.get(models.AccessRule, definition.access_rule_id)
            if (
                rule is None
                or rule.account_id != owner
                or "resource:share" not in rule.actions
                or rule.effect != "permit"
            ):
                raise ShareConflictError(
                    "rule must be an owner permit for resource:share"
                )
        duplicate = db.scalar(
            select(models.ResourceShare.id).where(
                models.ResourceShare.owner_account_id == owner,
                models.ResourceShare.resource_type == definition.resource_type,
                models.ResourceShare.resource_id == definition.resource_id,
                models.ResourceShare.is_automatic.is_(False),
                models.ResourceShare.revoked_at.is_(None),
            )
        )
        if duplicate:
            raise ShareConflictError("resource already has a live explicit share")
        row = models.ResourceShare(
            id=definition.id or uuid4(),
            owner_account_id=owner,
            created_by=_id(user_id),
            **definition.model_dump(exclude={"id"}),
        )
        if db.get(models.ResourceShare, row.id) is not None:
            raise ShareConflictError("share identity already exists")
        db.add(row)
        db.flush()
        self.reconcile(db, owner_account_ids=[owner], actor_id=_id(user_id))
        affected = list(
            db.scalars(
                select(models.ResourceShareRecipient.recipient_account_id).where(
                    models.ResourceShareRecipient.share_id == row.id
                )
            )
        )
        _audit(
            db,
            owner,
            affected,
            _id(user_id),
            "resource_shared",
            {"share_id": str(row.id), "resource_id": str(row.resource_id)},
        )
        if commit:
            db.commit()
        return next(
            item
            for item in self.list(db, owner_account_id=owner)
            if item["id"] == str(row.id)
        )

    def revoke(
        self,
        db: Session,
        *,
        owner_account_id: Any,
        user_id: Any,
        share_id: Any,
        commit: bool = True,
    ) -> None:
        """Revoke intent and recipients atomically; existing calls retain their rows."""
        owner = _id(owner_account_id)
        db.scalar(
            select(models.Account).where(models.Account.id == owner).with_for_update()
        )
        row = db.scalar(
            select(models.ResourceShare)
            .where(
                models.ResourceShare.id == _id(share_id),
                models.ResourceShare.owner_account_id == owner,
                models.ResourceShare.revoked_at.is_(None),
            )
            .with_for_update()
        )
        if row is None:
            raise ShareConflictError("share not found")
        affected = list(
            db.scalars(
                select(models.ResourceShareRecipient.recipient_account_id).where(
                    models.ResourceShareRecipient.share_id == row.id
                )
            )
        )
        row.revoked_at, row.revoked_by = datetime.now(timezone.utc), _id(user_id)
        db.flush()  # Existing database revoke trigger removes recipients.
        _audit(
            db,
            owner,
            affected,
            _id(user_id),
            "resource_unshared",
            {"share_id": str(row.id), "resource_id": str(row.resource_id)},
        )
        if commit:
            db.commit()

    def visible_ids(
        self, db: Session, *, account_id: Any, resource_type: str
    ) -> list[UUID]:
        """H3 hot path: materialized recipient index, never tree traversal."""
        if not sharing_enabled() or resource_type not in RESOURCE_MODELS:
            return []
        return list(
            db.scalars(
                select(models.ResourceShareRecipient.resource_id)
                .where(
                    models.ResourceShareRecipient.recipient_account_id
                    == _id(account_id),
                    models.ResourceShareRecipient.resource_type == resource_type,
                )
                .distinct()
            )
        )

    def visible_resource(
        self, db: Session, *, account_id: Any, resource_type: str, resource_id: Any
    ) -> Any:
        """Server-side resolution only; endpoints must use public_read instead."""
        model = RESOURCE_MODELS.get(resource_type)
        if model is None:
            return None
        row = db.get(model, _id(resource_id))
        if row is None:
            return None
        if row.account_id == _id(account_id):
            return row
        shared = db.scalar(
            select(models.ResourceShareRecipient.id).where(
                models.ResourceShareRecipient.recipient_account_id == _id(account_id),
                models.ResourceShareRecipient.resource_type == resource_type,
                models.ResourceShareRecipient.resource_id == row.id,
            )
        )
        return row if shared else None

    def public_read(
        self, db: Session, *, account_id: Any, resource_type: str, resource_id: Any
    ) -> SharedResourceRead | None:
        """Project a shared resource through a closed credential-free schema."""
        if not sharing_enabled():
            return None
        row = self.visible_resource(
            db,
            account_id=account_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        if row is None or row.account_id == _id(account_id):
            return None
        owner = db.get(models.Account, row.account_id)
        control_enabled = False
        control_online = False
        if resource_type == "managed_agent":
            from preloop.services.agent_control_dispatch import agent_has_control_config

            control_enabled = agent_has_control_config(
                db, account_id=str(row.account_id), agent=row
            )
            heartbeat = row.control_last_heartbeat_at
            if heartbeat:
                instant = (
                    heartbeat
                    if heartbeat.tzinfo
                    else heartbeat.replace(tzinfo=timezone.utc)
                )
                control_online = (
                    datetime.now(timezone.utc) - instant
                ).total_seconds() < 60
        return SharedResourceRead(
            id=row.id,
            resource_type=resource_type,
            name=getattr(row, "name", None)
            or getattr(row, "display_name", "Shared resource"),
            provider=getattr(row, "provider_name", None),
            identifier=getattr(row, "model_identifier", None),
            display_name=row.display_name if resource_type == "managed_agent" else None,
            agent_kind=row.agent_kind if resource_type == "managed_agent" else None,
            owner_name=owner.organization_name if owner else "Parent account",
            control_enabled=control_enabled,
            control_online=control_online,
            lifecycle_state=row.lifecycle_state
            if resource_type == "managed_agent"
            else None,
            price=self.public_price(db, row) if resource_type == "ai_model" else None,
        )

    def public_price(
        self, db: Session, model: models.AIModel
    ) -> dict[str, float] | None:
        """Return the owner's declared numeric tariff without arbitrary metadata."""
        from preloop.services.pricing_overrides import resolve_pricing_override

        pricing = resolve_pricing_override(
            db, account_id=model.account_id, ai_model=model
        )
        if not isinstance(pricing, dict):
            pricing = (model.meta_data or {}).get("pricing")
        if not isinstance(pricing, dict):
            pricing = (model.model_parameters or {}).get("pricing")
        fields = {
            "input_price_per_1k",
            "output_price_per_1k",
            "cache_read_input_price_per_1k",
            "cache_creation_input_price_per_1k",
            "price_per_1k",
            "request_price",
            "discount_percent",
        }
        if not isinstance(pricing, dict):
            return None
        return {
            key: float(value)
            for key, value in pricing.items()
            if key in fields and isinstance(value, (int, float))
        } or None

    def formerly_shared_model_alias(
        self, db: Session, *, account_id: Any, alias: str
    ) -> bool:
        """Recognize only model identities previously audited in this recipient."""
        if not sharing_enabled():
            return False
        from preloop.services.model_runtime_resolver import (
            gateway_model_alias_candidates,
        )

        identities = db.scalars(
            select(models.AuditLog.details["resource_id"].astext).where(
                models.AuditLog.account_id == _id(account_id),
                models.AuditLog.resource_type == "resource_share",
                models.AuditLog.details["operation"].astext == "resource_unshared",
            )
        ).all()
        rows = (
            db.scalars(
                select(models.AIModel).where(
                    models.AIModel.id.in_(
                        [_id(identity) for identity in identities if identity]
                    )
                )
            )
            if identities
            else []
        )
        return any(alias in gateway_model_alias_candidates(row) for row in rows)

    def public_list(
        self, db: Session, *, account_id: Any, resource_type: str
    ) -> list[SharedResourceRead]:
        """List only shared public projections for an account."""
        result = [
            self.public_read(
                db, account_id=account_id, resource_type=resource_type, resource_id=key
            )
            for key in self.visible_ids(
                db, account_id=account_id, resource_type=resource_type
            )
        ]
        return [row for row in result if row is not None]

    def shared_agent_budget_policies(
        self, db: Session, *, account_id: Any, agent_id: Any
    ) -> list[Any]:
        """The live owner's managed-agent ceilings, consumed alongside child budgets."""
        if not sharing_enabled():
            return []
        agent = self.visible_resource(
            db,
            account_id=account_id,
            resource_type="managed_agent",
            resource_id=agent_id,
        )
        if agent is None and self.shared_agent_spend_owner(
            db, account_id=account_id, agent_id=agent_id
        ):
            agent = db.get(models.ManagedAgent, _id(agent_id))
        if agent is None or str(agent.account_id) == str(account_id):
            return []
        return list(
            db.scalars(
                select(models.BudgetPolicy).where(
                    models.BudgetPolicy.account_id == agent.account_id,
                    models.BudgetPolicy.subject_type == "managed_agent",
                    models.BudgetPolicy.subject_id == agent.id,
                )
            )
        )

    def shared_agent_spend_owner(
        self, db: Session, *, account_id: Any, agent_id: Any
    ) -> UUID | None:
        """Resolve stored runtime attribution even when an in-flight share was revoked."""
        if not sharing_enabled():
            return None
        agent = db.get(models.ManagedAgent, _id(agent_id))
        if agent is None or str(agent.account_id) == str(account_id):
            return None
        issued = db.scalar(
            select(models.ApiKey.id)
            .where(
                models.ApiKey.account_id == _id(account_id),
                models.ApiKey.context_data["managed_agent_id"].as_string()
                == str(agent.id),
                models.ApiKey.context_data["shared_agent_owner_account_id"].as_string()
                == str(agent.account_id),
            )
            .limit(1)
        )
        return agent.account_id if issued else None

    def owner_agent_sessions(
        self, db: Session, *, owner_account_id: Any, agent_id: Any
    ) -> list[dict[str, Any]]:
        """Return session metadata for this owner's agent's consuming work only."""
        agent = db.get(models.ManagedAgent, _id(agent_id))
        if agent is None or agent.account_id != _id(owner_account_id):
            raise ShareConflictError("managed agent not found")
        rows = db.scalars(
            select(models.RuntimeSession)
            .join(
                models.AgentControlCommand,
                models.AgentControlCommand.runtime_session_id
                == models.RuntimeSession.id,
            )
            .where(
                models.AgentControlCommand.account_id == _id(owner_account_id),
                models.AgentControlCommand.managed_agent_id == agent.id,
                models.AgentControlCommand.consuming_account_id.is_not(None),
                models.AgentControlCommand.consuming_account_id
                != _id(owner_account_id),
            )
            .distinct()
            .order_by(models.RuntimeSession.started_at.desc())
            .limit(100)
        ).all()
        return [
            {
                "id": str(row.id),
                "account_id": str(row.account_id),
                "started_at": row.started_at,
                "last_activity_at": row.last_activity_at,
                "ended_at": row.ended_at,
            }
            for row in rows
        ]

    def bound_agent(self, db: Session, *, key: Any) -> Any:
        """Resolve a server-issued runtime binding even after sharing is revoked.

        Existing sessions keep their binding; new targeting requires a live receipt.
        """
        context = key.context_data or {}
        agent_id = context.get("managed_agent_id")
        owner_id = context.get("shared_agent_owner_account_id")
        session_id = context.get("runtime_session_id")
        if not agent_id or not owner_id or not session_id:
            return None
        agent = db.get(models.ManagedAgent, _id(agent_id))
        runtime = db.get(models.RuntimeSession, _id(session_id))
        if (
            agent is None
            or runtime is None
            or str(agent.account_id) != str(owner_id)
            or str(runtime.account_id) != str(key.account_id)
        ):
            return None
        return agent

    def shared_agent_governance(
        self,
        db: Session,
        *,
        account_id: Any,
        agent_id: Any,
    ) -> dict[str, Any]:
        """Load the live owner's agent policy for a consuming runtime."""
        from copy import deepcopy
        from preloop.services.subject_governance import get_subject_governance

        if not agent_id or not sharing_enabled():
            return {}
        agent = self.visible_resource(
            db,
            account_id=account_id,
            resource_type="managed_agent",
            resource_id=agent_id,
        )
        if agent is None and self.shared_agent_spend_owner(
            db, account_id=account_id, agent_id=agent_id
        ):
            agent = db.get(models.ManagedAgent, _id(agent_id))
        if agent is None or str(agent.account_id) == str(account_id):
            return {}
        owner = db.get(models.Account, agent.account_id)
        return (
            deepcopy(
                get_subject_governance(
                    owner.meta_data or {},
                    subject_type="managed_agents",
                    subject_id=str(agent.id),
                )
            )
            if owner
            else {}
        )

    def owner_tool_rules(
        self,
        db: Session,
        *,
        account_id: Any,
        server: models.MCPServer,
        tool_name: str,
    ) -> tuple[bool, bool, list[Any]]:
        """Return shared-server owner enablement, approval and ordered rule ceiling."""
        from preloop.models.crud import crud_tool_access_rule

        config = db.scalar(
            select(models.ToolConfiguration).where(
                models.ToolConfiguration.account_id == server.account_id,
                models.ToolConfiguration.mcp_server_id == server.id,
                models.ToolConfiguration.tool_name == tool_name,
                models.ToolConfiguration.managed_agent_id.is_(None),
            )
        )
        mandatory = self.requires_approval(
            db, account_id=account_id, server_id=server.id
        )
        if config is None:
            return True, mandatory, []
        rules = crud_tool_access_rule.get_multi_by_config(
            db,
            config_id=str(config.id),
            account_id=str(server.account_id),
            enabled_only=True,
        )
        return config.is_enabled, mandatory or config.requires_approval, rules

    def bind_runtime_key(
        self,
        db: Session,
        *,
        account_id: Any,
        agent: models.ManagedAgent,
        token: str,
        runtime_session_id: Any,
    ) -> None:
        """Bind a consumer credential to the live shared target using stored context."""
        from preloop.models.crud import crud_api_key

        visible = self.visible_resource(
            db,
            account_id=account_id,
            resource_type="managed_agent",
            resource_id=agent.id,
        )
        key = crud_api_key.get_by_key(db, key=token, account_id=str(account_id))
        if visible is None or key is None or not key.is_active:
            raise ShareConflictError("shared runtime credential unavailable")
        key.context_data = {
            **(key.context_data or {}),
            "managed_agent_id": str(agent.id),
            "shared_agent_owner_account_id": str(agent.account_id),
            "runtime_session_id": str(runtime_session_id),
        }
        db.flush()

    def requires_approval(
        self, db: Session, *, account_id: Any, server_id: Any
    ) -> bool:
        """Return the mandatory owner ceiling for a currently shared server."""
        return bool(
            db.scalar(
                select(models.ResourceShare.id)
                .join(
                    models.ResourceShareRecipient,
                    models.ResourceShareRecipient.share_id == models.ResourceShare.id,
                )
                .where(
                    models.ResourceShareRecipient.recipient_account_id
                    == _id(account_id),
                    models.ResourceShareRecipient.resource_type == "mcp_server",
                    models.ResourceShareRecipient.resource_id == _id(server_id),
                    models.ResourceShare.require_approval.is_(True),
                )
            )
        )

    def reconcile(
        self,
        db: Session,
        *,
        owner_account_ids: Iterable[Any] | None = None,
        actor_id: UUID | None = None,
    ) -> None:
        """Materialize selectors only on writes; callback is pure enterprise policy."""
        if _selector is None:
            return
        owners = (
            db.scalars(
                select(models.Account)
                .where(models.Account.id.in_([_id(key) for key in owner_account_ids]))
                .order_by(models.Account.id)
                .with_for_update()
            )
            if owner_account_ids
            else db.scalars(
                select(models.Account).order_by(models.Account.id).with_for_update()
            )
        )
        for owner in owners:
            children = list(descendants(db, owner))
            path_ids = list(owner.hierarchy_path or [owner.id])
            rules = db.scalars(
                select(models.AccessRule).where(
                    models.AccessRule.account_id.in_(path_ids),
                    models.AccessRule.is_enabled.is_(True),
                )
            ).all()
            rule_data = [
                {
                    **{
                        key: getattr(rule, key)
                        for key in AccessRuleDefinition.model_fields
                    },
                    "account_id": str(rule.account_id),
                }
                for rule in rules
            ]
            tags = {
                (tag.resource_type, str(tag.resource_id), tag.key): tag.value
                for tag in db.scalars(
                    select(models.ResourceTag).where(
                        models.ResourceTag.account_id.in_(
                            [*path_ids, *(child.id for child in children)]
                        )
                    )
                )
            }

            def account_context(
                account: models.Account,
                tag_index: dict[tuple[str, str, str], str] = tags,
            ) -> dict[str, Any]:
                return {
                    "id": str(account.id),
                    "path": [str(key) for key in account.hierarchy_path],
                    "tags": {
                        key: value
                        for (kind, identity, key), value in tag_index.items()
                        if kind == "account" and identity == str(account.id)
                    },
                }

            resources = {
                (kind, row.id): row
                for kind, model in RESOURCE_MODELS.items()
                for row in db.scalars(select(model).where(model.account_id == owner.id))
            }
            shares = list(
                db.scalars(
                    select(models.ResourceShare).where(
                        models.ResourceShare.owner_account_id == owner.id,
                        models.ResourceShare.revoked_at.is_(None),
                    )
                )
            )
            # Permit share rules are standing intent: tagged resources join or
            # leave without requiring an endpoint write for each resource.
            existing_auto = {
                (share.resource_type, share.resource_id, share.access_rule_id)
                for share in shares
                if share.is_automatic
            }
            for rule in rules:
                if (
                    rule.account_id != owner.id
                    or rule.effect != "permit"
                    or "resource:share" not in rule.actions
                    or rule.scope == "self"
                ):
                    continue
                for kind, identity in resources:
                    if rule.resource_type and rule.resource_type != kind:
                        continue
                    if (kind, identity, rule.id) not in existing_auto:
                        share = models.ResourceShare(
                            id=uuid4(),
                            owner_account_id=owner.id,
                            resource_type=kind,
                            resource_id=identity,
                            target_mode="rule",
                            access_rule_id=rule.id,
                            is_automatic=True,
                            selected_account_ids=[],
                            require_approval=False,
                        )
                        db.add(share)
                        shares.append(share)
            db.flush()
            for share in shares:
                row = resources.get((share.resource_type, share.resource_id))
                desired: set[UUID] = set()
                if row is not None:
                    resource = {
                        "type": share.resource_type,
                        "id": str(row.id),
                        "account_id": str(owner.id),
                        "account": account_context(owner),
                        "tags": {
                            key: value
                            for (kind, identity, key), value in tags.items()
                            if kind == share.resource_type and identity == str(row.id)
                        },
                    }
                    for child in children:
                        if (
                            share.target_mode == "selected"
                            and child.id not in share.selected_account_ids
                        ):
                            continue
                        subject = {
                            "kind": "flow",
                            "id": str(child.id),
                            "account": account_context(child),
                            "home_account": account_context(child),
                            "tags": {},
                            "roles": [],
                            "teams": [],
                        }
                        applicable = [
                            rule for rule in rule_data if rule["scope"] != "self"
                        ]
                        if _selector(
                            applicable,
                            subject,
                            resource,
                            share.access_rule_id
                            if share.target_mode == "rule"
                            else None,
                        ):
                            desired.add(child.id)
                current = list(
                    db.scalars(
                        select(models.ResourceShareRecipient).where(
                            models.ResourceShareRecipient.share_id == share.id
                        )
                    )
                )
                actual = {row.recipient_account_id for row in current}
                for recipient in current:
                    if recipient.recipient_account_id not in desired:
                        db.delete(recipient)
                for target in desired - actual:
                    db.add(
                        models.ResourceShareRecipient(
                            share_id=share.id,
                            recipient_account_id=target,
                            owner_account_id=owner.id,
                            resource_type=share.resource_type,
                            resource_id=share.resource_id,
                        )
                    )
                if desired != actual:
                    db.flush()
                    _audit(
                        db,
                        owner.id,
                        desired ^ actual,
                        actor_id,
                        "resource_share_targets_changed",
                        {
                            "share_id": str(share.id),
                            "added_account_ids": sorted(
                                str(key) for key in desired - actual
                            ),
                            "removed_account_ids": sorted(
                                str(key) for key in actual - desired
                            ),
                        },
                    )


crud_resource_share = CRUDResourceShare()


def _collect_changes(db: Session, flush_context: Any, instances: Any) -> None:
    if db.info.get("preloop_share_reconciling"):
        return
    affected = db.info.setdefault("preloop_share_owners", set())
    removed = db.info.setdefault("preloop_share_deleted_rules", {})
    for rule in list(db.deleted):
        if not isinstance(rule, models.AccessRule):
            continue
        for share in db.scalars(
            select(models.ResourceShare).where(
                models.ResourceShare.access_rule_id == rule.id
            )
        ):
            if share.id not in removed:
                targets = list(
                    db.scalars(
                        select(
                            models.ResourceShareRecipient.recipient_account_id
                        ).where(models.ResourceShareRecipient.share_id == share.id)
                    )
                )
                removed[share.id] = (share.owner_account_id, share.resource_id, targets)
    for row in [*db.new, *db.dirty, *db.deleted]:
        if isinstance(
            row,
            (
                models.Account,
                models.AccessRule,
                models.ResourceTag,
                *RESOURCE_MODELS.values(),
            ),
        ):
            key = (
                row.id
                if isinstance(row, models.Account)
                else getattr(row, "account_id", None)
            )
            if key:
                affected.add(key)
            if isinstance(row, models.Account):
                affected.update(row.hierarchy_path or [])
                history = inspect(row).attrs.hierarchy_path.history
                for previous in history.deleted:
                    affected.update(previous or [])
            elif getattr(row, "account_id", None):
                history = inspect(row).attrs.account_id.history
                affected.update(history.deleted)


def _before_commit(db: Session) -> None:
    if db.info.get("preloop_share_reconciling"):
        return
    _collect_changes(db, None, None)
    affected = db.info.pop("preloop_share_owners", set())
    if not affected:
        return
    for path in db.scalars(
        select(models.Account.hierarchy_path).where(models.Account.id.in_(affected))
    ):
        affected.update(path or [])
    db.info["preloop_share_reconciling"] = True
    try:
        db.flush()
        crud_resource_share.reconcile(db, owner_account_ids=affected)
        for share_id, (owner, resource_id, targets) in db.info.pop(
            "preloop_share_deleted_rules", {}
        ).items():
            _audit(
                db,
                owner,
                targets,
                None,
                "resource_share_targets_changed",
                {
                    "share_id": str(share_id),
                    "resource_id": str(resource_id),
                    "added_account_ids": [],
                    "removed_account_ids": [str(key) for key in targets],
                },
            )
    finally:
        db.info.pop("preloop_share_reconciling", None)
        db.info.pop("preloop_share_owners", None)


def _after_rollback(db: Session, previous_transaction: Any) -> None:
    """Discard write receipts when their transaction is rolled back."""
    db.info.pop("preloop_share_owners", None)
    db.info.pop("preloop_share_deleted_rules", None)


def install_materializer(selector: ShareSelector) -> None:
    """Enable transactional reconciliation only when the EE phase is loaded."""
    global _selector
    _selector = selector
    if not event.contains(Session, "before_flush", _collect_changes):
        event.listen(Session, "before_flush", _collect_changes)
        event.listen(Session, "before_commit", _before_commit)
        event.listen(Session, "after_soft_rollback", _after_rollback)


def uninstall_materializer() -> None:
    """Remove listeners during shutdown and test cleanup."""
    global _selector
    _selector = None
    for name, callback in (
        ("before_flush", _collect_changes),
        ("before_commit", _before_commit),
        ("after_soft_rollback", _after_rollback),
    ):
        if event.contains(Session, name, callback):
            event.remove(Session, name, callback)


def validate_policy_section(
    db: Session,
    account_id: UUID,
    user_id: UUID | None,
    shares: list[ResourceShareDefinition],
) -> None:
    """Reject YAML intents unless the owning account admin may write shares."""
    from preloop.utils.permissions import user_holds_permission

    account = db.get(models.Account, account_id)
    user = db.get(models.User, user_id) if user_id else None
    if (
        account is None
        or user is None
        or user.account_id != account_id
        or not user_holds_permission(db, user, "manage_account")
    ):
        raise ShareConflictError("resource sharing requires the owning account admin")
    children = {row.id for row in descendants(db, account)}
    resources = set()
    identities = set()
    for share in shares:
        if (share.resource_type, share.resource_id) in resources or (
            share.id and share.id in identities
        ):
            raise ShareConflictError("duplicate sharing intent or identity")
        resources.add((share.resource_type, share.resource_id))
        identities.add(share.id)
        row = db.get(RESOURCE_MODELS[share.resource_type], share.resource_id)
        if row is None or row.account_id != account_id:
            raise ShareConflictError("resource not found in owning account")
        if not set(share.selected_account_ids).issubset(children):
            raise ShareConflictError(
                "selected accounts must be descendants of the owner"
            )
        if share.id:
            existing = db.get(models.ResourceShare, share.id)
            if existing and (
                existing.owner_account_id != account_id
                or existing.is_automatic
                or existing.revoked_at
            ):
                raise ShareConflictError(
                    "share identity belongs to another or revoked intent"
                )
        if share.access_rule_id:
            rule = db.get(models.AccessRule, share.access_rule_id)
            if (
                rule is None
                or rule.account_id != account_id
                or rule.effect != "permit"
                or "resource:share" not in rule.actions
            ):
                raise ShareConflictError(
                    "rule must be an owner permit for resource:share"
                )


def apply_policy_section(
    db: Session, account_id: UUID, user_id: UUID, shares: list[ResourceShareDefinition]
) -> None:
    """Replace only own explicit intents, recording mirrored/audited targets."""
    validate_policy_section(db, account_id, user_id, shares)
    db.scalar(
        select(models.Account).where(models.Account.id == account_id).with_for_update()
    )
    rows = db.scalars(
        select(models.ResourceShare).where(
            models.ResourceShare.owner_account_id == account_id,
            models.ResourceShare.revoked_at.is_(None),
            models.ResourceShare.is_automatic.is_(False),
        )
    ).all()
    by_id = {row.id: row for row in rows}
    retained = set()
    for definition in shares:
        row = by_id.get(definition.id)
        if row is None:
            result = crud_resource_share.create(
                db,
                owner_account_id=account_id,
                user_id=user_id,
                definition=definition,
                commit=False,
            )
            retained.add(_id(result["id"]))
        else:
            for key, value in definition.model_dump(exclude={"id"}).items():
                setattr(row, key, value)
            retained.add(row.id)
    for row in rows:
        if row.id not in retained:
            crud_resource_share.revoke(
                db,
                owner_account_id=account_id,
                user_id=user_id,
                share_id=row.id,
                commit=False,
            )
    db.flush()
    crud_resource_share.reconcile(db, owner_account_ids=[account_id], actor_id=user_id)
    affected = list(
        db.scalars(
            select(models.ResourceShareRecipient.recipient_account_id).where(
                models.ResourceShareRecipient.owner_account_id == account_id
            )
        )
    )
    _audit(db, account_id, affected, user_id, "resource_shares_imported", {})
