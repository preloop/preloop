"""Instance registration service for self-hosted Preloop installations.

This service:
1. Creates/retrieves the local instance record on startup
2. Sends version check POST to preloop.ai for telemetry
3. Checks for updates periodically (default: once per day)
"""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx

from preloop.config import SERVER_VERSION

logger = logging.getLogger(__name__)

# Preloop.ai version check endpoint
VERSION_CHECK_URL = "https://preloop.ai/api/v1/version"

# Environment variables to disable telemetry (accept both for backwards compatibility)
DISABLE_TELEMETRY_ENV = "PRELOOP_DISABLE_TELEMETRY"
DISABLE_VERSION_CHECK_ENV = "DISABLE_VERSION_CHECK"

# Set on the hosted preloop.ai deployment itself: it IS the version-check
# tracker, so it must never show itself an "update available" banner.
HOSTED_ENV = "PRELOOP_HOSTED"

# Truthy values for the opt-out variables: true/1/t/yes, case-insensitive,
# surrounding whitespace ignored. Must stay aligned with the Go CLI's parsing
# in cli/internal/telemetry/optout.go — one variable, one parsing rule.
_TRUTHY_VALUES = ("true", "1", "t", "yes")


def is_telemetry_disabled() -> bool:
    """Check if telemetry is disabled via environment variables.

    Accepts both PRELOOP_DISABLE_TELEMETRY and DISABLE_VERSION_CHECK for
    backwards compatibility with documentation.
    """
    return (
        os.getenv(DISABLE_TELEMETRY_ENV, "").strip().lower() in _TRUTHY_VALUES
        or os.getenv(DISABLE_VERSION_CHECK_ENV, "").strip().lower() in _TRUTHY_VALUES
    )


def is_hosted_instance() -> bool:
    """True when this deployment is the hosted preloop.ai service itself."""
    return os.getenv(HOSTED_ENV, "").strip().lower() in _TRUTHY_VALUES


def _parse_semver(value: object) -> Optional[tuple]:
    """Parse a dotted version into an int tuple; None when unparseable.

    Accepts an optional leading ``v`` and ignores pre-release/build suffixes
    on each component (``1.0.0-rc1`` -> ``(1, 0, 0)``). Anything without
    numeric components (or not a string) is unparseable.
    """
    if not isinstance(value, str):
        return None
    v = value.strip().lstrip("v")
    if not v:
        return None
    parts = []
    for part in v.split("."):
        head = part.split("-")[0].split("+")[0]
        if not head.isdigit():
            return None
        parts.append(int(head))
    return tuple(parts)


def is_newer_version(candidate: object, current: str) -> bool:
    """True only when ``candidate`` is a valid version STRICTLY newer.

    Unparseable candidates (or currents) are never "newer" — a garbage or
    hardcoded latest-version value from the tracker must not light up the
    update banner.
    """
    parsed_candidate = _parse_semver(candidate)
    parsed_current = _parse_semver(current)
    if parsed_candidate is None or parsed_current is None:
        return False
    width = max(len(parsed_candidate), len(parsed_current))
    parsed_candidate += (0,) * (width - len(parsed_candidate))
    parsed_current += (0,) * (width - len(parsed_current))
    return parsed_candidate > parsed_current


# Version check interval (default: 24 hours)
VERSION_CHECK_INTERVAL = int(os.getenv("VERSION_CHECK_INTERVAL", "86400"))

# Global state for periodic checker
_version_check_task: Optional[asyncio.Task] = None
_current_instance: Optional["Instance"] = None


def get_or_create_instance() -> Optional["Instance"]:
    """Get or create the local instance record.

    For self-hosted installations, this creates a single Instance record
    that represents this installation. The instance_uuid is generated once
    and persisted in the database.

    Returns:
        The Instance record, or None if database is not available.
    """
    from preloop.models.db.session import get_db_session
    from preloop.models.models import Instance

    try:
        db = next(get_db_session())
        try:
            # Check if we already have a local instance record
            instance = db.query(Instance).first()

            if instance:
                # Update version if it changed
                if instance.version != SERVER_VERSION:
                    logger.info(
                        f"Updating instance version from {instance.version} "
                        f"to {SERVER_VERSION}"
                    )
                    instance.version = SERVER_VERSION
                    instance.last_seen = datetime.now(timezone.utc)
                    db.commit()
                    db.refresh(instance)
                return instance

            # Create new instance record
            edition = "enterprise" if _is_enterprise() else "oss"
            instance = Instance(
                instance_uuid=uuid.uuid4(),
                version=SERVER_VERSION,
                edition=edition,
                first_seen=datetime.now(timezone.utc),
                last_seen=datetime.now(timezone.utc),
                is_active=True,
            )
            db.add(instance)
            db.commit()
            db.refresh(instance)

            logger.info(
                f"Created new instance record: {instance.instance_uuid} "
                f"(version={instance.version}, edition={instance.edition})"
            )
            return instance

        finally:
            db.close()

    except Exception as e:
        logger.warning(f"Could not get/create instance record: {e}")
        return None


def _is_enterprise() -> bool:
    """Check if this is an enterprise installation."""
    # Check for enterprise plugins or features
    try:
        # If proprietary plugins are available, it's enterprise
        import preloop.plugins.proprietary  # noqa: F401

        return True
    except ImportError:
        return False


async def send_version_check(instance: "Instance") -> bool:
    """Send version check POST to preloop.ai.

    This is opt-out telemetry that helps us understand adoption.
    Set PRELOOP_DISABLE_TELEMETRY=true to disable.

    Args:
        instance: The local instance record.

    Returns:
        True if successful, False otherwise.
    """
    if is_telemetry_disabled():
        logger.debug("Telemetry disabled via environment variable")
        return False

    try:
        payload = {
            "instance_uuid": str(instance.instance_uuid),
            "version": instance.version,
            "edition": instance.edition,
            "metadata": instance.metadata_ or {},
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(VERSION_CHECK_URL, json=payload)

            if response.status_code == 200:
                data = response.json()
                if data.get("update_available"):
                    logger.info(
                        f"Update available: {data.get('current_version')} "
                        f"(you have {instance.version})"
                    )
                # Persist the result so the web UI (and /api/v1/version) can
                # surface an update banner to admins between checks.
                _persist_version_check_result(data)
                return True
            else:
                logger.debug(
                    f"Version check returned {response.status_code}: {response.text}"
                )
                return False

    except httpx.TimeoutException:
        logger.debug("Version check timed out")
        return False
    except Exception as e:
        logger.debug(f"Version check failed: {e}")
        return False


def _persist_version_check_result(data: dict) -> None:
    """Store the version-check response on the local instance record.

    Written to ``Instance.metadata_["version_check"]`` so the update status
    survives restarts and can be read by ``GET /api/v1/version`` for the
    admin update banner.

    The hosted preloop.ai deployment never persists update state (it IS the
    tracker), and the persisted ``update_available`` is recomputed locally:
    a "latest" version that is not strictly newer than this server's own
    version must never be stored as an available update.
    """
    if is_hosted_instance():
        return

    from preloop.models.db.session import get_db_session
    from preloop.models.models import Instance

    latest_version = data.get("current_version")
    update_available = bool(data.get("update_available")) and is_newer_version(
        latest_version, SERVER_VERSION
    )

    try:
        db = next(get_db_session())
        try:
            row = db.query(Instance).first()
            if row is None:
                return
            row.metadata_ = {
                **(row.metadata_ or {}),
                "version_check": {
                    "latest_version": latest_version,
                    "update_available": update_available,
                    "update_url": data.get("update_url"),
                    "changelog_url": data.get("changelog_url"),
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                },
            }
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"Could not persist version check result: {e}")


def get_local_update_status() -> dict:
    """Read the persisted update status for this installation.

    Returns:
        Dict with ``update_available`` (bool), ``latest_version``,
        ``update_url``, ``changelog_url``, ``checked_at`` — all falsy/None
        when telemetry is disabled or no check has completed yet.
    """
    empty = {
        "update_available": False,
        "latest_version": None,
        "update_url": None,
        "changelog_url": None,
        "checked_at": None,
    }
    if is_telemetry_disabled() or is_hosted_instance():
        # Hosted preloop.ai IS the update tracker: never surface a banner.
        return empty
    from preloop.models.db.session import get_db_session
    from preloop.models.models import Instance

    try:
        db = next(get_db_session())
        try:
            row = db.query(Instance).first()
            if row is None or not row.metadata_:
                return empty
            check = row.metadata_.get("version_check") or {}
            status = {**empty, **check}
            # Never trust the persisted boolean: recompute against this
            # server's version so a stale/bogus "latest" (e.g. a hardcoded
            # 1.0.0 from an old tracker deploy) cannot show an update.
            status["update_available"] = is_newer_version(
                status.get("latest_version"), SERVER_VERSION
            )
            return status
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"Could not read version check status: {e}")
        return empty


async def register_instance() -> None:
    """Register the local instance and send version check.

    Called during application startup. Creates the local instance record
    and sends a version check to preloop.ai in the background.
    """
    logger.info("Registering instance...")

    instance = get_or_create_instance()
    if not instance:
        logger.warning("Could not register instance - database not available")
        return

    logger.info(
        f"Instance registered: {instance.instance_uuid} "
        f"(version={instance.version}, edition={instance.edition})"
    )

    # Send version check in background (don't block startup)
    asyncio.create_task(_background_version_check(instance))


async def _background_version_check(instance: "Instance") -> None:
    """Background task to send initial version check and start periodic checker."""
    global _current_instance

    # Small delay to let the app fully start
    await asyncio.sleep(5)

    _current_instance = instance

    # Send initial version check
    success = await send_version_check(instance)
    if success:
        logger.debug("Initial version check sent successfully")

    # Start periodic version checker
    _start_periodic_checker()


async def _periodic_version_check_loop() -> None:
    """Background loop that checks for updates periodically."""
    global _current_instance

    while True:
        # Wait for the configured interval
        await asyncio.sleep(VERSION_CHECK_INTERVAL)

        if _current_instance is None:
            logger.debug("No instance registered, skipping periodic version check")
            continue

        try:
            success = await send_version_check(_current_instance)
            if success:
                logger.debug("Periodic version check completed")
        except Exception as e:
            logger.warning(f"Periodic version check failed: {e}")


def _start_periodic_checker() -> None:
    """Start the periodic version check background task."""
    global _version_check_task

    if is_telemetry_disabled():
        logger.debug("Periodic version checker disabled (telemetry disabled)")
        return

    if _version_check_task is not None:
        logger.debug("Periodic version checker already running")
        return

    logger.info(
        f"Starting periodic version checker (interval: {VERSION_CHECK_INTERVAL}s)"
    )
    _version_check_task = asyncio.create_task(_periodic_version_check_loop())


def stop_version_checker() -> None:
    """Stop the periodic version checker.

    Called during application shutdown.
    """
    global _version_check_task

    if _version_check_task is not None:
        _version_check_task.cancel()
        _version_check_task = None
        logger.info("Periodic version checker stopped")
