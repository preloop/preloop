"""Azure DevOps service hook setup: inspection, secret rotation, registration.

Webhook capability is reported separately from connection success: a
personal access token that reads pull requests may lack project subscription
permissions, and a subscription may exist without a usable secret on the Preloop
side. Each of those states is visible here.

Azure DevOps service hooks are unsigned. Preloop generates a per-tracker
secret, stores it encrypted, and configures each subscription to send it in
the ``X-Preloop-Webhook-Secret`` header. The intake rejects deliveries
without it (see ``preloop.api.endpoints.webhooks``).

The handlers are synchronous so FastAPI runs them, and their database work,
on the threadpool. The REST calls run in a private event loop on that worker
thread.
"""

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from fastapi import APIRouter, Depends, HTTPException
from pydantic import UUID4, BaseModel, Field
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.models import models
from preloop.models.crud import crud_tracker
from preloop.models.db.session import get_db_session
from preloop.schemas.auth import AuthUserResponse
from preloop.sync.exceptions import (
    TrackerAuthenticationError,
    TrackerError,
    TrackerPermissionError,
)
from preloop.sync.trackers.azure_devops import AzureDevOpsTracker
from preloop.utils.azure_devops import (
    AZURE_DEVOPS_TRACKER_TYPE,
    AZURE_DEVOPS_WEBHOOK_EVENTS,
    WEBHOOK_SECRET_HEADER,
    generate_webhook_secret,
    webhook_callback_path,
)
from preloop.utils.permissions import ensure_permission_in_oss, require_permission

logger = logging.getLogger(__name__)

router = APIRouter()

SETUP_INSTRUCTIONS = (
    "In Azure DevOps open Project settings, Service hooks, Create subscription, "
    "choose Web Hooks and the event 'Pull request created', filter by the "
    "repository, enter the callback URL and, under HTTP headers, "
    f"'{WEBHOOK_SECRET_HEADER}:<secret>'. Repeat for 'Pull request updated' "
    "with Change set to Source branch updated, and 'Pull request commented on'. "
    "Add another Pull request updated subscription with Change set to Pull request "
    "lifecycle status changed and the additional header X-Preloop-Azure-Lifecycle:true, "
    "and, without a repository filter, for 'Work item created', 'Work item "
    "updated' and 'Work item commented on'. Leave resource details at 'All'. "
    "Manual setup is available even when the token cannot register hooks. "
    "Automatic registration uses Code (Read) for PR events and Work Items (Read) "
    "for work item events, plus the user's project 'Edit subscriptions' and "
    "'View subscriptions' permissions. Service Connections is unrelated. "
    "A passing created/updated PR subscription Test checks the URL and secret, "
    "not repository scope. Verify comment hooks with a real /preloop review comment. "
    "Registering from Preloop also records the token's identity, so changes "
    "Preloop makes to work items do not trigger flows again."
)


class AzureDevOpsWebhookStatus(BaseModel):
    """Service hook readiness of one Azure DevOps tracker."""

    callback_url: Optional[str] = Field(
        None, description="URL the subscriptions call; null when PRELOOP_URL is unset."
    )
    callback_configured: bool
    required_events: List[str]
    header_name: str = Field(..., description="Header carrying the secret.")
    signature: str = Field(
        ..., description="'configured' or 'missing_secret' (Preloop side)."
    )
    registration: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Subscription state when checked: registered, events_missing, "
            "permission_denied, unauthorized or unavailable."
        ),
    )
    instructions: str
    last_ignored_reason: Optional[str] = Field(
        None, description="Most recent scope-related ignored delivery reason."
    )


class AzureDevOpsWebhookSecret(AzureDevOpsWebhookStatus):
    """A newly generated secret. It is shown once and stored encrypted."""

    secret: str


class AzureDevOpsWebhookRegisterRequest(BaseModel):
    """Optional repository override for a tracker not bound to one."""

    repository: Optional[str] = Field(
        None, description="'organization/project/repository'."
    )


def _tracker(
    db: Session, tracker_id: UUID4, current_user: AuthUserResponse
) -> models.Tracker:
    tracker = crud_tracker.get_by_id_and_account(
        db,
        id=str(tracker_id),
        account_id=current_user.account_id,
        include_deleted=False,
    )
    if tracker is None or tracker.tracker_type != AZURE_DEVOPS_TRACKER_TYPE:
        raise HTTPException(status_code=404, detail="Tracker not found")
    return tracker


def _callback_url(tracker: models.Tracker) -> Optional[str]:
    base = os.getenv("PRELOOP_URL")
    if not base:
        return None
    return urljoin(base, f"/api/v1{webhook_callback_path(tracker.id)}")


def _status(
    tracker: models.Tracker, registration: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    url = _callback_url(tracker)
    return {
        "callback_url": url,
        "callback_configured": url is not None,
        "required_events": list(AZURE_DEVOPS_WEBHOOK_EVENTS),
        "header_name": WEBHOOK_SECRET_HEADER,
        "signature": (
            "configured" if tracker.webhook_secret_id is not None else "missing_secret"
        ),
        "registration": registration,
        "instructions": SETUP_INSTRUCTIONS,
        "last_ignored_reason": (
            (getattr(tracker, "meta_data", None) or {}).get("last_ignored_webhook")
            or {}
        ).get("reason"),
    }


def _client(tracker: models.Tracker) -> AzureDevOpsTracker:
    return AzureDevOpsTracker(
        str(tracker.id),
        tracker.resolved_api_key or "",
        dict(tracker.connection_details or {}),
    )


@router.get(
    "/trackers/{tracker_id}/azure-devops/webhook",
    response_model=AzureDevOpsWebhookStatus,
)
@require_permission("view_trackers")
def get_azure_devops_webhook_status(
    tracker_id: UUID4,
    check: bool = False,
    current_user: AuthUserResponse = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> Dict[str, Any]:
    """Report callback, secret and (with ``check=true``) subscription state."""
    tracker = _tracker(db, tracker_id, current_user)
    registration = None
    url = _callback_url(tracker)
    if check and url:
        try:
            registration = asyncio.run(_client(tracker).inspect_service_hooks(url))
        except TrackerPermissionError:
            registration = {"status": "permission_denied", "missing_events": []}
        except TrackerAuthenticationError:
            registration = {"status": "unauthorized", "missing_events": []}
        except TrackerError as exc:
            logger.warning(
                "Service hook inspection failed for tracker %s: %s",
                tracker.id,
                type(exc).__name__,
            )
            registration = {"status": "unavailable", "missing_events": []}
    return _status(tracker, registration)


@router.post(
    "/trackers/{tracker_id}/azure-devops/webhook/secret",
    response_model=AzureDevOpsWebhookSecret,
)
@require_permission("edit_trackers")
def rotate_azure_devops_webhook_secret(
    tracker_id: UUID4,
    current_user: AuthUserResponse = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> Dict[str, Any]:
    """Generate a new per-tracker secret, store it encrypted, return it once.

    Deliveries carrying the previous secret are rejected from now on, so
    update the subscriptions (or call the register endpoint) right after.
    """
    ensure_permission_in_oss(db, current_user, "edit_trackers")
    tracker = _tracker(db, tracker_id, current_user)
    secret = generate_webhook_secret()
    # The tracker CRUD routes this field into an encrypted SecretReference
    # and clears the plaintext column; the name predates other providers.
    crud_tracker.update(db, db_obj=tracker, obj_in={"jira_webhook_secret": secret})
    return {**_status(tracker), "signature": "configured", "secret": secret}


@router.post(
    "/trackers/{tracker_id}/azure-devops/webhook/register",
    response_model=AzureDevOpsWebhookStatus,
)
@require_permission("edit_trackers")
def register_azure_devops_webhook(
    tracker_id: UUID4,
    body: Optional[AzureDevOpsWebhookRegisterRequest] = None,
    current_user: AuthUserResponse = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> Dict[str, Any]:
    """Create or update the subscriptions with the tracker's own token.

    Idempotent: re-running updates Preloop's subscriptions in place and
    never touches other subscriptions. A secret is generated first when the
    tracker has none.
    """
    ensure_permission_in_oss(db, current_user, "edit_trackers")
    tracker = _tracker(db, tracker_id, current_user)
    url = _callback_url(tracker)
    if url is None:
        raise HTTPException(
            status_code=409,
            detail="PRELOOP_URL is not set, so there is no callback URL to register.",
        )
    secret = tracker.resolved_webhook_secret
    if not secret:
        secret = generate_webhook_secret()
        crud_tracker.update(db, db_obj=tracker, obj_in={"jira_webhook_secret": secret})
    repository = body.repository if body else None
    client = _client(tracker)

    async def register() -> tuple[Dict[str, Any], Dict[str, Any]]:
        result = await client.ensure_service_hooks(
            url, secret, repo_full_name=repository
        )
        try:
            context = await client.work_item_context(repository)
        except TrackerError as exc:
            # The subscriptions exist; without the identity the loop guard
            # falls back to bot names, so this is not fatal.
            logger.warning(
                "Work item context unavailable for tracker %s: %s",
                tracker.id,
                type(exc).__name__,
            )
            context = {}
        return result, context

    try:
        result, context = asyncio.run(register())
    except (TrackerPermissionError, TrackerAuthenticationError) as exc:
        logger.info(
            "Service hook registration refused for tracker %s: %s",
            tracker.id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "The stored token cannot manage service hooks. Check its Code "
                "and Work Items read scopes and the user's project View subscriptions "
                "and Edit subscriptions permissions, or use manual setup. "
                + SETUP_INSTRUCTIONS
            ),
        ) from exc
    except TrackerError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Azure DevOps did not accept the request: {exc}",
        ) from exc
    # The intake ignores changes made with this token (no self-trigger) and
    # reads open/closed from the state categories without a network call.
    if context:
        crud_tracker.update(
            db,
            db_obj=tracker,
            obj_in={
                "connection_details": {
                    **dict(tracker.connection_details or {}),
                    **context,
                }
            },
        )
    return _status(
        tracker,
        {
            "status": "registered",
            "ids": result.get("ids"),
            "created": result.get("created"),
            "missing_events": [],
        },
    )
