"""Historical duration and current sampled state have independent meanings."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from preloop.schemas.readiness import ReadinessObservation, TicketCreationEvidence

STALE_AFTER = timedelta(minutes=10)


def readiness_fields(
    creation: TicketCreationEvidence | None,
    observations: list[tuple[ReadinessObservation | None, ReadinessObservation | None]],
    reason: str | None,
    policy_version: UUID | None = None,
    *,
    now: datetime,
) -> dict[str, Any]:
    """Flatten one unambiguous policy series; retain multi-PR sampled details."""
    fields: dict[str, Any] = {
        "ticket_created_at": creation.created_at if creation else None,
        "ticket_created_at_provenance": creation,
        "first_ready_observed_at": None,
        "ticket_to_observed_ready_hours": None,
        "readiness_scope": "configured_policy" if policy_version else None,
        "readiness_policy_version": policy_version,
        "first_ready_source_sha": None,
        "first_ready_target_sha": None,
        "first_ready_observation_id": None,
        "latest_readiness_state": "unknown",
        "latest_readiness_coverage": "unsupported",
        "latest_readiness_observed_at": None,
        "forge_coverage": "unknown",
        "readiness_unknown_reasons": [reason] if reason else [],
        "readiness_observation_started_at": None,
        "readiness_observation_completed_at": None,
        "readiness_observations": [latest for _, latest in observations if latest],
    }
    if reason:
        return fields
    if len(observations) != 1:
        fields["readiness_unknown_reasons"] = [
            "ambiguous_pr" if len(observations) > 1 else "missing_observation"
        ]
        return fields
    first, latest = observations[0]
    if latest:
        fields.update(
            readiness_scope=latest.scope,
            readiness_policy_version=latest.policy_version,
            latest_readiness_state=latest.state,
            latest_readiness_coverage=latest.coverage,
            latest_readiness_observed_at=latest.completed_at,
            readiness_observation_started_at=latest.started_at,
            readiness_observation_completed_at=latest.completed_at,
            readiness_unknown_reasons=list(latest.reasons),
        )
        if now.astimezone(UTC) - latest.completed_at.astimezone(UTC) > STALE_AFTER:
            fields["latest_readiness_state"] = "unknown"
            fields["readiness_unknown_reasons"].append("stale")
    else:
        fields["readiness_unknown_reasons"].append("missing_observation")
    if first:
        fields.update(
            first_ready_observed_at=first.completed_at,
            readiness_scope=first.scope,
            readiness_policy_version=first.policy_version,
            first_ready_source_sha=first.source_sha,
            first_ready_target_sha=first.target_sha,
            first_ready_observation_id=first.observation_id,
        )
        if creation and creation.created_at:
            elapsed = (
                first.completed_at.astimezone(UTC) - creation.created_at.astimezone(UTC)
            ).total_seconds()
            if elapsed < 0:
                fields["readiness_unknown_reasons"].append("invalid_time_order")
            else:
                fields["ticket_to_observed_ready_hours"] = elapsed / 3600
        else:
            fields["readiness_unknown_reasons"].append(
                creation.reason
                if creation and creation.reason
                else "ticket_creation_unavailable"
            )
    return fields
