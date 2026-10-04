"""Bitbucket Data Center webhook setup: inspection, secret rotation, registration.

Webhook capability is reported separately from connection success. A
personal access token that reads pull requests may still lack the
repository admin right needed to manage hooks, and a hook may exist without
a usable secret on the Preloop side. Each of those states is visible here.

Registration through the API uses only the credentials already stored on
the tracker. It never requests broader OAuth scopes; trackers that do not
hold a personal access token get administrator instructions instead.

The handlers are synchronous so FastAPI runs them, and their database work,
on the threadpool. The few REST calls to the instance run in a private event
loop on that worker thread.
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
    TrackerPermissionError,
    TrackerResponseError,
)
from preloop.sync.trackers.factory import create_tracker_client
from preloop.utils.bitbucket_dc import (
    BITBUCKET_DC_AUTH_API_TOKEN,
    BITBUCKET_DC_TRACKER_TYPE,
    BitbucketDCConfigError,
    bitbucket_dc_enabled,
)
from preloop.utils.bitbucket_dc_webhooks import (
    BITBUCKET_DC_WEBHOOK_EVENTS,
    generate_webhook_secret,
    webhook_callback_path,
)
from preloop.utils.permissions import require_permission

logger = logging.getLogger(__name__)

router = APIRouter()

SETUP_INSTRUCTIONS = (
    "A repository administrator opens Repository settings, Webhooks, Create "
    "webhook on the Bitbucket Data Center instance, enters the callback URL "
    "and the secret, selects every required event and saves. The instance "
    "must be able to reach the callback URL over HTTPS. Use Test connection "
    "on the webhook page, then check delivery status here."
)


class BitbucketDCWebhookStatus(BaseModel):
    """Webhook readiness of one Data Center tracker."""

    callback_url: Optional[str] = Field(
        None, description="URL the hook must call; null when PRELOOP_URL is unset."
    )
    callback_configured: bool
    required_events: List[str]
    signature: str = Field(
        ..., description="'configured' or 'missing_secret' (Preloop side)."
    )
    registration: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Hook state on the bound repository when checked: registered, "
            "inactive, events_missing, missing, permission_denied, unauthorized."
        ),
    )
    instructions: str


class BitbucketDCWebhookSecret(BitbucketDCWebhookStatus):
    """A newly generated secret. It is shown once and stored encrypted."""

    secret: str


class BitbucketDCWebhookRegisterRequest(BaseModel):
    """Optional repository override for an unbound tracker."""

    repository: Optional[str] = Field(
        None, description="'PROJECT/slug'; defaults to the bound repository."
    )


def _tracker(
    db: Session, tracker_id: UUID4, current_user: AuthUserResponse
) -> models.Tracker:
    if not bitbucket_dc_enabled():
        raise HTTPException(status_code=404, detail="Tracker not found")
    tracker = crud_tracker.get_by_id_and_account(
        db,
        id=str(tracker_id),
        account_id=current_user.account_id,
        include_deleted=False,
    )
    if tracker is None or tracker.tracker_type != BITBUCKET_DC_TRACKER_TYPE:
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
        "required_events": list(BITBUCKET_DC_WEBHOOK_EVENTS),
        "signature": (
            "configured" if tracker.webhook_secret_id is not None else "missing_secret"
        ),
        "registration": registration,
        "instructions": SETUP_INSTRUCTIONS,
    }


def _client(tracker: models.Tracker) -> Any:
    try:
        return asyncio.run(
            create_tracker_client(
                tracker_type=BITBUCKET_DC_TRACKER_TYPE,
                tracker_id=str(tracker.id),
                api_key=tracker.resolved_api_key,
                connection_details={
                    "url": tracker.url,
                    **(tracker.connection_details or {}),
                    "auth_type": tracker.auth_type,
                },
            )
        )
    except BitbucketDCConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/trackers/{tracker_id}/bitbucket-dc/webhook",
    response_model=BitbucketDCWebhookStatus,
)
@require_permission("view_trackers")
def get_bitbucket_dc_webhook_status(
    tracker_id: UUID4,
    check: bool = False,
    current_user: AuthUserResponse = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> Dict[str, Any]:
    """Report callback, secret and (with ``check=true``) hook registration."""
    tracker = _tracker(db, tracker_id, current_user)
    registration = None
    url = _callback_url(tracker)
    if check and url and tracker.auth_type == BITBUCKET_DC_AUTH_API_TOKEN:
        client = _client(tracker)
        if client is None or not getattr(client, "repo_full_name", None):
            registration = {"status": "unbound_repository", "missing_events": []}
        else:
            try:
                registration = asyncio.run(client.inspect_repository_webhook(url))
            except TrackerResponseError as exc:
                logger.warning(
                    "Webhook inspection failed for tracker %s: %s",
                    tracker.id,
                    type(exc).__name__,
                )
                registration = {"status": "unavailable", "missing_events": []}
    return _status(tracker, registration)


@router.post(
    "/trackers/{tracker_id}/bitbucket-dc/webhook/secret",
    response_model=BitbucketDCWebhookSecret,
)
@require_permission("edit_trackers")
def rotate_bitbucket_dc_webhook_secret(
    tracker_id: UUID4,
    current_user: AuthUserResponse = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> Dict[str, Any]:
    """Generate a new per-tracker secret, store it encrypted, return it once.

    Deliveries signed with the previous secret are rejected from now on, so
    update the hook (or call the register endpoint) right after rotating.
    """
    tracker = _tracker(db, tracker_id, current_user)
    secret = generate_webhook_secret()
    # The tracker CRUD routes this field into an encrypted SecretReference
    # and clears the plaintext column; the name predates other providers.
    crud_tracker.update(db, db_obj=tracker, obj_in={"jira_webhook_secret": secret})
    return {**_status(tracker), "signature": "configured", "secret": secret}


@router.post(
    "/trackers/{tracker_id}/bitbucket-dc/webhook/register",
    response_model=BitbucketDCWebhookStatus,
)
@require_permission("edit_trackers")
def register_bitbucket_dc_webhook(
    tracker_id: UUID4,
    body: Optional[BitbucketDCWebhookRegisterRequest] = None,
    current_user: AuthUserResponse = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> Dict[str, Any]:
    """Create or update the hook with the tracker's own credentials.

    Idempotent: re-running updates Preloop's hook in place and never
    deletes or edits other hooks on the repository.
    """
    tracker = _tracker(db, tracker_id, current_user)
    url = _callback_url(tracker)
    if url is None:
        raise HTTPException(
            status_code=409,
            detail="PRELOOP_URL is not set, so there is no callback URL to register.",
        )
    if tracker.auth_type != BITBUCKET_DC_AUTH_API_TOKEN:
        raise HTTPException(
            status_code=409,
            detail=(
                "This tracker has no personal access token with repository admin "
                "rights. Ask a repository administrator to add the webhook. "
                + SETUP_INSTRUCTIONS
            ),
        )
    secret = tracker.resolved_webhook_secret
    if not secret:
        raise HTTPException(
            status_code=409,
            detail="Generate a webhook secret before registering the webhook.",
        )
    client = _client(tracker)
    repository = body.repository if body else None
    try:
        result = asyncio.run(
            client.ensure_repository_webhook(url, secret, repo_full_name=repository)
        )
    except (TrackerPermissionError, TrackerAuthenticationError) as exc:
        logger.info(
            "Webhook registration refused for tracker %s: %s",
            tracker.id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "The stored token cannot manage webhooks on this repository "
                "(repository admin is required). Ask a repository administrator "
                "to add the webhook. " + SETUP_INSTRUCTIONS
            ),
        ) from exc
    except TrackerResponseError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _status(
        tracker,
        {
            "status": "registered",
            "id": result.get("id"),
            "created": result.get("created"),
            "missing_events": [],
        },
    )
