"""Fresh CI execution ownership and immutable accepted-review queries."""

from datetime import datetime, timezone
from typing import Any, List
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.orm import Query, Session

from preloop.models import models
from preloop.models.crud.ci_principal import CiAuthorizationContext, crud_ci_principal
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.schemas.ci_execution import CiReviewBinding, ci_review_event
from preloop.schemas.ci_principal import CiAction

# Only existing controller-generated records may accompany canonical provenance.
# This is not an external trigger schema: the API accepts PR number/head only.
_TRUSTED_EVENT_EXTRAS = frozenset({"_model_routing", "_subject", "test_mode"})


def _valid_event(binding: CiReviewBinding, event: Any) -> bool:
    expected = ci_review_event(binding)
    return (
        isinstance(event, dict)
        and not (set(event) - set(expected) - _TRUSTED_EVENT_EXTRAS)
        and all(event.get(name) == value for name, value in expected.items())
        and event.get("test_mode", False) is False
    )


def _identity(context: CiAuthorizationContext) -> dict[str, Any]:
    """The stable account/resource ceiling; a rotated key is not ownership."""
    return {
        "version": 1,
        "account_id": str(context.account_id),
        "principal_id": str(context.principal_id),
        "project_id": str(context.project_id),
        "flow_id": str(context.flow_id),
        "tracker_id": str(context.tracker_id),
        "repository_identifier": context.repository_identifier,
        "repository_slug": context.repository_slug,
        "tracker_type": context.tracker_type,
        "tracker_url": context.tracker_url,
    }


def context_from_binding(binding: CiReviewBinding) -> CiAuthorizationContext:
    """Only persisted, schema-validated snapshots reach dispatch attribution."""
    return CiAuthorizationContext(
        account_id=binding.account_id,
        principal_id=binding.principal_id,
        key_id=binding.key_id,
        project_id=binding.project_id,
        flow_id=binding.flow_id,
        tracker_id=binding.tracker_id,
        repository_identifier=binding.repository_identifier,
        repository_slug=binding.repository_slug,
        tracker_type=binding.tracker_type,
        tracker_url=binding.tracker_url,
        actions=frozenset({CiAction.TRIGGER}),
    )


class CRUDCiExecution:
    """Never infer CI ownership from a flow, account, human or API key."""

    def create(
        self,
        db: Session,
        *,
        context: CiAuthorizationContext,
        binding: CiReviewBinding,
        event: dict[str, Any],
    ) -> models.FlowExecution:
        """Persist trusted acceptance and attribution in the initial insert."""
        from preloop.models import crud

        fresh = crud_ci_principal.authorize(
            db, context=context, action=CiAction.TRIGGER
        )
        binding = CiReviewBinding.model_validate(binding.model_dump())
        stored = binding.model_dump(mode="json")
        if (
            any(stored[name] != value for name, value in _identity(fresh).items())
            or binding.key_id != fresh.key_id
        ):
            raise PermissionError("Restricted CI execution binding denied")
        canonical = ci_review_event(binding)
        canonical.update(event)
        if not _valid_event(binding, canonical):
            raise PermissionError("Restricted CI execution provenance denied")
        execution = crud.crud_flow_execution.create(
            db,
            obj_in=FlowExecutionCreate.model_validate(
                {
                    "flow_id": fresh.flow_id,
                    "status": "PENDING",
                    "ci_principal_id": fresh.principal_id,
                    "initiating_ci_key_id": fresh.key_id,
                    "ci_review_binding": stored,
                    "trigger_event_details": canonical,
                }
            ),
        )
        db.commit()
        db.refresh(execution)
        return execution

    def _owned_query(
        self, db: Session, *, context: CiAuthorizationContext, action: CiAction
    ) -> Query[models.FlowExecution]:
        if action not in {
            CiAction.READ_EXECUTION,
            CiAction.READ_RESULT,
            CiAction.STOP_EXECUTION,
        }:
            raise PermissionError("Restricted CI execution operation denied")
        fresh = crud_ci_principal.authorize(db, context=context, action=action)
        stored = models.FlowExecution.ci_review_binding
        return (
            db.query(models.FlowExecution)
            .populate_existing()
            .join(models.Flow, models.Flow.id == models.FlowExecution.flow_id)
            .filter(
                models.Flow.account_id == fresh.account_id,
                models.FlowExecution.flow_id == fresh.flow_id,
                models.FlowExecution.ci_principal_id == fresh.principal_id,
                models.FlowExecution.ci_review_binding.contains(_identity(fresh)),
                stored.op("-")(array(list(CiReviewBinding.model_fields))) == {},
                stored["version"].astext == "1",
                *(
                    func.jsonb_typeof(stored[name])
                    == ("null" if value is None else "string")
                    for name, value in _identity(fresh).items()
                    if name != "version"
                ),
                func.jsonb_typeof(stored["pr_number"]) == "number",
                stored["pr_number"].astext.op("~")(r"^[1-9][0-9]*$"),
                func.jsonb_typeof(stored["head_sha"]) == "string",
                stored["head_sha"].astext.op("~")(r"^[0-9a-f]{40}([0-9a-f]{24})?$"),
                func.jsonb_typeof(stored["provider_pr_id"]) == "string",
                func.length(stored["provider_pr_id"].astext) > 0,
                func.jsonb_typeof(stored["key_id"]) == "string",
                stored["key_id"].astext.op("~")(
                    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
                ),
                func.jsonb_typeof(stored["base_branch"]) == "string",
                stored["base_branch"].astext.op("~")(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"),
                stored["base_branch"].astext.op("!~")(
                    r"(\.\.|//|/\.|\.lock(/|$)|[./]$)"
                ),
                func.length(stored["base_branch"].astext) <= 255,
            )
        )

    def get(
        self,
        db: Session,
        *,
        context: CiAuthorizationContext,
        execution_id: UUID,
        action: CiAction,
    ) -> models.FlowExecution | None:
        """Require the current grant and stable principal before object reads."""
        execution = (
            self._owned_query(db, context=context, action=action)
            .filter(models.FlowExecution.id == execution_id)
            .first()
        )
        if execution is not None:
            try:
                CiReviewBinding.model_validate(execution.ci_review_binding)
            except ValidationError:
                return None
        return execution

    def _list_query(
        self,
        db: Session,
        *,
        context: CiAuthorizationContext,
        statuses: List[str] | None = None,
        started_after: datetime | None = None,
    ) -> Query[models.FlowExecution]:
        query = self._owned_query(db, context=context, action=CiAction.READ_EXECUTION)
        if statuses:
            query = query.filter(models.FlowExecution.status.in_(statuses))
        if started_after is not None:
            query = query.filter(models.FlowExecution.start_time >= started_after)
        return query

    def list(
        self,
        db: Session,
        *,
        context: CiAuthorizationContext,
        skip: int = 0,
        limit: int = 25,
        statuses: List[str] | None = None,
        started_after: datetime | None = None,
    ) -> list[models.FlowExecution]:
        """Apply account, binding and ownership before pagination."""
        return (
            self._list_query(
                db, context=context, statuses=statuses, started_after=started_after
            )
            .order_by(
                models.FlowExecution.start_time.desc(), models.FlowExecution.id.desc()
            )
            .offset(max(skip, 0))
            .limit(max(1, min(limit, 100)))
            .all()
        )

    def count(
        self,
        db: Session,
        *,
        context: CiAuthorizationContext,
        statuses: List[str] | None = None,
        started_after: datetime | None = None,
    ) -> int:
        """Count only the same owned set returned by the list."""
        return self._list_query(
            db, context=context, statuses=statuses, started_after=started_after
        ).count()

    def authorize_dispatch(
        self, db: Session, *, execution: models.FlowExecution
    ) -> CiReviewBinding | None:
        """Recheck stable authority before launch; existing human work is unchanged."""
        db.refresh(execution)
        if (
            execution.ci_principal_id is None
            and execution.ci_review_binding is None
            and execution.initiating_ci_key_id is None
        ):
            return None
        try:
            binding = CiReviewBinding.model_validate(execution.ci_review_binding)
        except ValidationError:
            raise PermissionError(
                "Restricted CI dispatch binding unavailable"
            ) from None
        if (
            execution.ci_principal_id != binding.principal_id
            or execution.flow_id != binding.flow_id
            or execution.initiating_ci_key_id not in (None, binding.key_id)
            or execution.parent_execution_id is not None
            or execution.root_execution_id is not None
            or execution.batch_id is not None
            or execution.retry_of_execution_id is not None
            or execution.delegation_depth != 0
        ):
            raise PermissionError("Restricted CI dispatch binding denied")
        if not _valid_event(binding, execution.trigger_event_details):
            raise PermissionError("Restricted CI dispatch provenance denied")
        crud_ci_principal.authorize_principal(
            db, context=context_from_binding(binding), action=CiAction.TRIGGER
        )
        return binding

    def reject_dispatch(self, db: Session, *, execution: models.FlowExecution) -> None:
        """Persist a safe failure when pending work has lost launch authority."""
        db.query(models.FlowExecution).filter(
            models.FlowExecution.id == execution.id,
            models.FlowExecution.status.in_(["PENDING", "STARTING", "INITIALIZING"]),
        ).update(
            {
                "status": "FAILED",
                "failure_category": "verification_blocked",
                "error_message": "Restricted CI execution admission denied",
                "end_time": datetime.now(timezone.utc),
            },
            synchronize_session=False,
        )
        db.commit()
        db.refresh(execution)

    def release_read(self, db: Session) -> None:
        """End materialized reads before asynchronous trusted provider I/O."""
        db.commit()


crud_ci_execution = CRUDCiExecution()
