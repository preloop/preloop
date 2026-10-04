import hashlib
import hmac
import logging
import json
import uuid
from dataclasses import dataclass, field
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from preloop.api.loop_safety import run_db_off_loop
from preloop.models import models
from preloop.models.crud import (
    crud_comment,
    crud_issue,
    crud_organization,
    crud_project,
    crud_tracker,
)
from preloop.models.db.session import get_db_session, _safe_close_db_session
from preloop.models.crud.project import find_repository_projects, repository_host
from preloop.services.issue_intake import IssuePayloadError, issue_values_from_payload
from preloop.sync.scanner.core import TrackerClient

from preloop.sync.services.event_bus import EventBus, get_task_publisher

from preloop.utils.bitbucket import (
    BITBUCKET_DELIVERY_HEADER,
    BITBUCKET_EVENT_HEADER,
    BITBUCKET_SIGNATURE_HEADER,
    BITBUCKET_WEBHOOK_EVENTS,
    verify_signature as verify_bitbucket_signature,
)
from preloop.utils.bitbucket_dc import (
    BITBUCKET_DC_TRACKER_TYPE,
    BitbucketDCConfigError,
    approved_instance_for,
    bitbucket_dc_enabled,
    validate_repository_id,
)
from preloop.utils.bitbucket_dc_webhooks import (
    BITBUCKET_DC_DELIVERY_HEADER,
    BITBUCKET_DC_EVENT_HEADER,
    BITBUCKET_DC_SIGNATURE_HEADER,
    BITBUCKET_DC_WEBHOOK_MAX_BYTES,
    BitbucketDCWebhookRejectedError,
    delivery_identity as bitbucket_dc_delivery_identity,
    normalize_delivery as normalize_bitbucket_dc_delivery,
    verify_webhook_signature as verify_bitbucket_dc_signature,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Default subscribed events if not configured on the tracker
DEFAULT_GITHUB_SUBSCRIBED_EVENTS = [
    "push",
    "issues",
    "issue_comment",
    "pull_request",
    "release",
    "deployment_status",
    "check_run",
    "check_suite",
    "workflow_run",
    "pull_request_review",
    "pull_request_review_comment",
    "status",
]
DEFAULT_GITLAB_SUBSCRIBED_EVENTS = [
    "Push Hook",
    "Tag Push Hook",
    "Issue Hook",
    "Note Hook",
    "Merge Request Hook",
    "Pipeline Hook",
    "Job Hook",
    "Deployment Hook",
    "Release Hook",
]
DEFAULT_BITBUCKET_SUBSCRIBED_EVENTS = list(BITBUCKET_WEBHOOK_EVENTS)
DEFAULT_JIRA_SUBSCRIBED_EVENTS = [
    "jira:issue_created",
    "jira:issue_updated",
    "jira:issue_deleted",
    "comment_created",
    "comment_updated",
    "comment_deleted",
    "worklog_created",
    "worklog_updated",
    "worklog_deleted",
]


@dataclass
class _WebhookPlan:
    """Plain payloads and IDs crossing from DB preparation to async delivery."""

    tasks: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = field(
        default_factory=list
    )
    embeddings: list[dict[str, Any]] = field(default_factory=list)
    tracker_id: Any = None
    organization_id: Any = None
    db_processed: Optional[bool] = None

    def queue_task(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.tasks.append((name, args, kwargs))


@router.post("/private/webhooks/{tracker_type}/{organization_id}", response_model=None)
async def receive_webhook(
    tracker_type: str,
    organization_id: str,
    request: Request,
    db: Session = Depends(get_db_session),
    task_publisher: EventBus = Depends(get_task_publisher),
) -> dict[str, Any]:
    """Receive tracker webhooks and queue event processing.

    Return HTTP 503 when task publication is not acknowledged.

    Bitbucket Data Center deliveries use ``bitbucket_dc`` as ``tracker_type``
    and the tracker id (not an organization id) as the second path segment:
    the hook belongs to one tracker and its per-tracker secret.
    """
    if tracker_type.lower() == BITBUCKET_DC_TRACKER_TYPE:
        raw_body = await _read_limited_body(request, BITBUCKET_DC_WEBHOOK_MAX_BYTES)
    else:
        raw_body = await request.body()
    headers = request.headers
    plan = _WebhookPlan()
    result: dict[str, Any] = {}
    preparation_error: Optional[Exception] = None

    def prepare() -> dict[str, Any]:
        try:
            if tracker_type.lower() == BITBUCKET_DC_TRACKER_TYPE:
                return _prepare_bitbucket_dc_webhook(
                    organization_id, raw_body, headers, db, plan
                )
            return _prepare_webhook(
                tracker_type, organization_id, raw_body, headers, db, plan
            )
        finally:
            _safe_close_db_session(db)

    try:
        result = await run_db_off_loop(prepare)
    except Exception as exc:
        # Notifications queued before a validation/DB failure must still be sent.
        preparation_error = exc

    publish_failed = False
    for name, args, kwargs in plan.tasks:
        try:
            ack = await task_publisher.publish_task(name, *args, **kwargs)
            if ack is None:
                raise RuntimeError("NATS did not acknowledge the webhook task")
        except Exception:
            logger.exception("Webhook task publication failed: %s", name)
            if name != "notify_admins":
                publish_failed = True

    if preparation_error is not None:
        raise preparation_error
    if publish_failed:
        # Returning 200 would stop tracker redelivery even though no task was
        # acknowledged. Inline issue changes are idempotent on a delivery retry.
        raise HTTPException(
            503, "Webhook task publication unavailable", headers={"Retry-After": "5"}
        )

    if plan.db_processed:

        def update_timestamp() -> None:
            try:
                crud_organization.touch_webhook(
                    db,
                    organization_id=plan.organization_id,
                    observed_at=datetime.now(timezone.utc),
                )
            finally:
                _safe_close_db_session(db)

        try:
            await run_db_off_loop(update_timestamp)
        except Exception:
            logger.exception(
                "Webhook timestamp update failed for tracker %s", plan.tracker_id
            )
        return {
            "status": "success",
            "message": "Webhook processed and task published",
            "tracker_id": plan.tracker_id,
        }
    if plan.db_processed is False:
        return {
            "status": "partial_success",
            "message": "Webhook event published but database processing failed",
            "tracker_id": plan.tracker_id,
            "nats_published": True,
            "db_processed": False,
        }
    return result


async def _read_limited_body(request: Request, limit: int) -> bytes:
    """Read the request body, refusing anything larger than ``limit`` bytes.

    The declared length is checked first and the stream is cut off as soon
    as it passes the limit, so an oversized body is never held in memory.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            too_large = int(declared) > limit
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length")
        if too_large:
            raise HTTPException(status_code=413, detail="Payload too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail="Payload too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _prepare_bitbucket_dc_webhook(
    tracker_id: str,
    raw_body: bytes,
    headers: Mapping[str, str],
    db: Session,
    plan: _WebhookPlan,
) -> dict[str, Any]:
    """Authenticate, validate and normalize one Bitbucket Data Center delivery.

    Order matters: the HMAC over the raw bytes is checked before the body is
    parsed, and the payload's repository is checked against the tracker's
    immutable binding before anything is dispatched. Logs carry the tracker
    id and a fixed reason code, never the secret, signature or payload.
    """
    if not bitbucket_dc_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    try:
        tracker_uuid = uuid.UUID(str(tracker_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")
    tracker = crud_tracker.get(db, id=tracker_uuid)
    if (
        tracker is None
        or tracker.tracker_type != BITBUCKET_DC_TRACKER_TYPE
        or getattr(tracker, "is_deleted", False) is True
    ):
        raise HTTPException(status_code=404, detail="Not found")
    plan.tracker_id = tracker.id

    secret = tracker.resolved_webhook_secret
    if not secret:
        logger.warning("Bitbucket DC webhook for tracker %s has no secret", tracker.id)
        raise HTTPException(status_code=403, detail="Webhook not configured")
    signature = headers.get(BITBUCKET_DC_SIGNATURE_HEADER)
    if not signature:
        logger.warning("Bitbucket DC webhook for tracker %s is unsigned", tracker.id)
        raise HTTPException(status_code=403, detail="Missing Bitbucket signature")
    if not verify_bitbucket_dc_signature(secret, raw_body, signature):
        logger.warning(
            "Bitbucket DC webhook signature mismatch for tracker %s", tracker.id
        )
        raise HTTPException(status_code=403, detail="Invalid Bitbucket signature")

    event_key = headers.get(BITBUCKET_DC_EVENT_HEADER) or ""
    if not tracker.is_active:
        return {"status": "ignored", "reason": "tracker_inactive"}

    details = tracker.connection_details or {}
    try:
        instance = approved_instance_for(details.get("instance_url") or tracker.url)
        bound_repository_id = (
            validate_repository_id(details["repository_id"])
            if details.get("repository_id") not in (None, "")
            else None
        )
    except BitbucketDCConfigError:
        logger.warning(
            "Bitbucket DC webhook for tracker %s: instance no longer approved",
            tracker.id,
        )
        raise HTTPException(status_code=403, detail="instance_not_approved")

    try:
        payload = json.loads(raw_body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    projects: dict[int, Optional[models.Project]] = {}

    def project_for(repository_id: int) -> Optional[models.Project]:
        if repository_id not in projects:
            projects[repository_id] = crud_project.get_for_tracker_by_identifier(
                db, tracker_id=tracker.id, identifier=str(repository_id)
            )
        return projects[repository_id]

    try:
        delivery = normalize_bitbucket_dc_delivery(
            event_key,
            payload,
            instance=instance,
            bound_repository_id=bound_repository_id,
            bound_project_key=details.get("project_key") or None,
            repository_known=lambda repo_id: project_for(repo_id) is not None,
            self_user_slug=details.get("username") or None,
        )
    except BitbucketDCWebhookRejectedError as exc:
        logger.warning(
            "Bitbucket DC webhook rejected for tracker %s: %s", tracker.id, exc.reason
        )
        raise HTTPException(status_code=exc.status_code, detail=exc.reason)

    if not delivery.accepted:
        logger.info(
            "Bitbucket DC webhook %s acknowledged without dispatch for tracker %s: %s",
            delivery.event_key,
            tracker.id,
            delivery.reason,
        )
        return {
            "status": "ignored",
            "reason": delivery.reason,
            "tracker_id": tracker.id,
        }
    subscribed = tracker.subscribed_events or []
    if subscribed and delivery.event_key not in subscribed:
        return {
            "status": "ignored",
            "reason": "not_subscribed",
            "tracker_id": tracker.id,
        }

    project = (
        project_for(delivery.repository_id)
        if delivery.repository_id is not None
        else None
    )
    if project is not None:
        plan.organization_id = project.organization_id
        plan.db_processed = True
    plan.queue_task(
        "process_webhook_event",
        tracker_type=BITBUCKET_DC_TRACKER_TYPE,
        event_type=delivery.event_key,
        delivery_id=bitbucket_dc_delivery_identity(
            tracker.id,
            headers.get(BITBUCKET_DC_DELIVERY_HEADER),
            delivery.event_key,
            delivery.payload,
            raw_body,
        ),
        payload=delivery.payload,
        tracker_id=str(tracker.id),
        organization_id=str(plan.organization_id) if plan.organization_id else None,
        embedding_requests=[],
    )
    return {"status": "success", "tracker_id": tracker.id}


def _resolve_webhook_project(
    db: Session,
    *,
    identifier: str,
    organization_id: Any,
    tracker: Any,
) -> Optional[models.Project]:
    """Find the project a webhook names, within the delivering tracker's reach.

    Prefer the organization the webhook was delivered for. Otherwise fall back
    to the same repository on the same tracker type and host elsewhere in the
    tracker's account, so a repository that moved owners keeps routing to its
    project until it is transferred (#1159). Never look outside the account,
    and never match an identifier from another tracker type or host: the same
    number names unrelated repositories there.
    """
    if organization_id is not None:
        project = crud_project.get_by_identifier(
            db,
            identifier=identifier,
            organization_id=str(organization_id),
            account_id=str(tracker.account_id),
        )
        if project is not None:
            return project
    matches = find_repository_projects(
        db,
        identifier=identifier,
        account_id=tracker.account_id,
        tracker_type=tracker.tracker_type,
        host=repository_host(tracker),
    )
    return matches[0] if matches else None


def _prepare_webhook(
    tracker_type: str,
    organization_id: str,
    raw_body: bytes,
    headers: Mapping[str, str],
    db: Session,
    plan: _WebhookPlan,
) -> dict[str, Any]:
    logger.info(
        f"Received webhook for tracker_type={tracker_type}, organization_id={organization_id}"
    )

    # --- 1. Read Raw Body ---
    logger.info(f"Raw webhook body length: {len(raw_body)}")

    # --- 2. Resolve Tracker, Secret, and context for timestamp update ---
    resolved_tracker: Optional[models.Tracker] = None
    webhook_secret_to_use: Optional[str] = None
    default_event_list_for_type: List[str] = []
    event_type_header_key: Optional[str] = None

    if tracker_type.lower() == "github":
        default_event_list_for_type = DEFAULT_GITHUB_SUBSCRIBED_EVENTS
        event_type_header_key = "X-GitHub-Event"
    elif tracker_type.lower() == "gitlab":
        default_event_list_for_type = DEFAULT_GITLAB_SUBSCRIBED_EVENTS
        event_type_header_key = "X-Gitlab-Event"
    elif tracker_type.lower() == "jira":
        default_event_list_for_type = DEFAULT_JIRA_SUBSCRIBED_EVENTS
    elif tracker_type.lower() == "bitbucket":
        default_event_list_for_type = DEFAULT_BITBUCKET_SUBSCRIBED_EVENTS
        event_type_header_key = BITBUCKET_EVENT_HEADER
    else:
        logger.error(f"Unsupported tracker_type for webhook: {tracker_type}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported tracker_type: {tracker_type}",
        )
    # Use CRUD layer to get organization with tracker
    organization_data = crud_organization.get_with_tracker(db, id=organization_id)
    if not organization_data:
        logger.warning(
            f"Organization not found for id={organization_id}, tracker_type={tracker_type}"
        )
        # Notify admins
        plan.queue_task(
            "notify_admins",
            subject="Organization not found for webhook",
            message=f"Organization not found for id={organization_id}, tracker_type={tracker_type}",
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found"
        )

    if not organization_data.tracker:
        logger.error(f"Tracker not found for organization ID {organization_data.id}")
        # Notify admins
        plan.queue_task(
            "notify_admins",
            subject="Tracker not found for organization ID",
            message=f"Tracker not found for organization ID {organization_data.id}",
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Tracker configuration error",
        )

    if not organization_data.webhook_secret:  # For GH/GL, secret is on Org model
        logger.error(
            f"Webhook secret not configured for organization ID {organization_data.id}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Webhook not configured correctly",
        )

    resolved_tracker = organization_data.tracker
    plan.tracker_id = resolved_tracker.id
    plan.organization_id = organization_data.id
    webhook_secret_to_use = organization_data.webhook_secret

    if not resolved_tracker:
        logger.error(
            f"Failed to resolve tracker for {tracker_type} with organization_id {organization_id}"
        )
        # Notify admins
        plan.queue_task(
            "notify_admins",
            subject="Failed to resolve tracker",
            message=f"Failed to resolve tracker for {tracker_type} with organization_id {organization_id}",
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error resolving tracker.",
        )

    if not resolved_tracker.is_active:
        logger.warning(
            f"Webhook received for inactive tracker ID {resolved_tracker.id} ({tracker_type}). Ignoring."
        )
        # Return 200 OK to not cause retries from Jira/GitHub/GitLab for an intentionally inactive tracker
        return {"status": "ignored", "message": "Tracker is inactive"}

    if webhook_secret_to_use is None:  # Should also be caught
        logger.error(f"Webhook secret is not set for tracker ID {resolved_tracker.id}")
        # Notify admins
        plan.queue_task(
            "notify_admins",
            subject="Webhook secret is not set for tracker",
            message=f"Webhook secret is not set for tracker ID {resolved_tracker.id}",
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook secret configuration error.",
        )

    # --- 3. Verify Signature ---
    encoded_secret = webhook_secret_to_use.encode("utf-8")

    if tracker_type.lower() == "github":
        signature_header = headers.get("X-Hub-Signature-256")
        if not signature_header:
            logger.warning("Missing X-Hub-Signature-256 header for GitHub webhook")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Missing GitHub signature"
            )
        try:
            method, signature_hash = signature_header.split("=", 1)
            if method.lower() != "sha256":
                logger.warning(f"Unsupported GitHub signature method: {method}")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Unsupported GitHub signature method",
                )

            expected_signature = hmac.new(
                encoded_secret, raw_body, hashlib.sha256
            ).hexdigest()

            if not hmac.compare_digest(signature_hash, expected_signature):
                logger.warning(
                    f"GitHub webhook signature mismatch for tracker ID {resolved_tracker.id}"
                )
                # Notify admins
                plan.queue_task(
                    "notify_admins",
                    subject="GitHub webhook signature mismatch",
                    message=f"GitHub webhook signature mismatch for tracker ID {resolved_tracker.id}",
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Invalid GitHub signature",
                )
            logger.info(
                f"GitHub webhook signature verified successfully for tracker ID {resolved_tracker.id}"
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error during GitHub signature verification: {e}")
            # Notify admins
            plan.queue_task(
                "notify_admins",
                subject="Error during GitHub signature verification",
                message=f"Error during GitHub signature verification for tracker ID {resolved_tracker.id}: {e}",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="GitHub signature verification failed",
            )

    elif tracker_type.lower() == "gitlab":
        token_header = headers.get("X-Gitlab-Token")
        if not token_header:
            logger.warning("Missing X-Gitlab-Token header for GitLab webhook")
            # Notify admins
            plan.queue_task(
                "notify_admins",
                subject="Missing X-Gitlab-Token header for GitLab webhook",
                message=f"Missing X-Gitlab-Token header for GitLab webhook, tracker ID {resolved_tracker.id}",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Missing GitLab token"
            )

        # Use compare_digest for timing attack resistance
        if not hmac.compare_digest(
            token_header.encode("utf-8"), encoded_secret
        ):  # Use encoded_secret
            logger.warning(
                f"GitLab webhook token mismatch for tracker ID {resolved_tracker.id}"
            )
            # Notify admins
            plan.queue_task(
                "notify_admins",
                subject="GitLab webhook token mismatch",
                message=f"GitLab webhook token mismatch for tracker ID {resolved_tracker.id}",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Invalid GitLab token"
            )
        logger.info(
            f"GitLab webhook token verified successfully for tracker ID {resolved_tracker.id}"
        )

    elif tracker_type.lower() == "jira":
        # Jira Cloud webhooks use HMAC-SHA256 signature with a pre-configured secret.
        # The signature is in the 'X-Hub-Signature' header, format: 'sha256=<signature>'
        signature_header = headers.get("X-Hub-Signature")
        if not signature_header:
            logger.warning(
                f"Missing X-Hub-Signature header for Jira webhook, tracker ID {resolved_tracker.id}"
            )
            # Notify admins
            plan.queue_task(
                "notify_admins",
                subject="Missing X-Hub-Signature header for Jira webhook",
                message=f"Missing X-Hub-Signature header for Jira webhook, tracker ID {resolved_tracker.id}",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Missing Jira signature"
            )
        else:
            try:
                method, signature_hash = signature_header.split("=", 1)
                if method.lower() != "sha256":
                    logger.warning(
                        f"Unsupported Jira signature method: {method} for tracker ID {resolved_tracker.id}"
                    )
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Unsupported Jira signature method",
                    )

                expected_signature = hmac.new(
                    encoded_secret, raw_body, hashlib.sha256
                ).hexdigest()

                if not hmac.compare_digest(signature_hash, expected_signature):
                    logger.warning(
                        f"Jira webhook signature mismatch for tracker ID {resolved_tracker.id}"
                    )
                    # Notify admins
                    plan.queue_task(
                        "notify_admins",
                        subject="Jira webhook signature mismatch",
                        message=f"Jira webhook signature mismatch for tracker ID {resolved_tracker.id}",
                    )
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Invalid Jira signature",
                    )
                logger.info(
                    f"Jira webhook signature verified successfully for tracker ID {resolved_tracker.id}"
                )
            except HTTPException:
                raise
            except Exception as e:
                logger.error(
                    f"Error during Jira signature verification for tracker ID {resolved_tracker.id}: {e}"
                )
                # Notify admins
                plan.queue_task(
                    "notify_admins",
                    subject="Error during Jira signature verification",
                    message=f"Error during Jira signature verification for tracker ID {resolved_tracker.id}: {e}",
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Jira signature verification failed",
                )

    elif tracker_type.lower() == "bitbucket":
        # Bitbucket Cloud signs the raw body with HMAC-SHA256 and sends
        # 'X-Hub-Signature: sha256=<hex>'. Hooks are always created with a
        # secret, so a missing header is rejected like a mismatch.
        signature_header = headers.get(BITBUCKET_SIGNATURE_HEADER)
        if not signature_header:
            logger.warning(
                f"Missing X-Hub-Signature header for Bitbucket webhook, tracker ID {resolved_tracker.id}"
            )
            plan.queue_task(
                "notify_admins",
                subject="Missing X-Hub-Signature header for Bitbucket webhook",
                message=f"Missing X-Hub-Signature header for Bitbucket webhook, tracker ID {resolved_tracker.id}",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Missing Bitbucket signature",
            )
        if not verify_bitbucket_signature(
            webhook_secret_to_use, raw_body, signature_header
        ):
            logger.warning(
                f"Bitbucket webhook signature mismatch for tracker ID {resolved_tracker.id}"
            )
            plan.queue_task(
                "notify_admins",
                subject="Bitbucket webhook signature mismatch",
                message=f"Bitbucket webhook signature mismatch for tracker ID {resolved_tracker.id}",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid Bitbucket signature",
            )
        logger.info(
            f"Bitbucket webhook signature verified successfully for tracker ID {resolved_tracker.id}"
        )

    # --- 4. Parse Payload & Determine Event Type ---
    actual_event_type: Optional[str] = None
    parsed_payload: Dict[str, Any]

    try:
        parsed_payload = json.loads(raw_body)
    except Exception as e:
        logger.error(
            f"Failed to parse webhook JSON payload for tracker ID {resolved_tracker.id}: {e}"
        )
        # Notify admins
        plan.queue_task(
            "notify_admins",
            subject="Failed to parse webhook JSON payload",
            message=f"Failed to parse webhook JSON payload for tracker ID {resolved_tracker.id}: {e}",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload"
        )

    if tracker_type.lower() in ["github", "gitlab", "bitbucket"]:
        if not event_type_header_key:  # Should be set during tracker resolution
            logger.error(
                f"Internal error: event_type_header_key not set for {tracker_type}"
            )
            # Notify admins
            plan.queue_task(
                "notify_admins",
                subject="Internal error: event_type_header_key not set for tracker",
                message=f"Internal error: event_type_header_key not set for tracker {tracker_type}",
            )
            raise HTTPException(
                status_code=500, detail="Internal configuration error for event type."
            )
        actual_event_type = headers.get(event_type_header_key)
        if not actual_event_type:
            logger.warning(
                f"Could not determine event type from header '{event_type_header_key}' for {tracker_type}, tracker ID {resolved_tracker.id}."
            )
    elif tracker_type.lower() == "jira":
        actual_event_type = parsed_payload.get("webhookEvent")
        if not actual_event_type:
            logger.warning(
                f"Jira webhook missing 'webhookEvent' in payload for tracker ID {resolved_tracker.id}."
            )
            # If no event type, it cannot match a specific subscription.
            # If default is to allow all, this might be an issue. For now, treat as unmatchable if specific subs exist.
            # Consider raising 400 if event type is strictly required.
            # For now, if it's None, it won't match any specific event in subscribed_events.

    # --- 5. Check Subscription ---
    subscribed_events_for_tracker = resolved_tracker.subscribed_events

    # Determine effective list of subscribed events
    if (
        subscribed_events_for_tracker is None or not subscribed_events_for_tracker
    ):  # Catches None or empty list
        effective_subscribed_events = default_event_list_for_type
        logger.debug(
            f"Tracker ID {resolved_tracker.id} has no specific subscriptions, using defaults for {tracker_type}: {default_event_list_for_type}"
        )
    else:
        effective_subscribed_events = subscribed_events_for_tracker
        logger.debug(
            f"Tracker ID {resolved_tracker.id} subscribed to: {effective_subscribed_events}"
        )

    if not actual_event_type or actual_event_type not in effective_subscribed_events:
        # Handles cases where actual_event_type is None (e.g. missing Jira 'webhookEvent' or GH/GL header)
        # or if the event is simply not in the list.
        log_msg_event_type = (
            actual_event_type if actual_event_type else "Unknown/Missing"
        )
        logger.info(
            f"Event '{log_msg_event_type}' for tracker ID {resolved_tracker.id} ({tracker_type}) is not in the subscribed list ({effective_subscribed_events}). Skipping NATS publish."
        )
        # Still update timestamp as the webhook was validly received and authenticated (if applicable)
        try:
            crud_organization.touch_webhook(
                db,
                organization_id=plan.organization_id,
                observed_at=datetime.now(timezone.utc),
            )
        except Exception as e_ts:
            db.rollback()
            logger.error(
                f"Failed to update timestamp after skipping non-subscribed event for tracker {resolved_tracker.id}: {e_ts}"
            )
            # Notify admins
            plan.queue_task(
                "notify_admins",
                subject="Failed to update timestamp after skipping non-subscribed event",
                message=f"Failed to update timestamp after skipping non-subscribed event for tracker {resolved_tracker.id}: {e_ts}",
            )
        return {
            "status": "success",
            "message": "Event not subscribed",
            "tracker_id": resolved_tracker.id,
        }

    logger.info(
        f"Event '{actual_event_type}' for tracker ID {resolved_tracker.id} ({tracker_type}) IS SUBSCRIBED. Proceeding."
    )

    # --- 6. Process Payload and Update Database ---
    try:
        if actual_event_type in [
            "Issue Hook",
            "issues",
            "jira:issue_created",
            "jira:issue_updated",
        ]:
            project_data = parsed_payload.get("project") or parsed_payload.get(
                "repository"
            )
            if not project_data and tracker_type.lower() != "jira":
                raise HTTPException(
                    status_code=400, detail="Project data missing from payload"
                )

            project_identifier = None
            if tracker_type.lower() == "gitlab":
                project_identifier = str(project_data["id"])
            elif tracker_type.lower() == "github":
                project_identifier = str(project_data["id"])
            elif tracker_type.lower() == "jira":
                project_identifier = (
                    parsed_payload.get("issue", {})
                    .get("fields", {})
                    .get("project", {})
                    .get("key")
                )

            if not project_identifier:
                # Notify admins
                plan.queue_task(
                    "notify_admins",
                    subject="Could not determine project identifier from payload",
                    message=f"Could not determine project identifier from payload for tracker {resolved_tracker.id}.",
                )
                raise HTTPException(
                    status_code=400,
                    detail="Could not determine project identifier from payload",
                )

            project = _resolve_webhook_project(
                db,
                identifier=project_identifier,
                organization_id=plan.organization_id,
                tracker=resolved_tracker,
            )
            if not project:
                # The webhook names a project we never imported. Usually the
                # repo is outside the integration's scope (GitHub App installed
                # on selected repositories only, or a TrackerScopeRule excludes
                # it), in which case a sync can never resolve it -- so retry
                # with backoff and eventually mark it degraded rather than
                # re-scanning on every event (#tracker-sync-loop).
                state = crud_tracker.record_unknown_project(
                    db,
                    id=str(resolved_tracker.id),
                    project_identifier=project_identifier,
                )
                context = (
                    f"project={project_identifier} tracker={resolved_tracker.id} "
                    f"tracker_type={tracker_type} account={resolved_tracker.account_id} "
                    f"attempts={state.attempts}"
                )
                if state.degraded:
                    logger.warning(
                        f"Unknown project on webhook; tracker marked degraded, not syncing. {context}. "
                        "Reason: the project is not visible to this integration after repeated syncs "
                        "(check that the repository is in the integration's selected repositories and "
                        "not excluded by a scope rule)."
                    )
                    return {
                        "status": "accepted",
                        "message": "Project not found; tracker needs attention.",
                        "degraded": True,
                    }
                if not state.should_sync:
                    logger.info(
                        f"Unknown project on webhook; sync suppressed by backoff "
                        f"(retry_after={state.retry_after_seconds}s). {context}."
                    )
                    return {
                        "status": "accepted",
                        "message": "Project not found; sync backoff in effect.",
                    }
                logger.warning(
                    f"Unknown project on webhook; triggering sync. {context}. "
                    "Reason: no project row matches the webhook's project identifier."
                )
                plan.queue_task(
                    "poll_tracker",
                    tracker_id=resolved_tracker.id,
                    force_update=True,
                )
                return {
                    "status": "accepted",
                    "message": "Project not found, sync triggered.",
                }

            issue_data = parsed_payload.get("object_attributes") or parsed_payload.get(
                "issue"
            )
            if not issue_data:
                raise HTTPException(
                    status_code=400, detail="Issue data missing from payload"
                )

            try:
                transformed_issue = issue_values_from_payload(
                    resolved_tracker, project, issue_data, tracker_type=tracker_type
                )
            except IssuePayloadError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            # The published event payload carries the constructed key.
            if transformed_issue.get("key"):
                issue_data.setdefault("key", transformed_issue["key"])

            # Atomic upsert: concurrent deliveries for one issue (opened plus
            # several labeled events within a second) converge on one row.
            db_issue, created = crud_issue.upsert(db, obj_in=transformed_issue)
            logger.info(
                "%s issue %s from webhook (title: '%s')",
                "Created" if created else "Updated",
                db_issue.id,
                db_issue.title,
            )

            plan.embeddings.append({"issue_id": db_issue.id, "force_update": True})

        elif actual_event_type in [
            "Note Hook",
            "issue_comment",
            "comment_created",
            "comment_updated",
        ]:
            project_data = parsed_payload.get("project") or parsed_payload.get(
                "repository"
            )
            if not project_data and tracker_type.lower() != "jira":
                raise HTTPException(
                    status_code=400, detail="Project data missing from payload"
                )

            project_identifier = None
            if tracker_type.lower() == "gitlab":
                project_identifier = str(project_data["id"])
            elif tracker_type.lower() == "github":
                project_identifier = str(project_data["id"])
            elif tracker_type.lower() == "jira":
                project_identifier = (
                    parsed_payload.get("issue", {})
                    .get("fields", {})
                    .get("project", {})
                    .get("key", "")
                )

            if not project_identifier:
                raise HTTPException(
                    status_code=400,
                    detail="Could not determine project identifier from payload",
                )

            project = _resolve_webhook_project(
                db,
                identifier=project_identifier,
                organization_id=plan.organization_id,
                tracker=resolved_tracker,
            )
            if not project:
                raise HTTPException(
                    status_code=404,
                    detail=f"Project with identifier {project_identifier} not found",
                )

            issue_data = parsed_payload.get("issue")
            if not issue_data:
                raise HTTPException(
                    status_code=400, detail="Issue data missing from payload"
                )

            issue = crud_issue.get_by_external_id(
                db,
                project_id=project.id,
                external_id=str(issue_data["id"]),
            )
            if not issue:
                raise HTTPException(status_code=404, detail="Issue not found")

            # Extract comment data based on tracker type
            if tracker_type.lower() == "gitlab":
                comment_data = parsed_payload.get("object_attributes")
            else:  # GitHub and Jira use "comment" field
                comment_data = parsed_payload.get("comment")

            if not comment_data:
                raise HTTPException(
                    status_code=400,
                    detail=f"No comment data found in webhook payload for {tracker_type}",
                )

            tracker_client = TrackerClient(resolved_tracker, initialize_client=False)
            transformed_comment = tracker_client.client.transform_comment(
                comment_data, issue.id
            )

            # Add tracker_id to the comment data
            transformed_comment["tracker_id"] = resolved_tracker.id

            existing_comment = crud_comment.get_by_external_id(
                db,
                issue_id=issue.id,
                external_id=transformed_comment.get("external_id"),
            )

            if existing_comment:
                db_comment = crud_comment.update(
                    db, db_obj=existing_comment, obj_in=transformed_comment
                )
            else:
                db_comment = crud_comment.create(db, obj_in=transformed_comment)

            plan.embeddings.append(
                {
                    "issue_id": issue.id,
                    "comment_id": db_comment.id,
                    "force_update": True,
                }
            )

        db.commit()
        db_processing_success = True
    except HTTPException as http_exc:
        db.rollback()
        db_processing_success = False
        # Use warning level for expected cases like "Issue not found"
        # These are common when webhooks arrive for issues not yet synced
        if http_exc.status_code == 404:
            logger.warning(f"HTTP 404 during webhook processing: {http_exc.detail}")
        elif http_exc.status_code == 400 and http_exc.detail in {
            "Issue data missing from payload",
            "Missing data to construct GitLab issue key.",
        }:
            logger.warning(
                "Ignored webhook payload without issue data: %s", http_exc.detail
            )
        else:
            logger.warning(
                "HTTP exception during webhook processing: %s", http_exc.detail
            )
        # Don't raise yet - publish to NATS first
    except Exception as e:
        db.rollback()
        db_processing_success = False
        logger.error(
            f"Failed to process webhook payload for tracker {resolved_tracker.id}: {e}",
            exc_info=True,
        )
        # Don't raise yet - publish to NATS first

    # Return primitive publication data; the request loop publishes only after
    # the worker has closed its transaction, including error/notification paths.
    plan.db_processed = db_processing_success
    plan.queue_task(
        "process_webhook_event",
        tracker_type=tracker_type.lower(),
        event_type=actual_event_type,
        delivery_id=headers.get("X-GitHub-Delivery")
        or headers.get("X-Gitlab-Event-UUID")
        or headers.get(BITBUCKET_DELIVERY_HEADER),
        payload=parsed_payload,
        tracker_id=str(plan.tracker_id),
        organization_id=str(plan.organization_id),
        embedding_requests=plan.embeddings if db_processing_success else [],
    )
    return {"status": "success", "tracker_id": plan.tracker_id}
