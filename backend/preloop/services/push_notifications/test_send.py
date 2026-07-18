"""Admin-only diagnostic push sends.

Sends a clearly-labelled TEST approval or TEST agent question to a user's
registered devices over the *real* delivery path (native APNs/FCM, or the OSS
push proxy) and reports the provider's verbatim result.

Isolation from real governance
------------------------------
A test send never creates an ``ApprovalRequest`` row and never touches a
workflow, policy, agent or execution. The ``approval_request_id`` it carries is
a freshly minted UUID that exists nowhere in the database, so:

* nothing is ever waiting on the test — it cannot block or gate an agent;
* it cannot appear in approval lists, activity feeds, or approval metrics;
* if the operator taps Approve/Decline, the decision endpoint 404s on the
  unknown id, so no state can change.

Every payload is additionally marked ``is_test`` and titled ``[TEST]`` so it is
unmistakable in the notification shade.
"""

import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)

TestPushKind = Literal["approval", "question"]

# Marker carried in the payload so clients can render a test badge and so the
# id can never be mistaken for a real request in logs.
TEST_TOOL_NAME = "preloop_test_notification"
TEST_TITLE_PREFIX = "[TEST]"


@dataclass
class DeviceSendResult:
    """Outcome of one test send to one device.

    Attributes:
        platform: 'ios' or 'android'.
        token: Masked device token, safe to show in the UI.
        transport: Which path was used ('apns', 'fcm', 'proxy', or 'none').
        success: Whether the provider accepted the notification.
        error: Verbatim provider error, if any.
        error_reason: Machine-readable reason code, if any.
        remediation: Operator-facing hint for fixing the failure.
        project_id: Firebase project id of the server credentials (FCM only).
        status_code: Provider HTTP status, if any (APNs).
        pruned: Whether this dead token was removed as a result.
    """

    platform: str
    token: str
    transport: str
    success: bool
    error: Optional[str] = None
    error_reason: Optional[str] = None
    remediation: Optional[str] = None
    project_id: Optional[str] = None
    status_code: Optional[int] = None
    pruned: bool = False


@dataclass
class TestPushReport:
    """Aggregate result of a test send across a user's devices."""

    kind: str
    request_id: str
    sent: int = 0
    failed: int = 0
    results: List[DeviceSendResult] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "kind": self.kind,
            "request_id": self.request_id,
            "sent": self.sent,
            "failed": self.failed,
            "results": [asdict(result) for result in self.results],
        }


def mask_token(token: str) -> str:
    """Return a short, non-sensitive identifier for a device token."""
    cleaned = (token or "").strip()
    if not cleaned:
        return "<blank>"
    if len(cleaned) <= 12:
        return cleaned
    return f"{cleaned[:8]}...{cleaned[-4:]}"


def build_test_payload(kind: TestPushKind, request_id: str) -> Dict[str, Any]:
    """Build a clearly-labelled synthetic payload for a test send.

    Reuses the production :class:`NotificationPayloadBuilder` so the test
    exercises the same payload shape real approvals use — a test built from a
    bespoke payload would not prove the real path works.

    Args:
        kind: 'approval' for a test approval request, 'question' for a test
            ``ask_user`` question.
        request_id: Synthetic (non-existent) approval request id.

    Returns:
        The APNs/FCM payload dictionary.
    """
    from .notification_payloads import NotificationPayloadBuilder

    if kind == "question":
        summary = (
            f"{TEST_TITLE_PREFIX} This is a test question from Preloop. "
            "No agent is waiting on your answer."
        )
        tool_args: Dict[str, Any] = {
            "is_question": True,
            "question": (
                f"{TEST_TITLE_PREFIX} Can you see this test question? "
                "Answering has no effect."
            ),
            "options": ["Yes, received", "No"],
            "allow_free_text": True,
        }
    else:
        summary = (
            f"{TEST_TITLE_PREFIX} This is a test approval from Preloop. "
            "Approving or declining has no effect on any agent or policy."
        )
        tool_args = {"test": True}

    payload = NotificationPayloadBuilder.new_approval_request(
        request_id=request_id,
        tool_name=TEST_TOOL_NAME,
        priority="medium",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        agent_reasoning="Diagnostic test send triggered from notification settings.",
        tool_args=tool_args,
        summary=summary,
    )

    # Mark the payload as a test at both levels clients read from, and make the
    # title itself unmistakable in the notification shade.
    payload["is_test"] = True
    payload.setdefault("data", {})["is_test"] = True
    alert = payload.get("aps", {}).get("alert", {})
    title = alert.get("title", "Preloop")
    if not title.startswith(TEST_TITLE_PREFIX):
        alert["title"] = f"{TEST_TITLE_PREFIX} {title}"
        payload["data"]["title"] = alert["title"]

    return payload


async def send_test_push(
    devices: List[Dict[str, str]],
    kind: TestPushKind = "approval",
) -> TestPushReport:
    """Send a test notification to each device over the real delivery path.

    Never raises: every device failure is captured in the report so a broken
    device can never break the caller.

    Args:
        devices: Device dicts with 'platform' and 'token' keys.
        kind: 'approval' or 'question'.

    Returns:
        A :class:`TestPushReport` with one entry per device.
    """
    from preloop.services.push_proxy import (
        is_push_proxy_configured,
        send_push_via_proxy,
    )

    from . import get_apns_service
    from .fcm_service import is_fcm_configured, send_fcm_notification

    request_id = str(uuid.uuid4())
    payload = build_test_payload(kind, request_id)
    report = TestPushReport(kind=kind, request_id=request_id)

    alert = payload.get("aps", {}).get("alert", {})
    title = alert.get("title", "Preloop test")
    body = alert.get("body", "Preloop test notification")
    data = payload.get("data", {})

    apns_service = get_apns_service()
    fcm_available = is_fcm_configured()
    proxy_available = is_push_proxy_configured()

    for device in devices:
        platform = (device.get("platform") or "").strip().lower()
        token = (device.get("token") or "").strip()
        result = await _send_one(
            platform=platform,
            token=token,
            payload=payload,
            title=title,
            body=body,
            data=data,
            apns_service=apns_service,
            fcm_available=fcm_available,
            proxy_available=proxy_available,
            send_fcm_notification=send_fcm_notification,
            send_push_via_proxy=send_push_via_proxy,
        )
        report.results.append(result)
        if result.success:
            report.sent += 1
        else:
            report.failed += 1

    logger.info(
        "Test push (%s) request_id=%s sent=%d failed=%d",
        kind,
        request_id,
        report.sent,
        report.failed,
    )
    return report


async def _send_one(
    *,
    platform: str,
    token: str,
    payload: Dict[str, Any],
    title: str,
    body: str,
    data: Dict[str, Any],
    apns_service: Any,
    fcm_available: bool,
    proxy_available: bool,
    send_fcm_notification: Any,
    send_push_via_proxy: Any,
) -> DeviceSendResult:
    """Send to a single device and translate the outcome into a result."""
    masked = mask_token(token)

    if platform not in ("ios", "android"):
        return DeviceSendResult(
            platform=platform or "unknown",
            token=masked,
            transport="none",
            success=False,
            error=f"Unsupported platform {platform!r}",
            error_reason="unsupported_platform",
            remediation="Device was stored with an unrecognised platform value.",
        )

    if not token:
        return DeviceSendResult(
            platform=platform,
            token=masked,
            transport="none",
            success=False,
            error="Stored device token is blank",
            error_reason="blank_token",
            remediation="Re-register the device.",
        )

    try:
        if platform == "ios" and apns_service is not None:
            success, status_code, error_reason = await apns_service.send_notification(
                device_token=token, payload=payload, priority=10
            )
            return DeviceSendResult(
                platform=platform,
                token=masked,
                transport="apns",
                success=bool(success),
                error=None if success else (error_reason or "APNs rejected the send"),
                error_reason=None
                if success
                else (error_reason or "unknown_apns_error"),
                status_code=status_code,
                remediation=None
                if success
                else _apns_remediation(status_code, error_reason),
            )

        if platform == "android" and fcm_available:
            result = await send_fcm_notification(
                token=token, title=title, body=body, data=data, priority="high"
            )
            return DeviceSendResult(
                platform=platform,
                token=masked,
                transport="fcm",
                success=bool(result.get("success")),
                error=result.get("error"),
                error_reason=result.get("error_reason"),
                remediation=result.get("remediation"),
                project_id=result.get("project_id"),
            )

        if proxy_available:
            result = await send_push_via_proxy(
                platform=platform,
                device_token=token,
                title=title,
                body=body,
                data=data,
                priority="high",
            )
            return DeviceSendResult(
                platform=platform,
                token=masked,
                transport="proxy",
                success=bool(result.get("success")),
                error=result.get("error"),
                error_reason=result.get("error_reason") or "push_proxy_error",
                remediation=result.get("remediation"),
            )

        return DeviceSendResult(
            platform=platform,
            token=masked,
            transport="none",
            success=False,
            error="Push notifications are not configured on this server",
            error_reason="not_configured",
            remediation=(
                "Set APNS_* credentials for iOS, FCM_CREDENTIALS_JSON for "
                "Android, or PUSH_PROXY_URL/PUSH_PROXY_API_KEY for the proxy."
            ),
        )

    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Test push to %s device %s raised", platform, masked)
        return DeviceSendResult(
            platform=platform,
            token=masked,
            transport="unknown",
            success=False,
            error=str(exc),
            error_reason="exception",
            remediation="Unexpected server error while sending; see server logs.",
        )


def _apns_remediation(status_code: int, error_reason: Optional[str]) -> str:
    """Return an operator-facing hint for an APNs failure."""
    reason = (error_reason or "").strip()
    if reason in {"BadDeviceToken", "DeviceTokenNotForTopic"}:
        return (
            "The stored token is not valid for this APNs topic/environment. "
            "Check APNS_BUNDLE_ID and APNS_USE_SANDBOX match the installed build."
        )
    if reason == "Unregistered" or status_code == 410:
        return "The device unregistered (app deleted). Re-pair the device."
    if status_code in (401, 403):
        return (
            "APNs rejected the server credentials. Check APNS_TEAM_ID, "
            "APNS_KEY_ID and APNS_AUTH_KEY."
        )
    return "See APNs reason code for details."
