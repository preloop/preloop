"""Managed-agent identity rekey and duplicate merge (#112 part b)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from preloop.models.crud import (
    crud_api_key,
    crud_managed_agent,
)
from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.approval_request import ApprovalRequest
from preloop.models.models.budget import BudgetPolicy, BudgetSpendActivity
from preloop.models.models.managed_agent import ManagedAgent
from preloop.models.models.runtime_session import RuntimeSession
from preloop.services.usage_fingerprint import fingerprint_from_usage_row


class ManagedAgentIdentityError(ValueError):
    """Raised when a rekey or merge request is refused."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class PrincipalIdentity:
    """Optional CLI-supplied identity metadata for a runtime principal."""

    hostname: Optional[str] = None
    config_path: Optional[str] = None
    source_type: Optional[str] = None
    derivation: Optional[str] = None


@dataclass
class IdentityMutationCounts:
    """Row counts reported by dry-run and executed identity mutations."""

    usage_moved: int = 0
    usage_deleted: int = 0
    runtime_sessions_moved: int = 0
    budget_spend_moved: int = 0
    budget_spend_merged: int = 0
    budget_policies_moved: int = 0
    budget_policies_dropped: int = 0
    approvals_moved: int = 0
    keys_deactivated: int = 0
    dropped_budget_policies: list[dict[str, Any]] = field(default_factory=list)


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _append_previous_id(agent: ManagedAgent, previous_id: str) -> None:
    tags = dict(agent.tags or {})
    existing = [
        part.strip()
        for part in str(tags.get("identity.previous_ids", "")).split(",")
        if part.strip()
    ]
    if previous_id not in existing:
        existing.append(previous_id)
    tags["identity.previous_ids"] = ",".join(existing)
    agent.tags = tags
    flag_modified(agent, "tags")


def _apply_identity_metadata(
    agent: ManagedAgent, identity: Optional[PrincipalIdentity]
) -> None:
    if identity is None:
        return
    if identity.hostname is not None:
        agent.enrollment_hostname = identity.hostname or None
    if identity.derivation is not None:
        agent.identity_derivation = identity.derivation or None
    if identity.config_path is not None:
        agent.session_reference = identity.config_path or agent.session_reference


def _rewrite_usage_principal(
    db: Session,
    *,
    account_id: Any,
    old_principal_id: str,
    new_principal_id: str,
    delete_fingerprint_collisions: bool,
) -> tuple[int, int]:
    rows = (
        db.query(ApiUsage)
        .filter(
            ApiUsage.account_id == account_id,
            ApiUsage.runtime_principal_id == old_principal_id,
        )
        .all()
    )
    moved = 0
    deleted = 0
    survivor_fps: set[str] = set()
    if delete_fingerprint_collisions:
        survivor_rows = (
            db.query(ApiUsage)
            .filter(
                ApiUsage.account_id == account_id,
                ApiUsage.runtime_principal_id == new_principal_id,
                ApiUsage.action_type == "imported_usage",
            )
            .all()
        )
        for row in survivor_rows:
            meta = dict(row.meta_data or {})
            fp = meta.get("import_fingerprint")
            if fp:
                survivor_fps.add(str(fp))

    for row in rows:
        if row.action_type == "imported_usage" and delete_fingerprint_collisions:
            new_fp = fingerprint_from_usage_row(
                row, agent_principal_id=new_principal_id
            )
            if new_fp and new_fp in survivor_fps:
                db.delete(row)
                deleted += 1
                continue
            meta = dict(row.meta_data or {})
            if new_fp:
                meta["import_fingerprint"] = new_fp
                row.meta_data = meta
                flag_modified(row, "meta_data")
                survivor_fps.add(new_fp)
        elif row.action_type == "imported_usage":
            new_fp = fingerprint_from_usage_row(
                row, agent_principal_id=new_principal_id
            )
            if new_fp:
                meta = dict(row.meta_data or {})
                meta["import_fingerprint"] = new_fp
                row.meta_data = meta
                flag_modified(row, "meta_data")
        row.runtime_principal_id = new_principal_id
        db.add(row)
        moved += 1
    return moved, deleted


def _rewrite_runtime_sessions(
    db: Session,
    *,
    account_id: Any,
    old_principal_id: str,
    new_principal_id: str,
) -> int:
    rows = (
        db.query(RuntimeSession)
        .filter(
            RuntimeSession.account_id == account_id,
            RuntimeSession.runtime_principal_id == old_principal_id,
        )
        .all()
    )
    for row in rows:
        row.runtime_principal_id = new_principal_id
        db.add(row)
    return len(rows)


def rekey_managed_agent(
    db: Session,
    *,
    account_id: Any,
    agent_id: str,
    new_session_source_id: str,
    identity: Optional[PrincipalIdentity] = None,
    user_id: Optional[Any] = None,
    commit: bool = True,
) -> tuple[ManagedAgent, IdentityMutationCounts]:
    """Rewrite one managed agent's durable principal id and dependent rows.

    Args:
        db: Database session.
        account_id: Owning account id.
        agent_id: Managed agent UUID.
        new_session_source_id: Target v2 (or other) principal id.
        identity: Optional identity metadata to persist.
        user_id: Acting user for the audit event.
        commit: Whether to commit the transaction.

    Returns:
        Updated agent and mutation counts.

    Raises:
        ManagedAgentIdentityError: On unknown agent or id collision.
    """
    agent = crud_managed_agent.get_for_account(
        db, account_id=str(account_id), agent_id=agent_id
    )
    if agent is None:
        raise ManagedAgentIdentityError("Managed agent not found", status_code=404)

    new_id = new_session_source_id.strip()
    if not new_id:
        raise ManagedAgentIdentityError("new_session_source_id is required")

    counts = IdentityMutationCounts()
    old_id = agent.session_source_id
    if old_id == new_id:
        _apply_identity_metadata(agent, identity)
        db.add(agent)
        if commit:
            db.commit()
            db.refresh(agent)
        else:
            db.flush()
        return agent, counts

    collision = crud_managed_agent.get_by_source(
        db,
        account_id=account_id,
        session_source_type=agent.session_source_type,
        session_source_id=new_id,
    )
    if collision is not None and str(collision.id) != str(agent.id):
        raise ManagedAgentIdentityError(
            "new_session_source_id is already used by another managed agent; "
            "merge the duplicates instead",
            status_code=409,
        )

    moved, deleted = _rewrite_usage_principal(
        db,
        account_id=account_id,
        old_principal_id=old_id,
        new_principal_id=new_id,
        delete_fingerprint_collisions=False,
    )
    counts.usage_moved = moved
    counts.usage_deleted = deleted
    counts.runtime_sessions_moved = _rewrite_runtime_sessions(
        db,
        account_id=account_id,
        old_principal_id=old_id,
        new_principal_id=new_id,
    )

    _append_previous_id(agent, old_id)
    agent.session_source_id = new_id
    _apply_identity_metadata(agent, identity)
    if identity and identity.derivation:
        agent.identity_derivation = identity.derivation
    elif agent.identity_derivation is None:
        agent.identity_derivation = "v2"
    db.add(agent)

    from preloop.models.models.audit_log import AuditLog

    db.add(
        AuditLog(
            account_id=account_id,
            user_id=user_id,
            action="managed_agent.rekey",
            resource_type="managed_agent",
            resource_id=str(agent.id),
            status="success",
            details={
                "old_session_source_id": old_id,
                "new_session_source_id": new_id,
                "counts": {
                    "usage_moved": counts.usage_moved,
                    "runtime_sessions_moved": counts.runtime_sessions_moved,
                },
            },
            timestamp=datetime.now(UTC),
        )
    )
    if commit:
        db.commit()
        db.refresh(agent)
    else:
        db.flush()
        db.refresh(agent)
    return agent, counts


def _agent_has_live_session(agent: ManagedAgent) -> bool:
    session = agent.runtime_session
    if session is None:
        return False
    return session.ended_at is None


def _merge_budget_spend(
    db: Session,
    *,
    account_id: Any,
    duplicate_id: UUID,
    survivor_id: UUID,
) -> tuple[int, int]:
    rows = (
        db.query(BudgetSpendActivity)
        .filter(
            BudgetSpendActivity.account_id == account_id,
            BudgetSpendActivity.subject_type == "managed_agent",
            BudgetSpendActivity.subject_id == duplicate_id,
        )
        .all()
    )
    moved = 0
    merged = 0
    for row in rows:
        existing = (
            db.query(BudgetSpendActivity)
            .filter(
                BudgetSpendActivity.account_id == account_id,
                BudgetSpendActivity.subject_type == "managed_agent",
                BudgetSpendActivity.subject_id == survivor_id,
                BudgetSpendActivity.model_alias == row.model_alias,
                BudgetSpendActivity.period == row.period,
                BudgetSpendActivity.period_start == row.period_start,
            )
            .first()
        )
        if existing is None:
            row.subject_id = survivor_id
            db.add(row)
            moved += 1
            continue
        existing.spend_usd = float(existing.spend_usd or 0.0) + float(
            row.spend_usd or 0.0
        )
        db.add(existing)
        db.delete(row)
        merged += 1
    return moved, merged


def _merge_budget_policies(
    db: Session,
    *,
    account_id: Any,
    duplicate_id: UUID,
    survivor_id: UUID,
) -> tuple[int, int, list[dict[str, Any]]]:
    rows = (
        db.query(BudgetPolicy)
        .filter(
            BudgetPolicy.account_id == account_id,
            BudgetPolicy.subject_type == "managed_agent",
            BudgetPolicy.subject_id == duplicate_id,
        )
        .all()
    )
    moved = 0
    dropped = 0
    dropped_details: list[dict[str, Any]] = []
    for row in rows:
        conflict = (
            db.query(BudgetPolicy)
            .filter(
                BudgetPolicy.account_id == account_id,
                BudgetPolicy.subject_type == "managed_agent",
                BudgetPolicy.subject_id == survivor_id,
                BudgetPolicy.model_alias == row.model_alias,
                BudgetPolicy.period == row.period,
            )
            .first()
        )
        if conflict is None:
            row.subject_id = survivor_id
            db.add(row)
            moved += 1
            continue
        dropped_details.append(
            {
                "kept_policy_id": str(conflict.id),
                "dropped_policy_id": str(row.id),
                "model_alias": row.model_alias,
                "period": str(getattr(row.period, "value", row.period)),
                "kept_hard_limit_usd": conflict.hard_limit_usd,
                "kept_soft_limit_usd": conflict.soft_limit_usd,
                "dropped_hard_limit_usd": row.hard_limit_usd,
                "dropped_soft_limit_usd": row.soft_limit_usd,
            }
        )
        db.delete(row)
        dropped += 1
    return moved, dropped, dropped_details


def merge_managed_agents(
    db: Session,
    *,
    account_id: Any,
    survivor_id: str,
    duplicate_id: str,
    dry_run: bool = False,
    user_id: Optional[Any] = None,
) -> tuple[ManagedAgent, ManagedAgent, IdentityMutationCounts]:
    """Merge a duplicate managed agent into a survivor.

    Args:
        db: Database session.
        account_id: Owning account id.
        survivor_id: Surviving managed agent UUID.
        duplicate_id: Duplicate managed agent UUID to archive.
        dry_run: When True, compute counts and roll back.
        user_id: Acting user for the audit event.

    Returns:
        Survivor, archived duplicate, and mutation counts.

    Raises:
        ManagedAgentIdentityError: When the merge is refused.
    """
    if survivor_id == duplicate_id:
        raise ManagedAgentIdentityError("survivor and duplicate must differ")

    survivor = crud_managed_agent.get_for_account(
        db, account_id=account_id, agent_id=survivor_id
    )
    duplicate = crud_managed_agent.get_for_account(
        db, account_id=account_id, agent_id=duplicate_id
    )
    if survivor is None or duplicate is None:
        raise ManagedAgentIdentityError("Managed agent not found", status_code=404)

    if survivor.session_source_type != duplicate.session_source_type:
        raise ManagedAgentIdentityError(
            "Cannot merge managed agents with different session_source_type",
            status_code=409,
        )
    if survivor.session_source_type == "custom" or duplicate.session_source_type == (
        "custom"
    ):
        raise ManagedAgentIdentityError(
            "Merging custom managed agents is out of scope",
            status_code=409,
        )
    dup_tags = dict(duplicate.tags or {})
    if dup_tags.get("merged_into"):
        raise ManagedAgentIdentityError(
            "Duplicate has already been merged",
            status_code=409,
        )
    if (
        survivor.lifecycle_state == "active"
        and duplicate.lifecycle_state == "active"
        and _agent_has_live_session(survivor)
        and _agent_has_live_session(duplicate)
    ):
        raise ManagedAgentIdentityError(
            "Both agents have live bound runtime sessions; offboard or suspend "
            "one before merging",
            status_code=409,
        )

    counts = IdentityMutationCounts()
    nested = db.begin_nested()
    try:
        moved, deleted = _rewrite_usage_principal(
            db,
            account_id=account_id,
            old_principal_id=duplicate.session_source_id,
            new_principal_id=survivor.session_source_id,
            delete_fingerprint_collisions=True,
        )
        counts.usage_moved = moved
        counts.usage_deleted = deleted
        counts.runtime_sessions_moved = _rewrite_runtime_sessions(
            db,
            account_id=account_id,
            old_principal_id=duplicate.session_source_id,
            new_principal_id=survivor.session_source_id,
        )
        moved_spend, merged_spend = _merge_budget_spend(
            db,
            account_id=account_id,
            duplicate_id=duplicate.id,
            survivor_id=survivor.id,
        )
        counts.budget_spend_moved = moved_spend
        counts.budget_spend_merged = merged_spend
        moved_policies, dropped_policies, dropped_details = _merge_budget_policies(
            db,
            account_id=account_id,
            duplicate_id=duplicate.id,
            survivor_id=survivor.id,
        )
        counts.budget_policies_moved = moved_policies
        counts.budget_policies_dropped = dropped_policies
        counts.dropped_budget_policies = dropped_details

        approval_rows = (
            db.query(ApprovalRequest)
            .filter(ApprovalRequest.managed_agent_id == duplicate.id)
            .all()
        )
        for row in approval_rows:
            row.managed_agent_id = survivor.id
            db.add(row)
        counts.approvals_moved = len(approval_rows)

        deactivated = crud_api_key.deactivate_runtime_keys_for_managed_agent(
            db,
            account_id=account_id,
            managed_agent_id=str(duplicate.id),
            commit=False,
        )
        unbound = crud_api_key.deactivate_unbound_runtime_keys_for_principal(
            db,
            account_id=account_id,
            runtime_principal_type=duplicate.session_source_type,
            runtime_principal_id=duplicate.session_source_id,
            commit=False,
        )
        counts.keys_deactivated = len(deactivated) + len(unbound)

        now = _utc_now()
        tags = dict(duplicate.tags or {})
        tags["merged_into"] = str(survivor.id)
        duplicate.tags = tags
        flag_modified(duplicate, "tags")
        duplicate.lifecycle_state = "decommissioned"
        duplicate.lifecycle_reason = f"merged into {survivor.id}"
        duplicate.lifecycle_updated_at = now
        duplicate.runtime_session_id = None
        db.add(duplicate)
        db.flush()

        if dry_run:
            nested.rollback()
            db.expire_all()
            survivor = crud_managed_agent.get_for_account(
                db, account_id=account_id, agent_id=survivor_id
            )
            duplicate = crud_managed_agent.get_for_account(
                db, account_id=account_id, agent_id=duplicate_id
            )
            assert survivor is not None and duplicate is not None
            return survivor, duplicate, counts

        nested.commit()
    except Exception:
        nested.rollback()
        raise

    from preloop.models.models.audit_log import AuditLog

    db.add(
        AuditLog(
            account_id=account_id,
            user_id=user_id,
            action="managed_agent.merge",
            resource_type="managed_agent",
            resource_id=str(survivor.id),
            status="success",
            details={
                "survivor_id": str(survivor.id),
                "duplicate_id": str(duplicate.id),
                "survivor_snapshot": {
                    "session_source_id": survivor.session_source_id,
                    "display_name": survivor.display_name,
                    "lifecycle_state": survivor.lifecycle_state,
                },
                "duplicate_snapshot": {
                    "session_source_id": duplicate.session_source_id,
                    "display_name": duplicate.display_name,
                    "lifecycle_state": "decommissioned",
                },
                "counts": {
                    "usage_moved": counts.usage_moved,
                    "usage_deleted": counts.usage_deleted,
                    "runtime_sessions_moved": counts.runtime_sessions_moved,
                    "budget_spend_moved": counts.budget_spend_moved,
                    "budget_spend_merged": counts.budget_spend_merged,
                    "budget_policies_moved": counts.budget_policies_moved,
                    "budget_policies_dropped": counts.budget_policies_dropped,
                    "approvals_moved": counts.approvals_moved,
                    "keys_deactivated": counts.keys_deactivated,
                },
                "dropped_budget_policies": counts.dropped_budget_policies,
            },
            timestamp=datetime.now(UTC),
        )
    )
    db.flush()
    db.refresh(survivor)
    db.refresh(duplicate)
    return survivor, duplicate, counts
