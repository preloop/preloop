"""CRUD operations for ApprovalWorkflow model."""

from typing import Any, Dict, List, Optional, Set, Union
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy.future import select

from .. import models
from ..schemas.tool_configuration import ApprovalWorkflowCreate, ApprovalWorkflowUpdate
from .base import CRUDBase


class CRUDApprovalWorkflow(CRUDBase[models.ApprovalWorkflow]):
    """CRUD operations for ApprovalWorkflow model."""

    def __init__(self):
        """Initialize with the ApprovalWorkflow model."""
        super().__init__(model=models.ApprovalWorkflow)

    def get(
        self, db: Session, id: UUID, account_id: str
    ) -> Optional[models.ApprovalWorkflow]:
        """Retrieve an approval workflow by its ID.

        Args:
            db: The database session.
            id: The ID of the approval workflow to retrieve.
            account_id: The ID of the account associated with the workflow.

        Returns:
            The approval workflow object if found, otherwise None.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.id == id,
                self.model.account_id == account_id,
            )
            .first()
        )

    def get_by_name(
        self, db: Session, account_id: str, name: str
    ) -> Optional[models.ApprovalWorkflow]:
        """Retrieve an approval workflow by name and account.

        Args:
            db: The database session.
            account_id: The ID of the account.
            name: The name of the approval workflow.

        Returns:
            The approval workflow object if found, otherwise None.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.name == name,
            )
            .first()
        )

    def get_multi_by_account(
        self,
        db: Session,
        account_id: str,
        skip: int = 0,
        limit: int = 100,
    ) -> List[models.ApprovalWorkflow]:
        """Retrieve approval workflows for a specific account.

        Args:
            db: The database session.
            account_id: The ID of the account.
            skip: Number of records to skip.
            limit: Maximum number of records to return.

        Returns:
            List of approval workflow objects.
        """
        return (
            db.query(self.model)
            .filter(self.model.account_id == account_id)
            .offset(skip)
            .limit(limit)
            .all()
        )

    def get_names_by_account(self, db: Session, account_id: str) -> Set[str]:
        """Return the names of every approval workflow in an account.

        Unpaginated on purpose: reference validation needs every name, and
        ``get_multi_by_account`` caps results at its page size.
        """
        rows = (
            db.query(self.model.name).filter(self.model.account_id == account_id).all()
        )
        return {row[0] for row in rows}

    def get_default(
        self, db: Session, account_id: str
    ) -> Optional[models.ApprovalWorkflow]:
        """Retrieve the default approval workflow for an account.

        Args:
            db: The database session.
            account_id: The ID of the account.

        Returns:
            The default approval workflow if found, otherwise None.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.is_default,
            )
            .first()
        )

    def find_legacy_default_workflows(self, db: Session) -> List[UUID]:
        """Return ``account_id``s whose default workflow needs repair.

        A default workflow is considered "legacy" / broken when it either:

        * was created with ``approval_type="manual"`` (the older synonym
          for the in-UI human-approval flow that the dialog dropdown can
          no longer render — the type field appears blank), or
        * has no ``approver_user_ids`` *and* no ``approver_team_ids`` —
          which makes any approval request routed through it impossible
          to act on.

        The startup repair pass uses this to find accounts that still
        carry pre-fix data and heal them on boot.
        """
        rows = (
            db.query(self.model.account_id)
            .filter(
                self.model.is_default.is_(True),
                (
                    (self.model.approval_type == "manual")
                    | (
                        (
                            (self.model.approver_user_ids.is_(None))
                            | (self.model.approver_user_ids == [])
                        )
                        & (
                            (self.model.approver_team_ids.is_(None))
                            | (self.model.approver_team_ids == [])
                        )
                    )
                ),
            )
            .all()
        )
        return [row[0] for row in rows]

    def find_accounts_missing_workflows(self, db: Session) -> List[UUID]:
        """Return active ``account_id``s that have no approval workflows at all.

        Every account is expected to get a default workflow seeded at signup,
        but that seeding runs as a background task and can fail (deploy
        restart, transient DB error). The startup repair pass uses this to
        find such accounts and seed the default workflow retroactively so
        approval requests always have a workflow (and approver) to route to.
        """
        rows = (
            db.query(models.Account.id)
            .outerjoin(self.model, self.model.account_id == models.Account.id)
            .filter(
                models.Account.is_active.is_(True),
                self.model.id.is_(None),
            )
            .all()
        )
        return [row[0] for row in rows]

    def find_startup_repair_targets(self, db: Session) -> tuple[List[UUID], List[UUID]]:
        """Return ``(legacy_default_ids, missing_workflow_ids)`` in one DB pass.

        Combines :meth:`find_legacy_default_workflows` and
        :meth:`find_accounts_missing_workflows` via ``UNION ALL`` so the
        startup repair scan pays one round-trip instead of two.
        """
        from sqlalchemy import literal

        broken_q = db.query(
            literal("broken").label("kind"),
            self.model.account_id.label("account_id"),
        ).filter(
            self.model.is_default.is_(True),
            (
                (self.model.approval_type == "manual")
                | (
                    (
                        (self.model.approver_user_ids.is_(None))
                        | (self.model.approver_user_ids == [])
                    )
                    & (
                        (self.model.approver_team_ids.is_(None))
                        | (self.model.approver_team_ids == [])
                    )
                )
            ),
        )
        missing_q = (
            db.query(
                literal("missing").label("kind"),
                models.Account.id.label("account_id"),
            )
            .outerjoin(self.model, self.model.account_id == models.Account.id)
            .filter(
                models.Account.is_active.is_(True),
                self.model.id.is_(None),
            )
        )
        broken: List[UUID] = []
        missing: List[UUID] = []
        for kind, account_id in broken_q.union_all(missing_q).all():
            if kind == "broken":
                broken.append(account_id)
            else:
                missing.append(account_id)
        return broken, missing

    def resolve_import_recipients(
        self,
        db: Session,
        *,
        account_id: str,
        data: Dict[str, Any],
        existing: Optional[models.ApprovalWorkflow] = None,
        actor_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resolve YAML names within the account before any import mutation."""
        resolved: Dict[str, Any] = {}
        for source, target, model, name_field in (
            ("approver_users", "approver_user_ids", models.User, models.User.username),
            ("approver_teams", "approver_team_ids", models.Team, models.Team.name),
            (
                "escalation_users",
                "escalation_user_ids",
                models.User,
                models.User.username,
            ),
            ("escalation_teams", "escalation_team_ids", models.Team, models.Team.name),
        ):
            names = data.get(source)
            if names is None:
                resolved[target] = list(getattr(existing, target, None) or [])
                continue
            rows = (
                db.query(model.id, name_field)
                .filter(model.account_id == account_id, name_field.in_(names))
                .all()
            )
            by_name = {name: str(identifier) for identifier, name in rows}
            missing = set(names) - by_name.keys()
            if missing:
                raise ValueError(
                    f"Unknown account {source}: {', '.join(sorted(missing))}"
                )
            resolved[target] = list(dict.fromkeys(by_name[name] for name in names))
        if data.get("approval_type", "standard") == "standard" and not (
            resolved["approver_user_ids"] or resolved["approver_team_ids"]
        ):
            if actor_id is None:
                account = (
                    db.query(models.Account)
                    .filter(models.Account.id == account_id)
                    .one()
                )
                actor_id = (
                    str(account.primary_user_id) if account.primary_user_id else None
                )
            if actor_id is None:
                raise ValueError(
                    "A human approval workflow requires an account approver"
                )
            resolved["approver_user_ids"] = [str(actor_id)]
        self.validate_configuration_references(db, account_id=account_id, data=resolved)
        return resolved

    def export_recipient_names(
        self, db: Session, *, workflow: models.ApprovalWorkflow
    ) -> Dict[str, Any]:
        """Export portable account-scoped recipient names without dropping routing."""
        result: Dict[str, Any] = {}
        for source, target, model, name_field in (
            ("approver_user_ids", "approver_users", models.User, models.User.username),
            ("approver_team_ids", "approver_teams", models.Team, models.Team.name),
            (
                "escalation_user_ids",
                "escalation_users",
                models.User,
                models.User.username,
            ),
            ("escalation_team_ids", "escalation_teams", models.Team, models.Team.name),
        ):
            ids = getattr(workflow, source) or []
            rows = (
                db.query(model.id, name_field)
                .filter(model.account_id == workflow.account_id, model.id.in_(ids))
                .all()
                if ids
                else []
            )
            by_id = {str(identifier): name for identifier, name in rows}
            result[target] = [
                by_id[str(identifier)] for identifier in ids if str(identifier) in by_id
            ]
        return result

    def stage_import(
        self,
        db: Session,
        *,
        account_id: str,
        workflow_id: UUID,
        data: Dict[str, Any],
    ) -> models.ApprovalWorkflow:
        """Stage an imported workflow in the caller's transaction."""
        workflow = self.get(db, id=workflow_id, account_id=account_id)
        if data.get("is_default"):
            self._unmark_default(db, account_id)
        if workflow is None:
            workflow = self.model(
                id=workflow_id, account_id=account_id, approval_type="manual"
            )
            db.add(workflow)
        for field, value in data.items():
            setattr(workflow, field, value)
        db.flush()
        return workflow

    def validate_configuration_references(
        self, db: Session, *, account_id: str, data: Dict[str, Any]
    ) -> None:
        """Only account members and workflows may receive approval routing."""
        for field, model in (
            ("approver_user_ids", models.User),
            ("escalation_user_ids", models.User),
            ("approver_team_ids", models.Team),
            ("escalation_team_ids", models.Team),
        ):
            ids = set(data.get(field) or [])
            if not ids:
                continue
            found = {
                str(row[0])
                for row in db.query(model.id)
                .filter(model.id.in_(ids), model.account_id == account_id)
                .all()
            }
            if found != set(map(str, ids)):
                raise ValueError("Approval recipients must belong to this account")
        escalation = data.get("escalation_workflow_id")
        if escalation and self.get(db, id=escalation, account_id=account_id) is None:
            raise ValueError("Escalation workflow must belong to this account")

    def create(
        self,
        db: Session,
        *,
        obj_in: Union[ApprovalWorkflowCreate, Dict[str, Any]],
        account_id: str,
    ) -> models.ApprovalWorkflow:
        """Create a new approval workflow.

        If this is the first workflow for the account, it will be marked as default.
        If is_default is explicitly set to True, any existing default will be unmarked.

        Args:
            db: The database session.
            obj_in: The approval workflow data.
            account_id: The ID of the account.

        Returns:
            The created approval workflow.
        """
        # Convert to dict if it's a Pydantic model
        if isinstance(obj_in, ApprovalWorkflowCreate):
            obj_data = obj_in.model_dump(exclude_unset=True)
        else:
            obj_data = obj_in.copy()

        # Ensure account_id is set
        obj_data["account_id"] = account_id

        # Check if this is the first workflow for the account
        existing_count = (
            db.query(self.model).filter(self.model.account_id == account_id).count()
        )

        # If no policies exist, make this one default
        if existing_count == 0:
            obj_data["is_default"] = True
        elif obj_data.get("is_default", False):
            # If explicitly setting as default, unmark existing default
            self._unmark_default(db, account_id)

        # Create the workflow
        db_obj = self.model(**obj_data)
        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    def update(
        self,
        db: Session,
        *,
        db_obj: models.ApprovalWorkflow,
        obj_in: Union[ApprovalWorkflowUpdate, Dict[str, Any]],
    ) -> models.ApprovalWorkflow:
        """Update an approval workflow.

        If is_default is set to True, any existing default will be unmarked.

        Args:
            db: The database session.
            db_obj: The existing approval workflow object.
            obj_in: The update data.

        Returns:
            The updated approval workflow.
        """
        # Convert to dict if it's a Pydantic model
        if isinstance(obj_in, ApprovalWorkflowUpdate):
            update_data = obj_in.model_dump(exclude_unset=True)
        else:
            update_data = obj_in.copy()

        # If setting as default, unmark existing default
        if update_data.get("is_default", False) and not db_obj.is_default:
            self._unmark_default(db, db_obj.account_id)

        # Update the object
        for field, value in update_data.items():
            setattr(db_obj, field, value)

        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    def _unmark_default(self, db: Session, account_id: str) -> None:
        """Unmark any existing default workflow for the account.

        Args:
            db: The database session.
            account_id: The ID of the account.
        """
        existing_default = self.get_default(db, account_id)
        if existing_default:
            existing_default.is_default = False
            db.add(existing_default)
            db.flush()  # Flush but don't commit yet

    def remove(
        self, db: Session, *, id: UUID, account_id: str
    ) -> Optional[models.ApprovalWorkflow]:
        """Remove an approval workflow by its ID.

        If removing the default workflow and other policies exist, the most recently
        created remaining workflow will be marked as default.

        Args:
            db: The database session.
            id: The ID of the approval workflow to remove.
            account_id: The ID of the account.

        Returns:
            The removed approval workflow object if found and deleted, otherwise None.
        """
        db_workflow = (
            db.query(self.model)
            .filter(
                self.model.id == id,
                self.model.account_id == account_id,
            )
            .order_by(self.model.created_at.desc())
            .first()
        )
        if db_workflow:
            was_default = db_workflow.is_default
            db.delete(db_workflow)
            db.flush()

            # If we deleted the default workflow, make another one default
            if was_default:
                replacement = (
                    db.query(self.model)
                    .filter(self.model.account_id == account_id)
                    .order_by(self.model.created_at.desc())
                    .first()
                )
                if replacement:
                    replacement.is_default = True
                    db.add(replacement)

            db.commit()
        return db_workflow


# Async helper functions
async def get_approval_workflow_async(
    db: Session, workflow_id: UUID
) -> Optional[models.ApprovalWorkflow]:
    """Async: Retrieve an approval workflow by its ID.

    Args:
        db: The async database session.
        workflow_id: The ID of the approval workflow.

    Returns:
        The approval workflow object if found, otherwise None.
    """
    result = await db.execute(
        select(models.ApprovalWorkflow).where(models.ApprovalWorkflow.id == workflow_id)
    )
    return result.scalar_one_or_none()


async def get_default_approval_workflow_async(
    db: Session, account_id: UUID
) -> Optional[models.ApprovalWorkflow]:
    """Async: Retrieve the default approval workflow for an account.

    Args:
        db: The async database session.
        account_id: The ID of the account.

    Returns:
        The default approval workflow if one is configured, otherwise None.
    """
    result = await db.execute(
        select(models.ApprovalWorkflow).where(
            models.ApprovalWorkflow.account_id == account_id,
            models.ApprovalWorkflow.is_default.is_(True),
        )
    )
    return result.scalars().first()
