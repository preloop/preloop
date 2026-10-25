"""Optional, permission-scoped links from an agent to fleet devices."""

import logging
from typing import Any

from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.models.crud.fleet_map import list_resources
from preloop.plugins.account_hooks import VISIBLE_MANAGED_AGENT, filter_viewable
from preloop.plugins.base import get_plugin_manager
from preloop.schemas.gateway_usage import ManagedAgentDeviceSummary
from preloop.services.agent_device_resolution import resolve_hostname_devices

logger = logging.getLogger(__name__)


def agent_device_links(
    db: Session, *, user: models.User | None, agent_id: str
) -> list[ManagedAgentDeviceSummary]:
    """Return no device metadata when the fleet extension is unavailable."""
    if user is None:
        return []
    service = get_plugin_manager().get_service("device_fleet")
    provider = getattr(service, "devices_for_agent", None)
    if provider is None:
        return []
    try:
        linked = [
            ManagedAgentDeviceSummary.model_validate(item)
            for item in provider(db, user=user, agent_id=agent_id)
        ]
        return linked or _hostname_device_links(db, service, user, agent_id)
    except Exception:
        # This optional extension must not take down the agent's primary read,
        # including when a plugin returns a response from an incompatible schema.
        logger.exception("Failed to load optional device links for agent %s", agent_id)
        return []


def _hostname_device_links(
    db: Session, service: Any, user: models.User, agent_id: str
) -> list[ManagedAgentDeviceSummary]:
    """Place an agent with no fingerprint link the way the fleet map does.

    Uses the same enrollment-hostname resolution as the fleet map, under the
    same fleet permission, so the agent page and the map name one device.
    """
    from preloop.services.fleet_map import SOURCE_LIMIT, _allowed

    read = getattr(service, "read", None)
    if read is None:
        return []
    account = crud.crud_account.get(db, id=user.account_id)
    owner = account is not None and str(account.primary_user_id) == str(user.id)
    if not _allowed(db, user, "view_device_fleet", owner):
        return []
    # Resolve against the same agent source the fleet map reads, so sibling
    # agents make a match ambiguous here exactly as they do on the map.
    rows = list_resources(
        db,
        source="agents",
        account_id=str(user.account_id),
        limit=SOURCE_LIMIT + 1,
    )
    if len(rows) > SOURCE_LIMIT:
        # The fleet map skips the fallback on a partial window too.
        return []
    agents = filter_viewable(db, user, VISIBLE_MANAGED_AGENT, rows)
    if not any(str(row.id) == str(agent_id) for row in agents):
        return []
    projection = read(db, account_id=user.account_id)
    devices = projection["items"]
    if projection.get("truncated") or len(devices) > SOURCE_LIMIT:
        # A partial window cannot prove the match is unique; the fleet map
        # applies the same two checks.
        return []
    device_key = resolve_hostname_devices(devices, agents).get(str(agent_id))
    return [
        ManagedAgentDeviceSummary.model_validate(
            {
                key: device.get(key)
                for key in (
                    "device_key",
                    "hostname",
                    "os",
                    "runner_state",
                    "last_seen_at",
                )
            }
        )
        for device in devices
        if device_key is not None and device["device_key"] == device_key
    ]
