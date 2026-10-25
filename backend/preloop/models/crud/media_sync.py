"""Account-scoped, content-free recording reads and atomic timing updates."""

from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session, defer

from preloop.models import models
from preloop.models.crud import crud_audit_log


def recording_rows(db: Session, *, account_id: UUID, session_id: UUID) -> list[Any]:
    """Read descriptors without loading encrypted recording bytes."""
    return list(
        db.query(models.RuntimeSessionArtifact)
        .options(defer(models.RuntimeSessionArtifact.ciphertext))
        .filter(
            models.RuntimeSessionArtifact.account_id == account_id,
            models.RuntimeSessionArtifact.runtime_session_id == session_id,
            models.RuntimeSessionArtifact.kind.in_(("recording", "screencast")),
        )
        .order_by(
            models.RuntimeSessionArtifact.created_at, models.RuntimeSessionArtifact.id
        )
        .all()
    )


def timing_rows(db: Session, *, account_id: UUID, session_id: UUID) -> list[Any]:
    """Read timing keys only; never detoast model conversation/tool payloads."""
    activity = models.RuntimeSessionActivity
    keys = ("occurred_at", "recorded_at", "source_step_id", "api_usage_id", "clock")
    metadata = func.jsonb_build_object(
        *[part for key in keys for part in (key, activity.metadata_[key])]
    ).label("metadata_")
    return list(
        db.query(activity.id, activity.activity_type, activity.timestamp, metadata)
        .filter(
            activity.account_id == account_id,
            activity.runtime_session_id == session_id,
            activity.activity_type.in_(
                ("browser_step", "tool_call", "model_gateway_call")
            ),
        )
        .order_by(activity.timestamp, activity.id)
        .all()
    )


def update_timing(
    db: Session,
    *,
    account_id: UUID,
    session_id: UUID,
    artifact_id: UUID,
    user_id: UUID | None,
    timing: dict[str, Any],
) -> models.RuntimeSessionArtifact | None:
    """Replace timing and audit it atomically; identical retries are no-ops."""
    # Match artifact deposit/eviction lock ordering before the audit append.
    db.query(models.Account).filter(models.Account.id == account_id).with_for_update(
        key_share=True
    ).one()
    artifact = (
        db.query(models.RuntimeSessionArtifact)
        .options(defer(models.RuntimeSessionArtifact.ciphertext))
        .filter(
            models.RuntimeSessionArtifact.account_id == account_id,
            models.RuntimeSessionArtifact.runtime_session_id == session_id,
            models.RuntimeSessionArtifact.id == artifact_id,
        )
        .with_for_update()
        .one_or_none()
    )
    if artifact is None:
        return None
    if artifact.kind not in ("recording", "screencast"):
        raise ValueError("artifact_timing_kind_invalid")
    if artifact.media_timing == timing:
        return artifact
    artifact.media_timing = timing
    crud_audit_log.log_action(
        db,
        account_id=account_id,
        user_id=user_id,
        action="artifact.timing.updated",
        resource_type="runtime_session_artifact",
        resource_id=str(artifact_id),
        status="success",
        details={"runtime_session_id": str(session_id), "media_timing": timing},
        commit=False,
    )
    db.commit()
    return artifact
