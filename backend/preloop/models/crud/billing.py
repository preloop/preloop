"""Account-scoped billing state, aggregate observations and serialized capacity.

Only this CRUD boundary owns billing queries/transactions. Account row locks
serialize seat claims and billing commands across API and worker processes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
import uuid

from sqlalchemy import and_, case, func, or_, text
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.plugins.account_hooks import billing_account_id
from .api_usage import exclude_replay_usage_condition
from .entitlement import (
    ENTITLED_STATUSES,
    entitlement_clause,
    grandfather_clause,
)

__all__ = ["ENTITLED_STATUSES", "CRUDBilling", "billing"]


class CRUDBilling:
    """Persistence primitives used by the optional enterprise billing service."""

    def lock_account(self, db: Session, account_id: str) -> models.Account:
        account = (
            db.query(models.Account)
            .filter(models.Account.id == account_id)
            .with_for_update()
            .populate_existing()
            .one_or_none()
        )
        if account is None:
            raise ValueError("Account not found")
        return account

    def has_billing_permission(self, db: Session, user: models.User) -> bool:
        """Honor explicit, account-scoped billing roles without an edition bypass."""
        if not user.is_active:
            return False
        roles = (
            db.query(models.Role.id)
            .join(models.UserRole, models.UserRole.role_id == models.Role.id)
            .filter(
                models.UserRole.user_id == user.id,
                or_(
                    models.Role.account_id == user.account_id,
                    models.Role.account_id.is_(None),
                ),
            )
        )
        team_roles = (
            db.query(models.Role.id)
            .join(models.TeamRole, models.TeamRole.role_id == models.Role.id)
            .join(models.Team, models.Team.id == models.TeamRole.team_id)
            .join(
                models.TeamMembership, models.TeamMembership.team_id == models.Team.id
            )
            .filter(
                models.TeamMembership.user_id == user.id,
                models.Team.account_id == user.account_id,
                or_(
                    models.Role.account_id == user.account_id,
                    models.Role.account_id.is_(None),
                ),
            )
        )
        roles = roles.union(team_roles)
        if (
            db.query(models.Role.id)
            .filter(models.Role.id.in_(roles), models.Role.name == "owner")
            .first()
            is not None
        ):
            return True
        return (
            db.query(models.RolePermission.role_id)
            .join(
                models.Permission,
                models.Permission.id == models.RolePermission.permission_id,
            )
            .filter(
                models.RolePermission.role_id.in_(roles),
                models.Permission.name == "manage_billing",
                models.Permission.is_active.is_(True),
            )
            .first()
            is not None
        )

    def commit(self, db: Session) -> None:
        db.commit()

    def rollback(self, db: Session) -> None:
        db.rollback()

    def entitled_subscription(
        self, db: Session, account_id: str
    ) -> models.Subscription | None:
        """Newest subscription row that actually entitles this account.

        Returns None for an account whose trial has ended, exactly as for an
        account that never subscribed, so every caller falls through to Free
        instead of honouring a stale ``trialing`` status. See
        :mod:`preloop.models.crud.entitlement` for the rule.

        A row pointing at a plan that has been withdrawn from sale
        (``plan.is_active == False``) only counts while somebody is paying
        for it: grandfathering is a promise to paying customers, not a way
        for an ended trial of a retired plan to keep that plan's terms and
        name. A row whose plan row is missing entirely is treated the same
        way, so it falls back to the default plan instead of resolving terms
        nobody can read.

        Account hook H7 may name another account whose subscription applies
        (for example a paying parent); its rows are read instead.
        """
        account_id = billing_account_id(db, account_id)
        return (
            db.query(models.Subscription)
            .filter(
                models.Subscription.account_id == account_id,
                entitlement_clause(models.Subscription),
                grandfather_clause(models.Subscription, models.Plan),
            )
            .order_by(models.Subscription.created_at.desc())
            .first()
        )

    def account_by_customer(
        self, db: Session, customer_id: str
    ) -> models.Account | None:
        return (
            db.query(models.Account)
            .filter(models.Account.stripe_customer_id == customer_id)
            .one_or_none()
        )

    def plan_by_product(self, db: Session, product_id: str) -> models.Plan | None:
        return (
            db.query(models.Plan)
            .filter(models.Plan.stripe_product_id == product_id)
            .one_or_none()
        )

    def counts(
        self, db: Session, account_id: str, *, lock: bool = False
    ) -> dict[str, int]:
        if lock:
            self.lock_account(db, account_id)
        now = datetime.now(timezone.utc)
        users = (
            db.query(func.count(models.User.id))
            .filter(
                models.User.account_id == account_id, models.User.is_active.is_(True)
            )
            .scalar()
        )
        invitations = (
            db.query(func.count(models.UserInvitation.id))
            .filter(
                models.UserInvitation.account_id == account_id,
                models.UserInvitation.status == models.UserInvitationStatus.PENDING,
                models.UserInvitation.expires_at > now,
            )
            .scalar()
        )
        agents = (
            db.query(func.count(models.ManagedAgent.id))
            .filter(
                models.ManagedAgent.account_id == account_id,
                models.ManagedAgent.lifecycle_state == "active",
            )
            .scalar()
        )
        return {
            "active_users": int(users or 0),
            "pending_invitations": int(invitations or 0),
            "active_agents": int(agents or 0),
        }

    def plan_fit_candidates(self, db: Session, account_id: str) -> dict[str, list[Any]]:
        """Members and agents of one account, most-keepable first.

        "Most keepable" is the order an over-capacity account is trimmed in
        when its plan shrinks (the enterprise plan-fit enforcer):

        Members
            The account owner (``account.primary_user_id``) first and always,
            then the most recently active member. Recency of a member is
            ``User.last_login``; a member who never logged in has none, so
            those sort last and are broken by ``created_at`` (newest first)
            and finally by id, which makes the order total and stable.

        Agents
            Most recently active first, where recency is
            ``ManagedAgent.last_seen_at`` (stamped by enrollment and by every
            piece of gateway traffic the agent produces), then ``created_at``
            and id for the same reason.

        Read-only: it returns rows, decides nothing and writes nothing.

        Args:
            db: Database session.
            account_id: Account whose capacity is being measured.

        Returns:
            ``{"users": [...], "agents": [...]}`` with active rows only.
        """
        account = db.query(models.Account).filter(models.Account.id == account_id).one()
        owner_first = case(
            (models.User.id == account.primary_user_id, 0),
            else_=1,
        )
        users = (
            db.query(models.User)
            .filter(
                models.User.account_id == account_id,
                models.User.is_active.is_(True),
            )
            .order_by(
                owner_first,
                models.User.last_login.desc().nullslast(),
                models.User.created_at.desc(),
                models.User.id,
            )
            .all()
        )
        agents = (
            db.query(models.ManagedAgent)
            .filter(
                models.ManagedAgent.account_id == account_id,
                models.ManagedAgent.lifecycle_state == "active",
            )
            .order_by(
                models.ManagedAgent.last_seen_at.desc(),
                models.ManagedAgent.created_at.desc(),
                models.ManagedAgent.id,
            )
            .all()
        )
        return {"users": users, "agents": agents}

    def enforce_plan_fit(
        self,
        db: Session,
        *,
        account_id: str,
        max_users: int,
        max_agents: int,
        reason: str,
        operation_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Trim one account to its plan's caps in a single transaction.

        Deactivates the members and suspends the agents that sit beyond the
        caps, keeping the most keepable of each (see
        :meth:`plan_fit_candidates`) and never the owner. A cap of ``-1`` is
        unlimited and that half of the account is left untouched.

        The whole change, including the audit row, is one transaction under
        the account lock, so a seat claim racing this cannot land between the
        measurement and the write. Nothing here decides *whether* an account
        should be trimmed: the caller (the enterprise plan-fit enforcer) owns
        that policy, which is why the caps arrive as numbers.

        Idempotency has two layers. A completed operation with the same key
        is reported back untouched, and an account already inside its caps
        writes nothing at all.

        Args:
            db: Database session; committed once on success.
            account_id: Account to trim.
            max_users: Seat ceiling, -1 for unlimited.
            max_agents: Active-agent ceiling, -1 for unlimited.
            reason: Short machine-readable cause, stored on each agent as its
                lifecycle reason and in the audit row.
            operation_key: Stable key for the audit/idempotency row.
            payload: Inputs recorded on the audit row.

        Returns:
            ``status`` (``applied``, ``within_limits`` or ``already_applied``),
            the ``users`` and ``agents`` that were deactivated (id, plus the
            label a notification needs) and ``operation_id`` when a row was
            written.
        """
        # The lock comes first so the idempotency read cannot see a twin's
        # half-finished work: two triggers on the same overage serialize here,
        # and the loser reads the winner's completed row.
        self.lock_account(db, account_id)
        existing = self.operation(db, account_id, operation_key)
        if existing is not None and existing.status == "completed":
            db.commit()
            return {**dict(existing.result), "status": "already_applied"}
        candidates = self.plan_fit_candidates(db, account_id)
        account_owner_id = (
            db.query(models.Account.primary_user_id)
            .filter(models.Account.id == account_id)
            .scalar()
        )
        surplus_users = candidates["users"][max_users:] if max_users >= 0 else []
        # The owner sorts first, so this only bites at max_users=0, which no
        # plan sets. It is here because deactivating the owner locks everyone
        # out of the account, including out of the upgrade that would undo it.
        surplus_users = [
            user for user in surplus_users if str(user.id) != str(account_owner_id)
        ]
        surplus_agents = candidates["agents"][max_agents:] if max_agents >= 0 else []
        if not surplus_users and not surplus_agents:
            # Release the lock without discarding anything the caller staged.
            db.commit()
            return {
                "status": "within_limits",
                "users": [],
                "agents": [],
                "operation_id": None,
            }
        now = datetime.now(timezone.utc)
        for user in surplus_users:
            user.is_active = False
            db.add(user)
        for agent in surplus_agents:
            agent.lifecycle_state = "suspended"
            agent.lifecycle_reason = reason
            agent.lifecycle_updated_at = now
            # Same as an operator pause: a suspended agent holds no control
            # session, and every auth path already refuses a non-active agent.
            agent.control_connection_id = None
            agent.control_last_heartbeat_at = None
            agent.control_session_mode = None
            db.add(agent)
        result = {
            "status": "applied",
            "reason": reason,
            "users": [
                {
                    "id": str(user.id),
                    "username": user.username,
                    "email": user.email,
                }
                for user in surplus_users
            ],
            "agents": [
                {"id": str(agent.id), "display_name": agent.display_name}
                for agent in surplus_agents
            ],
        }
        if surplus_users:
            from .capacity import record_capacity_change

            record_capacity_change(db, str(account_id))
        row = models.BillingOperation(
            account_id=account_id,
            operation_key=operation_key,
            kind="plan_fit_enforcement",
            status="completed",
            lease_until=None,
            payload=payload,
            result=result,
        )
        db.add(row)
        db.flush()
        result["operation_id"] = str(row.id)
        db.commit()
        return result

    def accept_invitation(
        self, db: Session, invitation_id: Any, user_data: dict[str, Any]
    ) -> models.User:
        """Replace a pending seat with its user atomically, rejecting token replay."""
        self.lock_account(db, str(user_data["account_id"]))
        invite = (
            db.query(models.UserInvitation)
            .filter(
                models.UserInvitation.id == invitation_id,
                models.UserInvitation.account_id == user_data["account_id"],
            )
            .with_for_update()
            .populate_existing()
            .one()
        )
        if (
            invite.status != models.UserInvitationStatus.PENDING
            or invite.expires_at <= datetime.now(timezone.utc)
        ):
            raise ValueError("Invitation is no longer pending")
        from .capacity import record_capacity_change

        record_capacity_change(db, str(user_data["account_id"]))
        user = models.User(**user_data)
        db.add(user)
        db.flush()
        invite.status = models.UserInvitationStatus.ACCEPTED
        invite.accepted_by = user.id
        invite.accepted_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(user)
        return user

    def reconcile_subscription(
        self, db: Session, *, account_id: str, stripe_id: str, values: dict[str, Any]
    ) -> models.Subscription:
        """Write one authoritative provider snapshot under the account lock."""
        account = self.lock_account(db, account_id)
        row = (
            db.query(models.Subscription)
            .filter(models.Subscription.stripe_subscription_id == stripe_id)
            .with_for_update()
            .one_or_none()
        )
        if row is not None and str(row.account_id) != str(account_id):
            raise ValueError("Subscription belongs to a different account")
        from .history_policy import analytics_plan_retention, preserve_history_retention

        plan_ids = {values["plan_id"]}
        if row is not None:
            plan_ids.add(row.plan_id)
        for plan_id in plan_ids:
            plan = db.query(models.Plan).filter(models.Plan.id == plan_id).one_or_none()
            if plan is not None:
                preserve_history_retention(
                    db,
                    account_id=account_id,
                    days=analytics_plan_retention(plan.features),
                )
        if row is None:
            row = models.Subscription(
                account_id=account_id, stripe_subscription_id=stripe_id, **values
            )
            db.add(row)
        else:
            for key, value in values.items():
                setattr(row, key, value)
        pending = account.billing_pending_change
        if pending:
            snapshot = values["billing_state"]
            operation = self.operation(db, account_id, pending["operation_key"])
            completed = operation is not None and operation.status == "completed"
            reached = completed and (
                (pending["plan_id"] == "free" and snapshot["status"] == "canceled")
                or (
                    snapshot["plan_id"] == pending["plan_id"]
                    and snapshot.get("interval") == pending.get("interval")
                    and datetime.fromisoformat(snapshot["current_period_start"])
                    >= datetime.fromisoformat(pending["effective_at"])
                )
            )
            removed = (
                operation is not None
                and operation.status == "completed"
                and not snapshot.get("schedule_id")
                and not snapshot.get("cancel_at_period_end")
            )
            if reached or removed:
                metadata = dict(account.meta_data or {})
                metadata.pop("billing_pending_change", None)
                account.billing_pending_change = None
                account.meta_data = metadata
        db.commit()
        db.refresh(row)
        return row

    def operation(
        self, db: Session, account_id: str, key: str
    ) -> models.BillingOperation | None:
        return (
            db.query(models.BillingOperation)
            .filter(
                models.BillingOperation.account_id == account_id,
                models.BillingOperation.operation_key == key,
            )
            .one_or_none()
        )

    def claim_operation(
        self,
        db: Session,
        *,
        account_id: str,
        key: str,
        kind: str,
        payload: dict[str, Any],
    ) -> tuple[models.BillingOperation, bool]:
        """Persist intent before a provider write; retries use the same operation id."""
        account = self.lock_account(db, account_id)
        now = datetime.now(timezone.utc)
        pending = account.billing_pending_change
        if kind == "plan_change" and pending and pending.get("operation_key") != key:
            db.rollback()
            raise ValueError("Another plan change is pending")
        row = self.operation(db, account_id, key)
        if row and row.status == "completed":
            db.commit()
            return row, False
        if row and row.lease_until and row.lease_until > now:
            db.commit()
            return row, False
        if row is None:
            row = models.BillingOperation(
                account_id=account_id,
                operation_key=key,
                kind=kind,
                payload=payload,
                result={},
            )
            db.add(row)
        elif row.payload != payload:
            raise ValueError("Operation key was reused with different parameters")
        if kind == "plan_change":
            counts = payload["counts"]
            seat_limit = payload["target_max_users"]
            agent_limit = payload["target_features"].get("max_agents", -1)
            if payload["target_plan_id"] == "free":
                seat_limit = max(
                    seat_limit, counts["active_users"] + counts["pending_invitations"]
                )
                agent_limit = max(agent_limit, counts["active_agents"])
            account.billing_pending_change = {
                "operation_key": key,
                "plan_id": payload["target_plan_id"],
                "effective_at": payload["effective_at"],
                "interval": payload.get("interval"),
                "max_users": seat_limit,
                "max_agents": agent_limit,
            }
            account.meta_data = {
                **(account.meta_data or {}),
                "billing_pending_change": account.billing_pending_change,
            }
        row.status = "applying"
        row.lease_until = now + timedelta(minutes=2)
        db.commit()
        db.refresh(row)
        return row, True

    def finish_operation(
        self, db: Session, operation_id: Any, result: dict[str, Any]
    ) -> None:
        account_id = (
            db.query(models.BillingOperation.account_id)
            .filter(models.BillingOperation.id == operation_id)
            .scalar()
        )
        account = self.lock_account(db, str(account_id))
        row = (
            db.query(models.BillingOperation)
            .filter(models.BillingOperation.id == operation_id)
            .with_for_update()
            .one()
        )
        if result.get("status") == "applied":
            metadata = dict(account.meta_data or {})
            metadata.pop("billing_pending_change", None)
            account.billing_pending_change = None
            account.meta_data = metadata
        row.status = "completed"
        row.result = result
        row.lease_until = None
        db.commit()

    def set_seat_sync_pending(
        self,
        db: Session,
        account_id: str,
        pending: bool,
        *,
        expected_generation: str | None = None,
        new_generation: bool = False,
    ) -> str | None:
        """Queue a mutation generation or CAS-clear only the processed generation."""
        account = self.lock_account(db, account_id)
        generation = account.billing_seat_sync_generation
        if pending:
            if (
                new_generation
                or not account.billing_seat_sync_pending
                or not generation
            ):
                generation = str(uuid.uuid4())
                account.billing_seat_sync_generation = generation
            account.billing_seat_sync_pending = True
        elif expected_generation is None or str(generation) == expected_generation:
            account.billing_seat_sync_pending = False
        account.meta_data = {
            **(account.meta_data or {}),
            "billing_seat_sync_pending": account.billing_seat_sync_pending,
            "billing_seat_sync_generation": str(generation) if generation else None,
        }
        return str(generation) if generation else None

    def bind_checkout_customer(
        self,
        db: Session,
        *,
        account_id: str,
        customer_id: str,
        session_id: str,
        subscription_id: str,
        allow_association: bool,
    ) -> bool:
        """Bind a trusted checkout or durably acknowledge an identity conflict.

        Email matching is not account authorization. Only a server-generated
        authenticated account reference may fill a NULL customer mapping.
        Conflicts never change ownership, entitlements or provider objects.
        Repeated deliveries retain one auditable reconciliation result.
        The EE checkout-completion webhook calls this with ``allow_association``
        only for a server-generated authenticated account reference.
        """
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:customer, 0))"),
            {"customer": customer_id},
        )
        account = self.lock_account(db, account_id)
        key = f"checkout:{session_id}"
        operation = self.operation(db, account_id, key)
        if operation is not None:
            result = operation.result or {}
            associated = (
                result.get("status") == "associated"
                and account.stripe_customer_id == customer_id
                and operation.payload.get("customer_id") == customer_id
                and operation.payload.get("subscription_id") == subscription_id
            )
            if result.get("status") == "associated" and not associated:
                operation.result = {
                    "status": "reconciliation_required",
                    "reason": "customer_mapping_changed",
                }
            db.commit()
            return associated
        owner = self.account_by_customer(db, customer_id)
        reason = None
        if owner is not None and str(owner.id) != str(account.id):
            reason = "customer_owned_by_another_account"
        elif account.stripe_customer_id and account.stripe_customer_id != customer_id:
            reason = "customer_mismatch"
        elif not account.stripe_customer_id and not allow_association:
            reason = "account_reference_required"
        if reason is None:
            account.stripe_customer_id = customer_id
        db.add(
            models.BillingOperation(
                account_id=account.id,
                operation_key=key,
                kind="checkout_reconciliation",
                status="completed",
                lease_until=None,
                payload={
                    "session_id": session_id,
                    "subscription_id": subscription_id,
                    "customer_id": customer_id,
                },
                result={
                    "status": "reconciliation_required" if reason else "associated",
                    "reason": reason,
                    "existing_customer_id": account.stripe_customer_id,
                },
            )
        )
        db.commit()
        return reason is None

    def checkout_reconciliation_hold(
        self, db: Session, *, customer_id: str, subscription_id: str
    ) -> models.BillingOperation | None:
        """Find only an already-recorded provider identity conflict.

        Provider webhook processing may acknowledge this exact pair without
        retrying a permanent conflict. Unrelated unlinked subscriptions must
        still retry until checkout establishes their account.
        The EE subscription-webhook reconciler consumes this result to acknowledge
        an identity hold without applying subscription entitlements.
        """
        return (
            db.query(models.BillingOperation)
            .filter(
                models.BillingOperation.kind == "checkout_reconciliation",
                models.BillingOperation.payload["customer_id"].as_string()
                == customer_id,
                models.BillingOperation.payload["subscription_id"].as_string()
                == subscription_id,
                models.BillingOperation.result["status"].as_string()
                == "reconciliation_required",
            )
            .first()
        )

    def create_checkout_account(
        self,
        db: Session,
        *,
        customer_id: str,
        organization_name: str,
        user_data: dict[str, Any],
    ) -> models.Account:
        """Customer-keyed idempotent owner/account creation in one transaction."""
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:customer, 0))"),
            {"customer": customer_id},
        )
        existing = self.account_by_customer(db, customer_id)
        if existing is not None:
            return existing
        account = models.Account(
            organization_name=organization_name,
            stripe_customer_id=customer_id,
            is_active=True,
        )
        db.add(account)
        db.flush()
        user = models.User(account_id=account.id, **user_data)
        db.add(user)
        db.flush()
        account.primary_user_id = user.id
        owner = (
            db.query(models.Role)
            .filter(models.Role.name == "owner", models.Role.account_id.is_(None))
            .one_or_none()
        )
        if owner is not None:
            db.add(models.UserRole(user_id=user.id, role_id=owner.id))
        db.commit()
        db.refresh(account)
        return account

    def deactivate_account_users(self, db: Session, account_id: str) -> None:
        account = self.lock_account(db, account_id)
        account.is_active = False
        db.query(models.User).filter(models.User.account_id == account_id).update(
            {"is_active": False}
        )
        self.set_seat_sync_pending(db, account_id, True, new_generation=True)
        db.commit()

    def mark_seat_sync_attempt(self, db: Session, account_id: str) -> None:
        account = self.lock_account(db, account_id)
        account.billing_seat_sync_attempted_at = datetime.now(timezone.utc)
        account.meta_data = {
            **(account.meta_data or {}),
            "billing_seat_sync_attempted_at": account.billing_seat_sync_attempted_at.isoformat(),
        }

    def pending_seat_sync_accounts(self, db: Session, *, limit: int = 25) -> list[str]:
        return [
            str(row[0])
            for row in db.query(models.Account.id)
            .filter(models.Account.billing_seat_sync_pending.is_(True))
            .order_by(
                models.Account.billing_seat_sync_attempted_at.asc().nullsfirst(),
                models.Account.id,
            )
            .limit(limit)
            .all()
        ]

    def stripe_subscription_page(
        self, db: Session, *, after_id: Any = None, limit: int = 100
    ) -> list[dict[str, str]]:
        """Read one bounded page of subscriptions linked to a Stripe id.

        Keyset paging over the primary key gives a stable order across pages
        even while rows are reconciled underneath the walk, which a periodic
        full-fleet pass needs. Returns identifiers only; no customer data.

        Args:
            db: Database session.
            after_id: Exclusive cursor, the ``id`` of the last row seen.
            limit: Page size, 1-500.

        Returns:
            ``{"id", "account_id", "stripe_subscription_id"}`` per row, in
            ascending ``id`` order.

        Raises:
            ValueError: When ``limit`` is outside 1-500.
        """
        if not 1 <= limit <= 500:
            raise ValueError("Subscription page size must be between 1 and 500")
        query = db.query(
            models.Subscription.id,
            models.Subscription.account_id,
            models.Subscription.stripe_subscription_id,
        ).filter(models.Subscription.stripe_subscription_id.isnot(None))
        if after_id is not None:
            query = query.filter(models.Subscription.id > after_id)
        query = query.order_by(models.Subscription.id).limit(limit)
        return [
            {
                "id": str(row[0]),
                "account_id": str(row[1]),
                "stripe_subscription_id": str(row[2]),
            }
            for row in query.all()
        ]

    def monthly_observations(
        self,
        db: Session,
        *,
        account_id: str,
        start: datetime,
        end: datetime,
        hosted_model_ids: list[str],
        byok_model_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """One bounded aggregate; unknown funding/currency never becomes zero-fit."""
        usage = models.ApiUsage
        hosted = usage.ai_model_id.in_(hosted_model_ids)
        byok = usage.ai_model_id.in_(byok_model_ids)
        known_ids = hosted_model_ids + byok_model_ids
        unknown = or_(usage.ai_model_id.is_(None), usage.ai_model_id.not_in(known_ids))
        usd = func.lower(func.coalesce(usage.currency, "USD")) == "usd"
        priced = and_(
            hosted, usd, usage.estimated_cost.is_not(None), usage.estimated_cost >= 0
        )
        month = func.date_trunc("month", func.timezone("UTC", usage.timestamp))
        rows = (
            db.query(
                month.label("month"),
                func.count(usage.id).label("requests"),
                func.coalesce(
                    func.sum(
                        case(
                            (and_(byok, usage.total_tokens >= 0), usage.total_tokens),
                            else_=0,
                        )
                    ),
                    0,
                ).label("byok_tokens"),
                func.coalesce(
                    func.sum(case((priced, usage.estimated_cost), else_=0)), 0
                ).label("hosted_cost"),
                func.count(
                    case(
                        (
                            and_(
                                hosted,
                                or_(
                                    usage.estimated_cost.is_(None),
                                    usage.estimated_cost < 0,
                                ),
                            ),
                            1,
                        )
                    )
                ).label("unpriced_hosted"),
                func.count(case((unknown, 1))).label("unknown_models"),
                func.count(case((and_(hosted, ~usd), 1))).label("non_usd_hosted"),
                func.count(
                    case(
                        (
                            and_(
                                byok,
                                or_(
                                    usage.total_tokens.is_(None), usage.total_tokens < 0
                                ),
                            ),
                            1,
                        )
                    )
                ).label("unknown_byok_tokens"),
            )
            .filter(
                usage.account_id == account_id,
                usage.timestamp >= start,
                usage.timestamp < end,
                usage.action_type == "model_gateway",
                exclude_replay_usage_condition(),
            )
            .group_by(month)
            .all()
        )
        return {
            row.month.strftime("%Y-%m"): {
                "request_count": int(row.requests),
                "observed_byok_tokens": int(row.byok_tokens),
                "observed_hosted_cost_usd": float(row.hosted_cost),
                "unpriced_hosted_requests": int(row.unpriced_hosted),
                "unknown_model_requests": int(row.unknown_models),
                "non_usd_hosted_requests": int(row.non_usd_hosted),
                "unknown_byok_token_requests": int(row.unknown_byok_tokens),
            }
            for row in rows
        }


billing = CRUDBilling()
