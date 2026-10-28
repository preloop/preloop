"""Fresh human consent and principal-bound reads of a completed result."""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.models.crud.ci_execution import _identity
from preloop.models.crud.ci_principal import CiAuthorizationContext
from preloop.schemas.ci_execution import CiReviewBinding
from preloop.schemas.ci_principal import CiAction, CiGrant
from preloop.schemas.ci_result_export import CiResultExportCreate
from preloop.services.ci_execution import public_ci_result
from preloop.services.execution_tool_evidence import digest


def _object(value: Any) -> dict[str, Any]:
    """Reject malformed persisted provenance without trusting caller assertions."""
    if not isinstance(value, dict):
        raise ValueError("Source execution lacks recorded review provenance")
    return value


def _owner(db: Session, actor: models.User, grant: CiGrant, operation: str) -> None:
    """Export consent is explicitly human and requires current owner authority."""
    crud.crud_ci_principal._authorize(db, actor=actor, operation=operation, grant=grant)
    account = crud.crud_account.get(db, id=actor.account_id)
    if account is None or account.primary_user_id != actor.id:
        raise PermissionError("Result export requires the current account owner")


def prepare_consent(
    db: Session,
    *,
    actor: models.User,
    execution_id: UUID,
    payload: CiResultExportCreate,
) -> CiAuthorizationContext:
    """Check the exact source and current principal before provider reads."""
    principal = crud.crud_ci_principal.get(
        db, account_id=actor.account_id, principal_id=payload.principal_id
    )
    if principal is None:
        raise LookupError("Export target unavailable")
    grant = CiGrant.model_validate(principal.grant)
    _owner(db, actor, grant, "grant_result_export")
    repo = _object(principal.repository_binding)
    if (
        not all(isinstance(repo.get(key), str) for key in ("slug", "tracker_type"))
        or "tracker_url" not in repo
    ):
        raise PermissionError("Export target binding unavailable")
    context = CiAuthorizationContext(
        account_id=principal.account_id,
        principal_id=principal.id,
        key_id=UUID(int=0),
        project_id=principal.project_id,
        flow_id=principal.flow_id,
        tracker_id=principal.tracker_id,
        repository_identifier=principal.repository_identifier,
        repository_slug=repo["slug"],
        tracker_type=repo["tracker_type"],
        tracker_url=repo["tracker_url"],
        actions=frozenset(grant.actions),
    )
    context = crud.crud_ci_principal.authorize_principal(
        db, context=context, action=CiAction.READ_RESULT_EXPORT
    )
    moment = datetime.now(timezone.utc)
    if (
        payload.expires_at.tzinfo is None
        or not moment < payload.expires_at <= moment + timedelta(hours=24)
    ):
        raise ValueError("Export consent must expire within 24 hours")
    _source(db, execution_id=execution_id, context=context, payload=payload)
    return context


def _source(
    db: Session,
    *,
    execution_id: UUID,
    context: CiAuthorizationContext,
    payload: CiResultExportCreate,
) -> models.FlowExecution:
    execution = (
        db.query(models.FlowExecution)
        .populate_existing()
        .join(models.Flow, models.Flow.id == models.FlowExecution.flow_id)
        .filter(
            models.FlowExecution.id == execution_id,
            models.Flow.account_id == context.account_id,
        )
        .first()
    )
    if execution is None:
        raise LookupError("Execution unavailable")
    if (
        execution.status != "SUCCEEDED"
        or execution.end_time is None
        or execution.result is None
        or any(
            getattr(execution, name) is not None
            for name in ("ci_principal_id", "ci_review_binding", "initiating_ci_key_id")
        )
        or _object(execution.result_schema_validation or {}).get("valid") is False
        or digest(public_ci_result(execution.result)) != payload.result_sha256
    ):
        raise ValueError(
            "Only an unchanged successful account-owned result can be exported"
        )
    details = _object(execution.trigger_event_details)
    body = _object(details.get("payload"))
    if details.get("source") != context.tracker_type or details.get(
        "project_id"
    ) != str(context.project_id):
        raise ValueError("Source execution has no matching recorded project binding")
    repository = (
        body.get("repository")
        if context.tracker_type == "github"
        else body.get("project")
    )
    repository = repository if isinstance(repository, dict) else {}
    if str(repository.get("id")) != context.repository_identifier:
        raise ValueError("Source execution repository differs from export target")
    if context.tracker_type == "github":
        pr = _object(body.get("pull_request"))
        head, base = _object(pr.get("head")), _object(pr.get("base"))
        valid = (
            pr.get("number") == payload.pr_number
            and head.get("sha") == payload.head_sha
            and base.get("sha") == payload.base_sha
            and all(
                str(_object(side.get("repo")).get("id"))
                == context.repository_identifier
                for side in (head, base)
            )
        )
    else:
        pr = _object(body.get("object_attributes"))
        valid = (
            pr.get("iid") == payload.pr_number
            and _object(pr.get("last_commit")).get("id") == payload.head_sha
            and _object(pr.get("diff_refs")).get("base_sha") == payload.base_sha
            and str(pr.get("target_project_id")) == context.repository_identifier
            and str(pr.get("source_project_id")) == context.repository_identifier
        )
    if not valid:
        raise ValueError("Source execution has no matching recorded revision binding")
    return execution


def create_consent(
    db: Session,
    *,
    actor: models.User,
    execution_id: UUID,
    payload: CiResultExportCreate,
    verified: CiReviewBinding,
) -> dict[str, Any]:
    """Recheck after provider I/O and record the reviewed bytes without adoption."""
    context = prepare_consent(
        db, actor=actor, execution_id=execution_id, payload=payload
    )
    if (
        verified.pr_number != payload.pr_number
        or verified.head_sha != payload.head_sha
        or any(
            verified.model_dump(mode="json")[key] != value
            for key, value in _identity(context).items()
        )
    ):
        raise PermissionError("Export target changed during verification")
    execution = _source(db, execution_id=execution_id, context=context, payload=payload)
    body = _object(execution.trigger_event_details["payload"])
    original_pr = _object(
        body.get(
            "pull_request" if context.tracker_type == "github" else "object_attributes"
        )
    )
    if str(original_pr.get("id")) != verified.provider_pr_id:
        raise ValueError("Source execution provider review identity differs")
    record = models.ExecutionResultGrant(
        account_id=context.account_id,
        execution_id=execution.id,
        principal_id=context.principal_id,
        granted_by_user_id=actor.id,
        result_sha256=payload.result_sha256,
        expires_at=payload.expires_at,
        binding={
            "review": verified.model_dump(mode="json"),
            "base_sha": payload.base_sha,
            "source_flow_id": str(execution.flow_id),
            "source_result_sha256": digest(execution.result),
            "source_event_sha256": digest(execution.trigger_event_details),
        },
    )
    db.add(record)
    db.flush()
    crud.crud_audit_log.log_action(
        db,
        account_id=actor.account_id,
        user_id=actor.id,
        action="execution_result_export_granted",
        resource_type="flow_execution",
        resource_id=str(execution.id),
        status="success",
        commit=False,
        details={
            "grant_id": str(record.id),
            "principal_id": str(context.principal_id),
            "result_sha256": record.result_sha256,
        },
    )
    db.commit()
    db.refresh(record)
    return {
        "grant_id": str(record.id),
        "execution_id": str(execution.id),
        "principal_id": str(record.principal_id),
        "expires_at": record.expires_at,
        "result_sha256": record.result_sha256,
        "head_sha": verified.head_sha,
        "base_sha": payload.base_sha,
        "purpose": "result_export",
        "publication_authority": "separate_publisher_required",
    }


def load_export(
    db: Session, *, context: CiAuthorizationContext, execution_id: UUID, grant_id: UUID
) -> tuple[models.ExecutionResultGrant, models.FlowExecution, CiReviewBinding]:
    """A key grants no historic ownership; the current explicit consent is required."""
    context = crud.crud_ci_principal.authorize(
        db, context=context, action=CiAction.READ_RESULT_EXPORT
    )
    record = (
        db.query(models.ExecutionResultGrant)
        .populate_existing()
        .filter(
            models.ExecutionResultGrant.id == grant_id,
            models.ExecutionResultGrant.account_id == context.account_id,
            models.ExecutionResultGrant.principal_id == context.principal_id,
            models.ExecutionResultGrant.execution_id == execution_id,
        )
        .first()
    )
    if record is None:
        raise LookupError("Result export unavailable")
    if (
        record.revoked_at is not None
        or record.expires_at <= datetime.now(timezone.utc)
        or record.granted_by_user_id is None
    ):
        raise PermissionError("Result export consent expired or was revoked")
    human = crud.crud_user.get(db, id=record.granted_by_user_id)
    if human is None:
        raise PermissionError("Result export authorizer unavailable")
    principal = crud.crud_ci_principal.get(
        db, account_id=context.account_id, principal_id=context.principal_id
    )
    if principal is None or human.account_id != context.account_id:
        raise PermissionError("Result export authorizer unavailable")
    _owner(db, human, CiGrant.model_validate(principal.grant), "grant_result_export")
    stored = _object(record.binding)
    if set(stored) != {
        "review",
        "base_sha",
        "source_flow_id",
        "source_result_sha256",
        "source_event_sha256",
    }:
        raise PermissionError("Invalid result export receipt")
    binding = CiReviewBinding.model_validate(stored["review"])
    if any(
        binding.model_dump(mode="json")[key] != value
        for key, value in _identity(context).items()
    ):
        raise PermissionError("Result export target binding changed")
    payload = CiResultExportCreate(
        principal_id=context.principal_id,
        pr_number=binding.pr_number,
        head_sha=binding.head_sha,
        base_sha=record.binding["base_sha"],
        result_sha256=record.result_sha256,
        expires_at=record.expires_at,
    )
    execution = _source(db, execution_id=execution_id, context=context, payload=payload)
    if (
        str(execution.flow_id) != record.binding["source_flow_id"]
        or digest(execution.result) != record.binding["source_result_sha256"]
        or digest(execution.trigger_event_details)
        != record.binding["source_event_sha256"]
    ):
        raise PermissionError("Source execution changed after export consent")
    return record, execution, binding


def revoke_consent(
    db: Session, *, actor: models.User, execution_id: UUID, grant_id: UUID
) -> None:
    """Current owner can revoke without requiring an active execution/flow.

    Serialize concurrent revocations and refresh any cached consent so every
    caller preserves the first revocation timestamp required by the DB guard.
    """
    record = (
        db.query(models.ExecutionResultGrant)
        .populate_existing()
        .with_for_update()
        .filter(
            models.ExecutionResultGrant.id == grant_id,
            models.ExecutionResultGrant.execution_id == execution_id,
            models.ExecutionResultGrant.account_id == actor.account_id,
        )
        .first()
    )
    if record is None:
        raise LookupError("Result export unavailable")
    principal = crud.crud_ci_principal.get(
        db, account_id=actor.account_id, principal_id=record.principal_id
    )
    if principal is None:
        raise LookupError("Export target unavailable")
    _owner(db, actor, CiGrant.model_validate(principal.grant), "revoke")
    record.revoked_at = record.revoked_at or datetime.now(timezone.utc)
    crud.crud_audit_log.log_action(
        db,
        account_id=actor.account_id,
        user_id=actor.id,
        action="execution_result_export_revoked",
        resource_type="flow_execution",
        resource_id=str(execution_id),
        status="success",
        commit=False,
        details={"grant_id": str(record.id)},
    )
    db.commit()
