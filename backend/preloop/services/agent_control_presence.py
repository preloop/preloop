"""One definition of "the Agent Control plugin is connected".

Presence has two sources and they must agree. The api process that holds the
managed agent's WebSocket knows the truth first hand; every other process (the
second api replica, a worker) can only read what the socket wrote to the
database. Cloud runs more than one api replica, so a request answered by the
process without the socket has to reach the same verdict or the console shows
an agent that toggles online/offline at polling cadence.

The persisted signal is ``managed_agent.control_last_heartbeat_at``, written
only by the Agent Control WebSocket. ``last_seen_at`` is not usable for this:
enrollment and ordinary gateway traffic stamp it too, so an agent whose plugin
died would keep looking connected for as long as it kept making model calls.

The freshness window is deliberately three heartbeats wide. Plugins beat every
``AGENT_CONTROL_HEARTBEAT_INTERVAL`` and a single late beat (a busy Node event
loop, a laptop asleep for a moment, a reconnect backoff) must not flip the
badge.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Optional

#: How often runtime plugins send a heartbeat envelope. Mirrors
#: ``HEARTBEAT_INTERVAL_MS`` in every runtime plugin under ``runtime-plugins/``.
AGENT_CONTROL_HEARTBEAT_INTERVAL = timedelta(seconds=30)

#: How long a heartbeat keeps an agent "connected" for processes that do not
#: hold its WebSocket. Three beats: one lost beat is noise, three is a gone
#: plugin.
AGENT_CONTROL_PRESENCE_TTL = AGENT_CONTROL_HEARTBEAT_INTERVAL * 3


def control_heartbeat_is_fresh(
    heartbeat_at: Any, *, now: Optional[datetime] = None
) -> bool:
    """Is this the timestamp of a heartbeat inside the presence window?

    Args:
        heartbeat_at: Persisted ``control_last_heartbeat_at``, aware or naive
            UTC, or ``None`` when the agent has never connected.
        now: Override for tests.

    Returns:
        True when a control heartbeat landed inside
        :data:`AGENT_CONTROL_PRESENCE_TTL`.
    """
    if not isinstance(heartbeat_at, datetime):
        return False
    if heartbeat_at.tzinfo is None:
        heartbeat_at = heartbeat_at.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    try:
        return reference - heartbeat_at <= AGENT_CONTROL_PRESENCE_TTL
    except TypeError:
        return False
