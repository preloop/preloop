import logging
import os
import re
from typing import Annotated, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session  # Import synchronous Session

from preloop.config import (
    SERVER_VERSION,
    MIN_CLIENT_VERSION,
    MAX_CLIENT_VERSION,
    settings,
)  # Import constants directly
from preloop.models.crud import crud_audit_log
from preloop.api.auth import get_current_active_user, get_current_user
from preloop.models.models import User
from preloop.schemas.version import ClientVersionInfo, VersionInfo, VersionStatus
from preloop.services.instance_service import (
    get_local_update_status,
    is_telemetry_disabled,
)
from preloop.utils import get_client_ip
from preloop.models.db.session import (
    get_db_session,
    get_session_factory,
)  # Correct function name

# Account model is returned by get_current_user
from preloop.models.models.client_version_log import ClientVersionLog
from fastapi import HTTPException  # To catch auth errors

logger = logging.getLogger(__name__)
router = APIRouter()

# Matches the CLI's User-Agent, e.g. "preloop-cli/0.10.0 (darwin; arm64)".
CLI_USER_AGENT_RE = re.compile(r"^preloop-cli/(?P<version>[0-9A-Za-z.+-]+)")


def _log_cli_activity(
    *,
    request: Request,
    client_ip: Optional[str],
    client_version_header: Optional[str],
) -> None:
    """Record a CLI check-in in the audit log for adoption analytics.

    The CLI hits ``GET /api/v1/version`` at most once per day per machine
    (client-side throttle), so one audit row per check-in stays cheap. Only
    active when ``INSTALLER_AUDIT_ACCOUNT_ID`` is configured — the same
    opt-in used for installer download analytics.

    Uses a short-lived session so ``log_action``'s commit cannot flush or
    commit unrelated pending work on the request-scoped session.
    """
    if not settings.installer_audit_account_id:
        return

    match = CLI_USER_AGENT_RE.match(request.headers.get("user-agent", ""))
    if not match:
        return

    audit_db = get_session_factory()()
    try:
        crud_audit_log.log_action(
            audit_db,
            account_id=settings.installer_audit_account_id,
            action="cli_activity",
            resource_type="cli",
            resource_id="version_check",
            status="success",
            ip_address=client_ip,
            user_agent=request.headers.get("user-agent"),
            details={
                "cli_version": client_version_header or match.group("version"),
            },
        )
    except SQLAlchemyError:
        # Fire-and-forget: never fail the version response for audit errors.
        # Do not catch broader Exception — programming errors should surface.
        try:
            audit_db.rollback()
        except SQLAlchemyError:
            logger.debug(
                "Failed to roll back CLI activity audit session",
                exc_info=True,
            )
        logger.exception("Failed to record CLI activity audit log")
    finally:
        audit_db.close()


@router.get("/version", response_model=VersionInfo)
async def get_version_info(
    request: Request,
    x_client_version: Annotated[Optional[str], Header(alias="X-Client-Version")] = None,
    x_client_organization: Annotated[
        Optional[str], Header(alias="X-Client-Organization")
    ] = None,
    x_client_project: Annotated[Optional[str], Header(alias="X-Client-Project")] = None,
    x_additional_info: Annotated[
        Optional[str], Header(alias="X-Additional-Info")
    ] = None,
    db: Session = Depends(get_db_session),  # Use synchronous Session type hint
    # Removed token dependency: token: Optional[str] = Depends(oauth2_scheme),
):
    """
    Returns the server version information and logs the client version.

    Accepts an optional `X-Client-Version` header from the client.
    If an `Authorization: Bearer <token>` header is provided and valid,
    the associated account ID will also be logged.
    """
    client_ip = get_client_ip(request)
    account_id: Optional[int] = None
    current_user = None
    # Explicitly check header for optional authentication
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.replace("Bearer ", "")
        try:
            # Manually attempt to get user if token exists
            current_user = get_current_user(token=token, db=db)
            account_id = current_user.account_id
        except HTTPException:
            # Ignore auth errors (invalid token, inactive user, etc.)
            logger.debug(
                "Optional authentication failed for /version endpoint (token provided but invalid/inactive)."
            )
        except Exception as e:
            # Log unexpected errors but don't fail the request
            logger.error(
                f"Unexpected error during optional auth in /version: {e}", exc_info=True
            )

    # Log the client version information
    log_entry = ClientVersionLog(
        ip_address=client_ip,
        client_version=x_client_version if x_client_version else "unknown",
        account_id=account_id,
        organization_identifier=x_client_organization,
        project_identifier=x_client_project,
    )
    db.add(log_entry)
    try:
        db.commit()  # Remove await for synchronous commit
        logger.info(
            f"Logged client version: IP={client_ip}, Version={x_client_version}, Org={x_client_organization}, Proj={x_client_project}, AccountID={account_id}, AdditionalInfo={x_additional_info}"
        )
    except Exception as e:
        db.rollback()  # Remove await for synchronous rollback
        logger.error(f"Failed to log client version: {e}", exc_info=True)
        # Continue even if logging fails, returning version info is primary goal

    # Best-effort audit; uses its own session (sync, matching other audit paths).
    _log_cli_activity(
        request=request,
        client_ip=client_ip,
        client_version_header=x_client_version,
    )

    update_status = get_local_update_status()

    return VersionInfo(
        server_version=SERVER_VERSION,
        min_client_version=MIN_CLIENT_VERSION,
        max_client_version=MAX_CLIENT_VERSION,
        latest_version=update_status.get("latest_version") or SERVER_VERSION,
        min_version=MIN_CLIENT_VERSION,
        download_url="https://preloop.ai/install/cli",
        update_available=bool(update_status.get("update_available")),
        clients=_client_version_contracts(),
    )


_IOS_STORE_HOSTS = frozenset({"apps.apple.com"})
_ANDROID_STORE_HOSTS = frozenset({"play.google.com"})
_DEFAULT_IOS_STORE_URL = "https://apps.apple.com/app/preloop/id6757803021"
_DEFAULT_ANDROID_STORE_URL = (
    "https://play.google.com/store/apps/details?id=ai.spacecode.preloop"
)


def _validated_store_url(url: str, allowed_hosts: frozenset[str], default: str) -> str:
    """Return url when it is https and on an allowed host, else default."""
    candidate = (url or "").strip() or default
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return default
    if parsed.scheme != "https" or not parsed.hostname:
        return default
    host = parsed.hostname.lower()
    if host in allowed_hosts or any(
        host.endswith(f".{allowed}") for allowed in allowed_hosts
    ):
        return candidate
    return default


def _client_version_contracts() -> dict[str, ClientVersionInfo]:
    """Per-platform native-app update contracts from deploy-time env vars."""
    contracts: dict[str, ClientVersionInfo] = {}
    ios_latest = os.environ.get("IOS_LATEST_VERSION", "").strip()
    android_latest = os.environ.get("ANDROID_LATEST_VERSION", "").strip()
    if ios_latest:
        contracts["ios"] = ClientVersionInfo(
            min_version=os.environ.get("IOS_MIN_VERSION", "").strip(),
            latest_version=ios_latest,
            store_url=_validated_store_url(
                os.environ.get("IOS_APP_STORE_URL", _DEFAULT_IOS_STORE_URL),
                _IOS_STORE_HOSTS,
                _DEFAULT_IOS_STORE_URL,
            ),
        )
    if android_latest:
        contracts["android"] = ClientVersionInfo(
            min_version=os.environ.get("ANDROID_MIN_VERSION", "").strip(),
            latest_version=android_latest,
            store_url=_validated_store_url(
                os.environ.get("ANDROID_PLAY_STORE_URL", _DEFAULT_ANDROID_STORE_URL),
                _ANDROID_STORE_HOSTS,
                _DEFAULT_ANDROID_STORE_URL,
            ),
        )
    return contracts


@router.get("/version/status", response_model=VersionStatus)
async def get_version_status(
    current_user: User = Depends(get_current_active_user),
):
    """Update status for this installation (admins only).

    Drives the console's "a newer Preloop is available" banner for
    self-hosted administrators. Reflects the instance's own daily version
    check; reports no update when telemetry is disabled or no check has
    completed.
    """
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin access required")

    status = get_local_update_status()
    return VersionStatus(
        version=SERVER_VERSION,
        update_available=bool(status.get("update_available")),
        latest_version=status.get("latest_version"),
        update_url=status.get("update_url"),
        changelog_url=status.get("changelog_url"),
        checked_at=status.get("checked_at"),
        telemetry_enabled=not is_telemetry_disabled(),
    )
