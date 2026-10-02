"""Deliver a policy notice (#959) on the channels the account already uses.

Recipients are the policy owners: active users of the account who are its
primary user, a superuser, or hold ``manage_policies``. Channels:

* email, for each owner whose ``enable_email`` preference is on (the default
  when no preferences row exists);
* mobile push, for each owner with ``enable_mobile_push`` on;
* Slack, Mattermost or a generic webhook, when the rule's approval workflow
  (or the account default workflow) is of that type and has a URL. The body
  goes through the webhook outbox like approval messages do.

A notice names the rule, says this rule did not block the call (a later
deny or approval rule in the same evaluation still can), and carries the
redacted excerpt. It has no approve or deny link: there is nothing to decide.
Debouncing happens before this module is called.
"""

from __future__ import annotations

import asyncio
import html
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar
from urllib.parse import urljoin

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_approval_workflow,
    crud_policy_notice_hit,
    crud_user,
    notification_preferences,
)

logger = logging.getLogger(__name__)

#: Event type recorded on the outbox row. Not part of the signed v1 event
#: catalogue: the body is the legacy approval-workflow shape.
EVENT_POLICY_NOTICE = "policy.notice"

#: Workflow types whose webhook receives notices.
WEBHOOK_APPROVAL_TYPES = frozenset({"slack", "mattermost", "webhook"})

_MAX_RECIPIENTS = 500

T = TypeVar("T")


def _base_url() -> str:
    from preloop.config import settings

    return (settings.preloop_url or "http://localhost:8000").rstrip("/") + "/"


def attention_url() -> str:
    """Console page where the notice card lives."""
    return urljoin(_base_url(), "console/attention")


def policy_owners(
    db: Session, account_id: Any, *, limit: int = _MAX_RECIPIENTS
) -> List[models.User]:
    """Active users of the account who may manage its policies.

    Owners are the primary user, superusers, and users holding
    ``manage_policies`` directly or through a team. The filter runs in the
    query, so plain members never take a recipient slot.

    Args:
        db: Database session.
        account_id: Account whose owners to list.
        limit: Maximum number of recipients.

    Returns:
        The recipients, primary user first when present.
    """
    account = crud_account.get(db, id=account_id)
    return crud_policy_notice_hit.get_policy_owners(
        db,
        account_id=account_id,
        primary_user_id=getattr(account, "primary_user_id", None),
        permission_name="manage_policies",
        limit=limit,
    )


def _run_async(factory: Callable[[], Awaitable[T]]) -> T:
    """Run a coroutine to completion from sync code.

    Delivery runs on the DB thread pool where no loop is running, so
    ``asyncio.run`` is the normal path. If a loop is running on this thread
    the coroutine is run on a helper thread instead of nesting loops.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    box: Dict[str, Any] = {}

    def _target() -> None:
        try:
            box["value"] = asyncio.run(factory())
        except Exception as exc:  # re-raised on the calling thread below
            box["error"] = exc

    worker = threading.Thread(target=_target, daemon=True)
    worker.start()
    worker.join()
    if "error" in box:
        raise box["error"]
    if "value" not in box:
        raise RuntimeError("policy notice delivery helper thread did not finish")
    return box["value"]


def _who(hit: models.PolicyNoticeHit, db: Session) -> str:
    if hit.user_id is None:
        return "an API caller"
    user = crud_user.get(db, id=hit.user_id)
    return user.username if user is not None else "a removed user"


def _target_label(target: str) -> str:
    return "response" if target == "model.response" else "request"


def build_message(db: Session, hit: models.PolicyNoticeHit) -> Dict[str, str]:
    """Subject, plain text and HTML of one notice.

    Args:
        db: Database session.
        hit: The recorded hit.

    Returns:
        ``subject``, ``text``, ``html`` and ``headline`` strings.
    """
    who = _who(hit, db)
    label = _target_label(hit.target)
    headline = f"Policy notice: {hit.rule_id}"
    excerpt = hit.excerpt or "(excerpt not available)"
    link = attention_url()
    lines = [
        f"The notify rule '{hit.rule_id}' matched a model {label} from {who}.",
        "This notify rule did not block the call.",
    ]
    if hit.rule_description:
        lines.append(f"Rule: {hit.rule_description}")
    lines += [
        "",
        "Excerpt (secrets redacted):",
        excerpt,
        "",
        "Further matches of this rule for this user are counted but not",
        "sent again for an hour.",
        f"See all notices: {link}",
    ]
    text = "\n".join(lines)
    description = (
        f"<p>Rule: {html.escape(hit.rule_description)}</p>"
        if hit.rule_description
        else ""
    )
    body_html = (
        f"<p>The notify rule <strong>{html.escape(hit.rule_id)}</strong> matched "
        f"a model {label} from {html.escape(who)}. This notify rule did not block "
        "the call.</p>"
        f"{description}"
        "<p>Excerpt (secrets redacted):</p>"
        f"<pre>{html.escape(excerpt)}</pre>"
        "<p>Further matches of this rule for this user are counted but not sent "
        "again for an hour.</p>"
        f'<p><a href="{html.escape(link)}">See all notices</a></p>'
    )
    return {"subject": headline, "headline": headline, "text": text, "html": body_html}


def send_email_notices(
    db: Session, hit: models.PolicyNoticeHit, owners: List[models.User]
) -> int:
    """Email every owner whose preferences allow email. Returns the count."""
    from preloop.utils.email import send_email

    message = build_message(db, hit)
    sent = 0
    for owner in owners:
        if not owner.email:
            continue
        prefs = notification_preferences.get_by_user(db, owner.id)
        if prefs is not None and not prefs.enable_email:
            continue
        try:
            send_email(
                owner.email, message["subject"], message["text"], message["html"]
            )
            sent += 1
        except Exception:  # noqa: BLE001 - one bad address must not stop the rest
            logger.warning("Policy notice email failed", exc_info=True)
    return sent


def build_push_payload(hit: models.PolicyNoticeHit) -> Dict[str, Any]:
    """APNs-shaped payload with no approval category (nothing to decide)."""
    body = hit.excerpt or f"A model {_target_label(hit.target)} matched."
    data = {
        "type": "policy_notice",
        "rule_id": hit.rule_id,
        "hit_id": str(hit.id),
        "url": "/console/attention",
    }
    return {
        "aps": {
            "alert": {"title": f"Policy notice: {hit.rule_id}", "body": body[:180]},
            "sound": "default",
            "thread-id": "policy-notices",
        },
        "data": data,
    }


def send_push_notices(
    db: Session, hit: models.PolicyNoticeHit, owners: List[models.User]
) -> int:
    """Push to every owner device with mobile push on. Returns the count."""
    from preloop.services.approval_service import _send_android_push_transport
    from preloop.services.push_notifications import (
        get_apns_service,
        is_fcm_configured,
    )
    from preloop.services.push_proxy import (
        is_push_proxy_configured,
        send_push_via_proxy,
    )

    targets: List[tuple[str, str]] = []
    for owner in owners:
        prefs = notification_preferences.get_by_user(db, owner.id)
        if prefs is None or not prefs.enable_mobile_push:
            continue
        if getattr(prefs, "notify_when_needed", True) is False:
            continue
        for platform in ("ios", "android"):
            for token in prefs.get_device_tokens(platform=platform) or []:
                if token and token.strip():
                    targets.append((platform, token.strip()))
    if not targets:
        return 0

    apns = get_apns_service()
    fcm_available = is_fcm_configured()
    use_proxy = apns is None and is_push_proxy_configured()
    payload = build_push_payload(hit)
    title = payload["aps"]["alert"]["title"]
    body = payload["aps"]["alert"]["body"]
    data = payload["data"]

    async def _send_all() -> int:
        sent = 0
        for platform, token in targets:
            try:
                if platform == "ios" and apns is not None:
                    ok, _status, _reason = await apns.send_notification(
                        device_token=token, payload=payload, priority=5
                    )
                elif platform == "ios" and use_proxy:
                    result = await send_push_via_proxy(
                        platform="ios",
                        device_token=token,
                        title=title,
                        body=body,
                        data=data,
                        priority="normal",
                    )
                    ok = bool(result.get("success"))
                elif platform == "android":
                    result = await _send_android_push_transport(
                        token=token,
                        title=title,
                        body=body,
                        data=data,
                        priority="normal",
                        fcm_available=fcm_available,
                        use_proxy=use_proxy,
                    )
                    ok = bool(result.get("success"))
                else:
                    ok = False
            except Exception:  # noqa: BLE001 - keep going with the next device
                logger.warning("Policy notice push failed", exc_info=True)
                ok = False
            sent += int(ok)
        return sent

    return _run_async(_send_all)


def _resolve_workflow(
    db: Session, account_id: Any, name: Optional[str]
) -> Optional[models.ApprovalWorkflow]:
    if name:
        workflow = crud_approval_workflow.get_by_name(
            db, account_id=str(account_id), name=name
        )
        if workflow is not None:
            return workflow
    return crud_approval_workflow.get_default(db, account_id=str(account_id))


def build_webhook_payload(
    db: Session, hit: models.PolicyNoticeHit, approval_type: str
) -> Dict[str, Any]:
    """Body for the workflow webhook: chat text or a generic JSON object."""
    message = build_message(db, hit)
    if approval_type in ("slack", "mattermost"):
        excerpt = hit.excerpt or "(excerpt not available)"
        text = (
            f"**{message['headline']}**\n\n"
            f"The notify rule `{hit.rule_id}` matched a model "
            f"{_target_label(hit.target)} from {_who(hit, db)}. "
            "This notify rule did not block the call.\n\n"
            f"**Excerpt (secrets redacted):**\n```\n{excerpt}\n```\n\n"
            f"[See all notices]({attention_url()})\n"
        )
        return {"text": text}
    created = hit.created_at or datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return {
        "type": "policy_notice",
        "hit_id": str(hit.id),
        "rule_id": hit.rule_id,
        "rule_description": hit.rule_description,
        "target": hit.target,
        "user": _who(hit, db) if hit.user_id else None,
        "excerpt": hit.excerpt,
        "text_sha256": hit.text_sha256,
        "occurred_at": created.isoformat(),
        "url": attention_url(),
    }


def send_webhook_notice(
    db: Session,
    hit: models.PolicyNoticeHit,
    *,
    approval_workflow: Optional[str] = None,
) -> bool:
    """Queue the notice on the workflow's Slack, Mattermost or webhook URL.

    Args:
        db: Database session.
        hit: The recorded hit.
        approval_workflow: Workflow named on the rule, if any.

    Returns:
        True when a delivery was queued.
    """
    from preloop.services.event_webhooks import outbox
    from preloop.services.event_webhooks.approval_shim import (
        resolve_webhook_target,
        sync_shim_endpoint,
    )

    workflow = _resolve_workflow(db, hit.account_id, approval_workflow)
    if workflow is None:
        return False
    target = resolve_webhook_target(workflow)
    endpoint = sync_shim_endpoint(db, workflow)
    if endpoint is None or target is None:
        # The workflow has no URL. Keep the shim's deactivation of a stale
        # endpoint, as the approval path does, instead of losing it when the
        # short-lived session closes.
        db.commit()
        return False
    result = outbox.enqueue_raw_delivery(
        db,
        endpoint=endpoint,
        event_type=EVENT_POLICY_NOTICE,
        payload=build_webhook_payload(db, hit, target[0]),
        natural_key=f"policy_notice:{hit.id}",
        occurred_at=hit.created_at,
        subject_id=None,
    )
    db.commit()
    return bool(result.delivery_ids)


def deliver_policy_notice(
    db: Session,
    hit: models.PolicyNoticeHit,
    *,
    approval_workflow: Optional[str] = None,
) -> Dict[str, Any]:
    """Send one notice on every enabled channel. Never raises.

    Args:
        db: Database session.
        hit: The recorded hit (already claimed for delivery).
        approval_workflow: Workflow named on the rule, if any.

    Returns:
        Per-channel counts, for logs and tests.
    """
    result: Dict[str, Any] = {"email": 0, "push": 0, "webhook": False}
    owners = policy_owners(db, hit.account_id)
    result["recipients"] = len(owners)
    for channel, send in (
        ("email", lambda: send_email_notices(db, hit, owners)),
        ("push", lambda: send_push_notices(db, hit, owners)),
        (
            "webhook",
            lambda: send_webhook_notice(db, hit, approval_workflow=approval_workflow),
        ),
    ):
        try:
            result[channel] = send()
        except Exception:  # noqa: BLE001 - one channel must not stop the others
            logger.warning("Policy notice %s delivery failed", channel, exc_info=True)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
    return result
