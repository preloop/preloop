"""Account-scoped access-rule storage and detached authorization bundles.

Provider/evaluator code must use this CRUD contract, never endpoint SQL.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Callable, Iterator
from uuid import UUID, uuid4

from sqlalchemy import Engine, func, or_, select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.db.session import get_engine
from preloop.schemas.access_rule import AccessRuleDefinition

RULE_FIELDS = tuple(AccessRuleDefinition.model_fields.keys())
RESOURCE_MODELS = {
    "ai_model": models.AIModel,
    "mcp_server": models.MCPServer,
    "managed_agent": models.ManagedAgent,
    "flow": models.Flow,
    "runner": models.FlowRunner,
    "mcp_tool": models.ToolConfiguration,
    "policy": models.PolicySnapshot,
    "policy_baseline": models.PolicySnapshot,
    "tracker": models.Tracker,
    "api_key": models.ApiKey,
}


class AccessRuleConflictError(ValueError):
    """Missing, inherited or stale policy mutation, safe for an HTTP conflict."""


class AccessReferenceError(ValueError):
    def __init__(self, field: str, reference: str) -> None:
        self.field = field
        super().__init__(
            f"{field}: unknown or ambiguous account-owned reference {reference!r}"
        )


def rule_definition(row: models.AccessRule) -> dict[str, Any]:
    return {key: getattr(row, key) for key in RULE_FIELDS}


def policy_section(db: Session, account_id: UUID) -> dict[str, Any]:
    account = db.get(models.Account, account_id)
    if account is None:
        raise AccessRuleConflictError("account not found")
    rows = db.scalars(
        select(models.AccessRule)
        .where(models.AccessRule.account_id == account_id)
        .order_by(models.AccessRule.priority, models.AccessRule.id)
    ).all()
    return {
        "access_rules": [
            AccessRuleDefinition.model_validate(rule_definition(row)).model_dump(
                mode="json", by_alias=True
            )
            for row in rows
        ],
        "access_rule_mode": dict(
            (account.meta_data or {}).get("access_rule_mode") or {}
        ),
    }


def _references(
    db: Session, definition: AccessRuleDefinition, account: models.Account
) -> AccessRuleDefinition:
    value = definition.model_dump(mode="json", by_alias=True)
    accounts = list(account.hierarchy_path or [account.id])
    for field, model in (("teams", models.Team), ("roles", models.Role)):
        scope = model.account_id.in_(accounts)
        if field == "roles":
            scope = or_(scope, model.account_id.is_(None))
        available = db.scalars(select(model).where(scope)).all()
        resolved = []
        for reference in value["subject_selector"][field]:
            matches = [
                row
                for row in available
                if str(row.id) == reference or row.name == reference
            ]
            if len(matches) != 1:
                raise AccessReferenceError(f"subject_selector.{field}", reference)
            resolved.append(str(matches[0].id))
        value["subject_selector"][field] = resolved
    return AccessRuleDefinition.model_validate(value)


def _record_change(
    db: Session,
    account_id: UUID,
    user_id: UUID | None,
    action: str,
    details: dict[str, Any],
    resource_type: str = "access_rule",
) -> None:
    from preloop.models.crud.audit_log import crud_audit_log
    from preloop.services.policy.loader import export_current_policy

    db.flush()
    document = export_current_policy(
        db, account_id, include_credentials=True
    ).model_dump(mode="json", exclude_none=True)
    document.update(policy_section(db, account_id))
    version = (
        db.scalar(
            select(func.max(models.PolicySnapshot.version_number)).where(
                models.PolicySnapshot.account_id == account_id
            )
        )
        or 0
    ) + 1
    db.add(
        models.PolicySnapshot(
            account_id=account_id,
            version_number=version,
            description=action,
            snapshot_data=document,
            created_by_user_id=user_id,
            mcp_servers_count=len(document.get("mcp_servers") or []),
            policies_count=len(document.get("approval_workflows") or []),
            tools_count=len(document.get("tools") or []),
        )
    )
    crud_audit_log.log_action(
        db,
        account_id=account_id,
        user_id=user_id,
        action="configuration_change",
        resource_type=resource_type,
        status="success",
        details={"config_type": "access_rules", "operation": action, **details},
        commit=False,
    )


class CRUDAccessRule:
    def __init__(self, engine: Engine | None = None) -> None:
        self.engine = engine

    @contextmanager
    def session(self) -> Iterator[Session]:
        with Session(self.engine or get_engine(), expire_on_commit=False) as db:
            yield db

    def rules(self, *, account_id: UUID) -> list[dict[str, Any]]:
        with self.session() as db:
            account = db.get(models.Account, account_id)
            if not account:
                raise AccessRuleConflictError("account not found")
            ancestors = list(account.hierarchy_path or [account.id])[:-1]
            rows = db.scalars(
                select(models.AccessRule)
                .where(
                    or_(
                        models.AccessRule.account_id == account_id,
                        (
                            models.AccessRule.account_id.in_(ancestors)
                            & models.AccessRule.scope.in_(
                                ["subaccounts", "self_and_subaccounts"]
                            )
                        ),
                    )
                )
                .order_by(models.AccessRule.priority, models.AccessRule.id)
            ).all()
            return [
                {
                    **row.to_dict(),
                    "inherited": row.account_id != account_id,
                    "editable": row.account_id == account_id,
                }
                for row in rows
            ]

    def write(
        self,
        *,
        account_id: UUID,
        user_id: UUID,
        definition: AccessRuleDefinition,
        rule_id: UUID | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        with self.session() as db, db.begin():
            account = db.scalar(
                select(models.Account)
                .where(models.Account.id == account_id)
                .with_for_update()
            )
            if not account:
                raise AccessRuleConflictError("account not found")
            validated = _references(db, definition, account)
            value = validated.model_dump(mode="json", by_alias=True, exclude={"id"})
            if rule_id:
                row = db.scalar(
                    select(models.AccessRule)
                    .where(
                        models.AccessRule.account_id == account_id,
                        models.AccessRule.id == rule_id,
                    )
                    .with_for_update()
                )
                if (
                    row is None
                    or expected_version is None
                    or row.version != expected_version
                ):
                    raise AccessRuleConflictError(
                        "rule is missing, inherited or changed"
                    )
                for key, field in value.items():
                    setattr(row, key, field)
                row.version += 1
            else:
                row = models.AccessRule(
                    id=uuid4(), account_id=account_id, created_by=user_id, **value
                )
                db.add(row)
            _record_change(
                db, account_id, user_id, "access_rule_saved", {"rule_id": str(row.id)}
            )
            db.flush()
            return row.to_dict()

    def delete(
        self, *, account_id: UUID, user_id: UUID, rule_id: UUID, expected_version: int
    ) -> None:
        with self.session() as db, db.begin():
            account = db.scalar(
                select(models.Account)
                .where(models.Account.id == account_id)
                .with_for_update()
            )
            row = db.scalar(
                select(models.AccessRule)
                .where(
                    models.AccessRule.account_id == account_id,
                    models.AccessRule.id == rule_id,
                )
                .with_for_update()
            )
            if account is None or row is None or row.version != expected_version:
                raise AccessRuleConflictError("rule is missing, inherited or changed")
            db.delete(row)
            _record_change(
                db,
                account_id,
                user_id,
                "access_rule_deleted",
                {"rule_id": str(rule_id)},
            )

    def set_mode(
        self,
        *,
        account_id: UUID,
        user_id: UUID,
        action: str,
        mode: str,
        expected_generation: int,
        losing_subjects: list[dict[str, Any]],
    ) -> None:
        from preloop.schemas.access_rule import Action, Mode
        from pydantic import TypeAdapter

        TypeAdapter(Action).validate_python(action)
        TypeAdapter(Mode).validate_python(mode)
        with self.session() as db, db.begin():
            account = db.scalar(
                select(models.Account)
                .where(models.Account.id == account_id)
                .with_for_update()
            )
            if account is None or account.access_rule_generation != expected_generation:
                raise AccessRuleConflictError("policy changed since preview")
            metadata = dict(account.meta_data or {})
            modes = dict(metadata.get("access_rule_mode") or {})
            before = modes.get(action, "additive")
            if before == mode:
                raise AccessRuleConflictError(
                    "mode already saved; preview cannot be replayed"
                )
            modes[action] = mode
            metadata["access_rule_mode"] = modes
            account.meta_data = metadata
            _record_change(
                db,
                account_id,
                user_id,
                "access_rule_mode_changed",
                {
                    "action": action,
                    "before": before,
                    "after": mode,
                    "losing_subjects": losing_subjects,
                },
            )

    def generation(self, *, account_id: UUID) -> int:
        with self.session() as db:
            generation = db.scalar(
                select(models.Account.access_rule_generation).where(
                    models.Account.id == account_id
                )
            )
            if generation is None:
                raise AccessRuleConflictError("account not found")
            return int(generation)

    def bundle(self, *, account_id: UUID) -> dict[str, Any]:
        """Load one consistent, secret-free account snapshot for an evaluator cache."""
        with self.session() as db:
            db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            current = db.get(models.Account, account_id)
            if not current:
                raise AccessRuleConflictError("account not found")
            path = list(current.hierarchy_path or [current.id])
            account_rows = db.scalars(
                select(models.Account).where(models.Account.id.in_(path))
            ).all()
            accounts = {
                str(row.id): {
                    "id": str(row.id),
                    "path": [str(key) for key in row.hierarchy_path],
                    "tags": {},
                }
                for row in account_rows
            }
            tags: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
            for row in db.scalars(
                select(models.ResourceTag).where(
                    models.ResourceTag.account_id.in_(path)
                )
            ):
                tags[(row.resource_type, str(row.resource_id))][row.key] = row.value
            for key, account in accounts.items():
                account["tags"] = tags.get(("account", key), {})
            users = db.scalars(
                select(models.User).where(models.User.account_id.in_(path))
            ).all()
            persons = db.scalars(
                select(models.Person).where(
                    models.Person.id.in_([user.person_id for user in users])
                )
            ).all()
            primary = {str(person.id): person.primary_user_id for person in persons}
            primary_users = db.scalars(
                select(models.User).where(
                    models.User.id.in_([key for key in primary.values() if key])
                )
            ).all()
            user_accounts = {
                str(user.id): str(user.account_id) for user in [*users, *primary_users]
            }
            home_ids = list({user.account_id for user in primary_users} - set(path))
            for row in db.scalars(
                select(models.Account).where(models.Account.id.in_(home_ids))
            ):
                accounts[str(row.id)] = {
                    "id": str(row.id),
                    "path": [str(key) for key in row.hierarchy_path],
                    "tags": {},
                }
            for row in db.scalars(
                select(models.ResourceTag).where(
                    models.ResourceTag.account_id.in_(home_ids),
                    models.ResourceTag.resource_type == "account",
                )
            ):
                accounts[str(row.resource_id)]["tags"][row.key] = row.value
            teams: dict[str, set[str]] = defaultdict(set)
            roles: dict[str, set[str]] = defaultdict(set)
            user_ids = [user.id for user in users]
            for row in db.scalars(
                select(models.TeamMembership)
                .join(models.Team, models.Team.id == models.TeamMembership.team_id)
                .where(
                    models.TeamMembership.user_id.in_(user_ids),
                    models.Team.account_id.in_(path),
                )
            ):
                teams[str(row.user_id)].add(str(row.team_id))
            grants = {
                str(row.id): row
                for row in db.scalars(
                    select(models.AccountAccessGrant).where(
                        models.AccountAccessGrant.parent_account_id.in_(path),
                        models.AccountAccessGrant.revoked_at.is_(None),
                    )
                )
            }
            for user in users:
                grant = grants.get(str(user.access_grant_id))
                if (
                    user.membership_kind == "inherited"
                    and grant
                    and grant.subject_type == "team"
                ):
                    teams[str(user.id)].add(str(grant.subject_id))
            for row in db.scalars(
                select(models.UserRole)
                .join(models.Role, models.Role.id == models.UserRole.role_id)
                .where(
                    models.UserRole.user_id.in_(user_ids),
                    or_(
                        models.Role.account_id.in_(path),
                        models.Role.account_id.is_(None),
                    ),
                )
            ):
                roles[str(row.user_id)].add(str(row.role_id))
            team_roles = db.scalars(
                select(models.TeamRole)
                .join(models.Role, models.Role.id == models.TeamRole.role_id)
                .where(
                    models.TeamRole.team_id.in_(
                        {UUID(team) for values in teams.values() for team in values}
                    ),
                    or_(
                        models.Role.account_id.in_(path),
                        models.Role.account_id.is_(None),
                    ),
                )
            ).all()
            for user_id, held in teams.items():
                roles[user_id].update(
                    str(row.role_id) for row in team_roles if str(row.team_id) in held
                )
            subjects: dict[tuple[str, str], dict[str, Any]] = {}
            for user in users:
                if user.account_id != account_id:
                    continue
                key = str(user.id)
                home = user_accounts.get(
                    str(primary.get(str(user.person_id))), str(account_id)
                )
                subjects[("user", key)] = {
                    "kind": "user",
                    "id": key,
                    "roles": sorted(roles[key]),
                    "teams": sorted(teams[key]),
                    "membership_kind": user.membership_kind,
                    "home_account": accounts.get(home, accounts[str(account_id)]),
                    "account": accounts[str(account_id)],
                    "tags": {},
                }
            resources = {}
            for kind, model in RESOURCE_MODELS.items():
                # Select identifiers only: owner tokens, agent system metadata and
                # arbitrary customer config never enter this cache or explain DTOs.
                columns = [model.id, model.account_id]
                if kind == "runner":
                    columns.append(model.labels)
                visible = model.account_id.in_(path)
                if kind in ("ai_model", "mcp_server"):
                    visible = or_(visible, model.account_id.is_(None))
                for row in db.execute(select(*columns).where(visible)):
                    key = str(row.id)
                    resources[(kind, key)] = {
                        "type": kind,
                        "id": key,
                        "account_id": str(row.account_id),
                        "account": accounts.get(
                            str(row.account_id), {"id": None, "tags": {}, "path": []}
                        ),
                        "tags": tags.get((kind, key), {}),
                        "labels": row.labels if kind == "runner" else {},
                    }
                    if row.account_id == account_id and kind in (
                        "managed_agent",
                        "flow",
                        "api_key",
                    ):
                        subject_kind = "agent" if kind == "managed_agent" else kind
                        subjects[(subject_kind, key)] = {
                            "kind": subject_kind,
                            "id": key,
                            "roles": [],
                            "teams": [],
                            "membership_kind": "direct",
                            "account": accounts[str(account_id)],
                            "home_account": accounts[str(account_id)],
                            "tags": tags.get((kind, key), {}),
                        }
            shared_agents = db.scalars(
                select(models.ManagedAgent)
                .join(
                    models.ResourceShareRecipient,
                    models.ResourceShareRecipient.resource_id == models.ManagedAgent.id,
                )
                .where(
                    models.ResourceShareRecipient.recipient_account_id == account_id,
                    models.ResourceShareRecipient.resource_type == "managed_agent",
                )
                .distinct()
            ).all()
            from preloop.models.crud.resource_share import crud_resource_share

            # Issued consumer-session credentials preserve their agent identity
            # after rescinding; only new targeting depends on live recipients.
            by_id = {agent.id: agent for agent in shared_agents}
            for key in db.scalars(
                select(models.ApiKey).where(
                    models.ApiKey.account_id == account_id,
                    models.ApiKey.context_data["shared_agent_owner_account_id"]
                    .as_string()
                    .is_not(None),
                )
            ):
                agent = crud_resource_share.bound_agent(db, key=key)
                if agent is not None:
                    by_id[agent.id] = agent
            shared_agents = list(by_id.values())
            # A detached consumer may still finish an issued session; its
            # former owner is no longer in the hierarchy path.
            missing_owners = {
                agent.account_id
                for agent in shared_agents
                if str(agent.account_id) not in accounts
            }
            for owner in db.scalars(
                select(models.Account).where(models.Account.id.in_(missing_owners))
            ):
                accounts[str(owner.id)] = {
                    "id": str(owner.id),
                    "path": [str(key) for key in owner.hierarchy_path],
                    "tags": {},
                }
            shared_agent_ids = {
                str(agent.id): str(agent.account_id) for agent in shared_agents
            }
            for agent in shared_agents:
                key = str(agent.id)
                subjects[("agent", key)] = {
                    "kind": "agent",
                    "id": key,
                    "roles": [],
                    "teams": [],
                    "membership_kind": "inherited",
                    "account": accounts[str(account_id)],
                    "home_account": accounts[str(agent.account_id)],
                    "tags": tags.get(("managed_agent", key), {}),
                }
            key_agents = {
                str(row.api_key_id): str(row.managed_agent_id)
                for row in db.scalars(
                    select(models.ManagedAgentCredential).where(
                        models.ManagedAgentCredential.account_id == account_id,
                        models.ManagedAgentCredential.status == "active",
                    )
                )
            }
            agents_by_source = {
                (row.session_source_type, row.session_source_id): str(row.id)
                for row in db.scalars(
                    select(models.ManagedAgent).where(
                        models.ManagedAgent.account_id == account_id
                    )
                )
            }
            runtime_agents = {
                str(row.id): agents_by_source.get(
                    (row.session_source_type, row.session_source_id)
                )
                for row in db.execute(
                    select(
                        models.RuntimeSession.id,
                        models.RuntimeSession.session_source_type,
                        models.RuntimeSession.session_source_id,
                    ).where(models.RuntimeSession.account_id == account_id)
                )
            }
            for row in db.execute(
                select(
                    models.ApiKey.id, models.ApiKey.user_id, models.ApiKey.context_data
                ).where(models.ApiKey.account_id == account_id)
            ):
                subject = subjects.get(("api_key", str(row.id)))
                owner = subjects.get(("user", str(row.user_id)))
                if subject and owner:
                    for attribute in (
                        "roles",
                        "teams",
                        "membership_kind",
                        "home_account",
                    ):
                        subject[attribute] = owner[attribute]
                shared_id = str((row.context_data or {}).get("managed_agent_id") or "")
                shared_owner = str(
                    (row.context_data or {}).get("shared_agent_owner_account_id") or ""
                )
                if shared_agent_ids.get(shared_id) == shared_owner:
                    key_agents[str(row.id)] = shared_id
                if str(row.id) not in key_agents:
                    runtime = str(
                        (row.context_data or {}).get("runtime_session_id") or ""
                    )
                    if runtime_agents.get(runtime):
                        key_agents[str(row.id)] = runtime_agents[runtime]
            rules = db.scalars(
                select(models.AccessRule)
                .where(
                    models.AccessRule.account_id.in_(path),
                    models.AccessRule.is_enabled.is_(True),
                )
                .order_by(models.AccessRule.priority, models.AccessRule.id)
            ).all()
            applicable = [
                {**rule_definition(row), "account_id": str(row.account_id)}
                for row in rules
                if (row.account_id == account_id and row.scope != "subaccounts")
                or (row.account_id != account_id and row.scope != "self")
            ]
            modes = {}
            ancestor_require_permit = set()
            for row in account_rows:
                for action, mode in (
                    (row.meta_data or {}).get("access_rule_mode") or {}
                ).items():
                    if row.id != account_id and mode == "require_permit":
                        ancestor_require_permit.add(action)
                    if mode == "require_permit" or action not in modes:
                        modes[action] = mode
            return {
                "account_id": str(account_id),
                "generation": current.access_rule_generation,
                "accounts": accounts,
                "rules": applicable,
                "modes": modes,
                "ancestor_require_permit": sorted(ancestor_require_permit),
                "subjects": subjects,
                "resources": resources,
                "key_agents": key_agents,
            }

    def listen(
        self,
        callback: Callable[[str, int], None],
        ready: threading.Event,
        stop: threading.Event,
    ) -> None:
        """Consume committed generation events on a connection that is not pooled.

        The shared engine's checkout would return to the pool still in
        autocommit with LISTEN registered. A private NullPool engine is
        discarded with the listener.
        """
        import select
        from contextlib import closing

        from sqlalchemy import create_engine
        from sqlalchemy.pool import NullPool

        source = self.engine or get_engine()
        engine = create_engine(source.url, poolclass=NullPool)
        try:
            with closing(engine.raw_connection()) as connection:
                driver = connection.driver_connection
                driver.autocommit = True
                with driver.cursor() as cursor:
                    cursor.execute("LISTEN preloop_access")
                ready.set()
                notifies = getattr(driver, "notifies", None)
                while not stop.is_set():
                    if callable(notifies):
                        for notification in notifies(timeout=1, stop_after=1):
                            _deliver_access_notification(notification, callback)
                        continue
                    select.select([driver], [], [], 1)
                    driver.poll()
                    while driver.notifies:
                        _deliver_access_notification(driver.notifies.pop(0), callback)
        finally:
            engine.dispose()


def _deliver_access_notification(notification: Any, callback: Callable) -> None:
    data = json.loads(notification.payload)
    callback(str(data["account_id"]), int(data["generation"]))


crud_access_rule = CRUDAccessRule()


def validate_policy_section(
    db: Session,
    account_id: UUID,
    rules: list[AccessRuleDefinition] | None,
    modes: dict[str, str] | None,
) -> list[AccessRuleDefinition]:
    """Validate import without letting YAML bypass audited mode preview."""
    account = db.get(models.Account, account_id)
    if not account:
        raise AccessRuleConflictError("account not found")
    current = (account.meta_data or {}).get("access_rule_mode") or {}
    if any(
        mode == "require_permit" and current.get(action) != mode
        for action, mode in (modes or {}).items()
    ):
        raise AccessRuleConflictError(
            "require_permit requires the access mode explain preview endpoint"
        )
    validated = [_references(db, definition, account) for definition in rules or []]
    identities = [str(rule.id) for rule in validated if rule.id]
    names = [rule.name for rule in validated]
    if len(identities) != len(set(identities)) or len(names) != len(set(names)):
        raise AccessRuleConflictError("duplicate access rule id or name")
    for rule in validated:
        if rule.id:
            row = db.get(models.AccessRule, rule.id)
            if row is not None and row.account_id != account_id:
                raise AccessRuleConflictError(
                    "access rule identity belongs to another account"
                )
    return validated


def apply_policy_section(
    db: Session,
    account_id: UUID,
    user_id: UUID | None,
    rules: list[AccessRuleDefinition] | None,
    modes: dict[str, str] | None,
) -> None:
    account = db.scalar(
        select(models.Account).where(models.Account.id == account_id).with_for_update()
    )
    if not account:
        raise AccessRuleConflictError("account not found")
    validated = validate_policy_section(db, account_id, rules, modes)
    if rules is not None:
        existing = db.scalars(
            select(models.AccessRule).where(models.AccessRule.account_id == account_id)
        ).all()
        by_id = {row.id: row for row in existing}
        by_name = {row.name: row for row in existing}
        retained = set()
        for rule in validated:
            row = by_id.get(rule.id) if rule.id else by_name.get(rule.name)
            value = rule.model_dump(mode="json", by_alias=True, exclude={"id"})
            if row:
                for key, field in value.items():
                    setattr(row, key, field)
                row.version += 1
            else:
                row = models.AccessRule(
                    id=rule.id or uuid4(),
                    account_id=account_id,
                    created_by=user_id,
                    **value,
                )
                db.add(row)
            retained.add(row.id)
        for row in existing:
            if row.id not in retained:
                db.delete(row)
    if modes is not None:
        metadata = dict(account.meta_data or {})
        metadata["access_rule_mode"] = dict(modes)
        account.meta_data = metadata
    _record_change(db, account_id, user_id, "access_rules_imported", {})
