"""Explicit result export, with fresh provider reads and no publication writes."""

from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from preloop.api.loop_safety import run_db_off_loop
from preloop.models import models
from preloop.models.crud import ci_result_export
from preloop.models.crud.ci_principal import CiAuthorizationContext
from preloop.schemas.ci_execution import CiReviewBinding, CiReviewRequest
from preloop.schemas.ci_result_export import CiResultExportCreate
from preloop.services.ci_execution import (
    CiReviewUnavailableError,
    _ProviderRead,
    _provider_source,
    _read_provider,
    public_ci_result,
)
from preloop.services.execution_tool_evidence import digest


def _consent_source(
    db: Session, actor: models.User, execution_id: UUID, payload: CiResultExportCreate
) -> _ProviderRead:
    context = ci_result_export.prepare_consent(
        db, actor=actor, execution_id=execution_id, payload=payload
    )
    return _provider_source(db, context, payload)


async def grant_result_export(
    db: Session,
    *,
    actor: models.User,
    execution_id: UUID,
    payload: CiResultExportCreate,
) -> dict[str, Any]:
    """Require explicit reviewed bytes and revisions, then persist fresh consent."""
    try:
        source = await run_db_off_loop(
            lambda: _consent_source(db, actor, execution_id, payload)
        )
        verified = await _read_provider(source, expected_base_sha=payload.base_sha)
        return await run_db_off_loop(
            lambda: ci_result_export.create_consent(
                db,
                actor=actor,
                execution_id=execution_id,
                payload=payload,
                verified=verified,
            )
        )
    except SQLAlchemyError:
        raise CiReviewUnavailableError() from None


def _export_source(
    db: Session, context: CiAuthorizationContext, execution_id: UUID, grant_id: UUID
) -> tuple[_ProviderRead, str]:
    record, _, binding = ci_result_export.load_export(
        db, context=context, execution_id=execution_id, grant_id=grant_id
    )
    base_sha = record.binding["base_sha"]
    source = _provider_source(
        db,
        context,
        CiReviewRequest(pr_number=binding.pr_number, head_sha=binding.head_sha),
    )
    return source, base_sha


def _projection(
    db: Session,
    context: CiAuthorizationContext,
    execution_id: UUID,
    grant_id: UUID,
    verified: CiReviewBinding,
) -> dict[str, Any]:
    record, execution, binding = ci_result_export.load_export(
        db, context=context, execution_id=execution_id, grant_id=grant_id
    )
    # Key rotation and edited title/description do not change the reviewed commit.
    ignored = {
        "key_id",
        "review_url",
        "review_title",
        "review_description",
        "review_author",
        "review_source_branch",
    }
    if verified.model_dump(exclude=ignored) != binding.model_dump(exclude=ignored):
        raise PermissionError("Result export review binding changed")
    receipt = {
        "version": 1,
        "account_id": str(context.account_id),
        "execution_id": str(execution.id),
        "flow_id": str(binding.flow_id),
        "project_id": str(binding.project_id),
        "tracker_id": str(binding.tracker_id),
        "tracker_type": binding.tracker_type,
        "tracker_url": binding.tracker_url,
        "repository_identifier": binding.repository_identifier,
        "repository_slug": binding.repository_slug,
        "pr_number": binding.pr_number,
        "provider_pr_id": binding.provider_pr_id,
        "head_sha": binding.head_sha,
        "base_sha": record.binding["base_sha"],
        "result_sha256": record.result_sha256,
    }
    return {
        "grant_id": str(record.id),
        "principal_id": str(record.principal_id),
        "expires_at": record.expires_at,
        "receipt": receipt,
        "publication_idempotency_key": "preloop-result-export-v1:" + digest(receipt),
        "purpose": "result_export",
        "publication_authority": "separate_publisher_required",
        "status": execution.status,
        "result": public_ci_result(execution.result),
    }


async def export_result(
    db: Session, *, context: CiAuthorizationContext, execution_id: UUID, grant_id: UUID
) -> dict[str, Any]:
    """Recheck consent after a bounded provider read; never adopt or rerun work."""
    try:
        source, base_sha = await run_db_off_loop(
            lambda: _export_source(db, context, execution_id, grant_id)
        )
        verified = await _read_provider(source, expected_base_sha=base_sha)
        return await run_db_off_loop(
            lambda: _projection(db, context, execution_id, grant_id, verified)
        )
    except SQLAlchemyError:
        raise CiReviewUnavailableError() from None
