import logging
import re
from typing import Annotated, Optional

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
from preloop.api.auth import get_current_user  # Remove oauth2_scheme import
from preloop.schemas.version import VersionInfo
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
            pass  # Keep account_id as None
        except Exception as e:
            # Log unexpected errors but don't fail the request
            logger.error(
                f"Unexpected error during optional auth in /version: {e}", exc_info=True
            )
            pass  # Keep account_id as None

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

    return VersionInfo(
        server_version=SERVER_VERSION,
        min_client_version=MIN_CLIENT_VERSION,
        max_client_version=MAX_CLIENT_VERSION,
        latest_version=SERVER_VERSION,
        min_version=MIN_CLIENT_VERSION,
        download_url="https://preloop.ai/install/cli",
    )
