"""Janitor for captured workspace snapshots and Docker workspace volumes.

Retention is a decision, not a leak. Every hosted run stores a tar.gz of
``/workspace`` on its execution row, and the Docker runner leaves a named
volume ``agent-workspace-<execution_id>`` behind. Both are kept for
``WORKSPACE_SNAPSHOT_TTL_HOURS`` (24 by default, 0 meaning "delete on the next
pass") and removed after that by :func:`cleanup_workspace_artifacts`, which
the scheduler publishes hourly.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.models.flow_execution import FlowExecution
from preloop.utils.workspace_snapshot import WORKSPACE_VOLUME_PREFIX

logger = logging.getLogger(__name__)


def workspace_snapshot_cutoff(now: Optional[datetime] = None) -> datetime:
    """Return the timestamp before which snapshots are reaped."""

    ttl_hours = max(0, int(getattr(settings, "workspace_snapshot_ttl_hours", 24) or 0))
    reference = now or datetime.now(UTC).replace(tzinfo=None)
    return reference - timedelta(hours=ttl_hours)


def purge_expired_snapshots(db: Session, *, cutoff: datetime) -> int:
    """Clear ``workspace_snapshot`` on executions that ended before ``cutoff``.

    Only the blob is cleared: the execution row, its logs and its evidence
    pack stay, so history is never rewritten by retention.
    """

    query = (
        db.query(FlowExecution)
        .filter(FlowExecution.workspace_snapshot.isnot(None))
        .filter(
            or_(
                FlowExecution.end_time < cutoff,
                FlowExecution.end_time.is_(None),
            )
        )
        .filter(FlowExecution.start_time < cutoff)
    )
    purged = query.update(
        {FlowExecution.workspace_snapshot: None},
        synchronize_session=False,
    )
    if purged:
        db.commit()
        logger.info("Purged %d expired workspace snapshot(s)", purged)
    return purged


async def purge_expired_docker_volumes(*, cutoff: datetime) -> int:
    """Remove ``agent-workspace-*`` Docker volumes created before ``cutoff``.

    Best effort: no Docker socket (Kubernetes deployments, or a control plane
    that does not run the Docker runner) is a no-op, and a volume still in use
    by a running container is skipped by the daemon.
    """

    try:
        import aiodocker
    except Exception:  # pragma: no cover - aiodocker is a hard dependency
        logger.debug("aiodocker unavailable; skipping workspace volume cleanup")
        return 0

    docker = None
    removed = 0
    try:
        docker = aiodocker.Docker()
        payload = await docker.volumes.list()
        for volume in payload.get("Volumes") or []:
            name = volume.get("Name") or ""
            if not name.startswith(WORKSPACE_VOLUME_PREFIX):
                continue
            if not _volume_is_expired(volume, cutoff=cutoff):
                continue
            try:
                await docker.volumes.get(name).delete()
                removed += 1
            except Exception as e:
                logger.info("Could not remove workspace volume %s: %s", name, e)
    except Exception as e:
        logger.info("Workspace volume cleanup skipped: %s", e)
    finally:
        if docker is not None:
            try:
                await docker.close()
            except Exception:  # pragma: no cover - close is best effort
                pass
    if removed:
        logger.info("Removed %d expired workspace volume(s)", removed)
    return removed


def _volume_is_expired(volume: Dict[str, Any], *, cutoff: datetime) -> bool:
    """Whether a Docker volume payload is older than the cutoff."""

    created = volume.get("CreatedAt")
    if not created:
        # A daemon that does not report CreatedAt would otherwise pin the
        # volume forever; treat it as expired so retention still applies.
        return True
    try:
        parsed = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed < cutoff


async def cleanup_workspace_artifacts(
    db: Session, *, now: Optional[datetime] = None
) -> Dict[str, int]:
    """Run one retention pass over snapshots and Docker workspace volumes."""

    cutoff = workspace_snapshot_cutoff(now)
    snapshots = purge_expired_snapshots(db, cutoff=cutoff)
    volumes = await purge_expired_docker_volumes(cutoff=cutoff)
    return {"snapshots_purged": snapshots, "volumes_removed": volumes}


__all__ = [
    "cleanup_workspace_artifacts",
    "purge_expired_docker_volumes",
    "purge_expired_snapshots",
    "workspace_snapshot_cutoff",
]
