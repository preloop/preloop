"""Service for managing approval requests and webhook posting."""

import asyncio
import concurrent.futures
import json
import uuid
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Set
from urllib.parse import urljoin

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from preloop.models.models import (
    ApprovalRequest,
    ApprovalWorkflow,
    AutoApprovedReason,
)
from preloop.models.schemas.approval_request import (
    ApprovalRequestUpdate,
)
from preloop.models.crud.approval_request import (
    get_approval_request_async,
    get_approval_request_for_update_async,
)
from preloop.models.crud.approval_bypass import (
    get_active_bypass_async,
    record_bypass_use_async,
)
from preloop.models.models.approval_bypass import ApprovalBypass, ApprovalBypassMode
from preloop.sync.services.event_bus import get_task_publisher
from preloop.services.ai_approval_service import get_ai_approval_service

logger = logging.getLogger(__name__)

_LOCAL_PRESENCE_WINDOW = timedelta(seconds=20)


def _approval_config_dict(workflow: ApprovalWorkflow) -> Dict[str, Any]:
    """Return ``approval_config`` only when it is a real dict."""
    cfg = getattr(workflow, "approval_config", None)
    return cfg if isinstance(cfg, dict) else {}


def _parse_mattermost_bot_target(
    workflow: ApprovalWorkflow,
) -> tuple[Optional[tuple[str, str, str]], Optional[str]]:
    """Return ``(target, error)`` for a Mattermost bot post.

    Incoming webhooks cannot target direct messages (Mattermost returns 403).
    The bot-token Posts API can. Both values are None when no bot keys were
    supplied so empty standard workflows and MagicMock fixtures stay silent.
    """
    cfg = _approval_config_dict(workflow)
    raw_token = cfg.get("bot_token") or cfg.get("mattermost_token")
    raw_url = cfg.get("mattermost_url") or cfg.get("base_url")
    raw_channel = cfg.get("channel_id")
    if not isinstance(raw_channel, str) or not raw_channel.strip():
        fallback = getattr(workflow, "channel", None)
        raw_channel = fallback if isinstance(fallback, str) else ""

    token_ok = isinstance(raw_token, str) and bool(raw_token.strip())
    url_ok = isinstance(raw_url, str) and bool(raw_url.strip())
    if not token_ok and not url_ok:
        return None, None

    missing: List[str] = []
    if not token_ok:
        missing.append("bot_token (or mattermost_token)")
    if not url_ok:
        missing.append("mattermost_url (or base_url)")
    channel = raw_channel.strip() if isinstance(raw_channel, str) else ""
    if not channel:
        missing.append("channel_id")
    if missing:
        return None, (
            "Mattermost bot configuration incomplete: missing " + ", ".join(missing)
        )

    token = str(raw_token).strip()
    url = str(raw_url).strip().rstrip("/")
    if not url.startswith(("https://", "http://")):
        return None, (
            "Mattermost bot configuration incomplete: "
            "mattermost_url must start with https:// or http://"
        )
    if url.startswith("http://"):
        logger.warning(
            "Mattermost bot token will be sent over plaintext HTTP (%s). "
            "Prefer https:// unless this is a trusted internal instance.",
            url,
        )
    return (url, token, channel), None


def mattermost_bot_target(
    workflow: ApprovalWorkflow,
) -> Optional[tuple[str, str, str]]:
    """Return ``(base_url, token, channel_id)`` for a Mattermost bot post.

    Incoming webhooks cannot target direct messages (Mattermost returns 403).
    The bot-token Posts API can. Non-string fields return None so empty
    standard workflows and MagicMock fixtures stay silent.
    """
    target, _error = _parse_mattermost_bot_target(workflow)
    return target


def mattermost_bot_config_error(workflow: ApprovalWorkflow) -> Optional[str]:
    """Return why bot credentials were rejected, or None if none were set."""
    _target, error = _parse_mattermost_bot_target(workflow)
    return error


def _operator_present_at_machine(db, approval_request: ApprovalRequest) -> bool:
    """True only when the operator is at the local TTY.

    Sidecar WebSocket heartbeats stamp ``last_seen_at`` in both local and
    remote mode, so recency alone is not presence. Skip the approval push
    only when the last advertised session mode is ``local`` *and* that
    heartbeat is inside a 20s window. Unknown or remote mode fail open:
    send the push rather than hide an approval from a phone/web operator.
    """
    agent_id = getattr(approval_request, "managed_agent_id", None)
    if agent_id is None:
        return False
    from preloop.models.crud import crud_managed_agent

    agent = crud_managed_agent.get(db, id=agent_id)
    if agent is None:
        return False
    mode = getattr(agent, "control_session_mode", None)
    if mode != "local":
        snapshot: dict = {}
        try:
            from preloop.api.endpoints.agent_control import agent_control_snapshot

            snapshot = agent_control_snapshot(str(agent_id))
        except (ImportError, AttributeError, RuntimeError):
            snapshot = {}
        if snapshot.get("session_mode") != "local":
            return False
    seen = getattr(agent, "last_seen_at", None)
    if seen is None:
        return False
    if seen.tzinfo is None:
        from datetime import UTC as _UTC

        seen = seen.replace(tzinfo=_UTC)
    from datetime import UTC

    return datetime.now(UTC) - seen <= _LOCAL_PRESENCE_WINDOW


def _get_audit_service():
    """Get the audit service instance (lazy import to avoid circular deps)."""
    try:
        from plugins.audit.service import get_audit_service

        return get_audit_service()
    except ImportError:
        logger.debug("Audit service not available")
        return None


def _get_db_factory():
    """Get a database session factory for async audit logging."""
    try:
        from preloop.models.db.session import get_session_factory

        def _create_session():
            """Create a session that the caller is responsible for closing."""
            factory = get_session_factory()
            return factory()

        return _create_session
    except ImportError:
        return None


def _log_approval_lifecycle_async(
    account_id: str,
    approval_id: uuid.UUID,
    event: str,
    tool_name: Optional[str] = None,
    approver_id: Optional[uuid.UUID] = None,
    reason: Optional[str] = None,
    execution_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    extra_details: Optional[Dict[str, Any]] = None,
) -> None:
    """Log an approval lifecycle event asynchronously (fire-and-forget).

    This helper function wraps the audit service call to log approval lifecycle
    events without blocking the main execution flow.

    Args:
        account_id: Account ID
        approval_id: Approval request ID
        event: Lifecycle event ('created', 'approved', 'denied', 'expired', 'escalated')
        tool_name: Name of the tool (if available)
        approver_id: ID of the user who approved/denied
        reason: Reason or comment for the decision
        execution_id: Flow execution ID (if applicable)
        correlation_id: Correlation ID for grouping related audit events
        extra_details: Additional metadata to include in the audit log details
    """
    try:
        audit_service = _get_audit_service()
        if not audit_service:
            return

        db_factory = _get_db_factory()
        if not db_factory:
            return

        # Convert execution_id to UUID if it's a string
        exec_id = None
        if execution_id:
            try:
                exec_id = uuid.UUID(execution_id)
            except (ValueError, TypeError):
                # execution_id may be a non-UUID flow id; omit it from audit metadata.
                pass

        audit_service.log_approval_lifecycle_async(
            db_factory=db_factory,
            account_id=account_id,
            approval_id=approval_id,
            event=event,
            tool_name=tool_name,
            approver_id=approver_id,
            reason=reason,
            execution_id=exec_id,
            correlation_id=correlation_id,
            extra_details=extra_details,
        )
    except Exception as e:
        logger.debug(f"Failed to log approval lifecycle to audit: {e}")


def _current_correlation_id() -> Optional[str]:
    """Best-effort lookup of the active correlation_id from the MCP context var."""
    try:
        from preloop.services.dynamic_fastmcp import _correlation_id_var

        return _correlation_id_var.get(None)
    except Exception:
        return None


def _log_approval_notification_async(
    account_id: str,
    approval_id: uuid.UUID,
    channel: str,
    status: str,
    tool_name: Optional[str] = None,
    recipient_user_ids: Optional[List[uuid.UUID]] = None,
    sent_count: Optional[int] = None,
    failed_count: Optional[int] = None,
    skipped_count: Optional[int] = None,
    error: Optional[str] = None,
    correlation_id: Optional[str] = None,
    extra_details: Optional[Dict[str, Any]] = None,
) -> None:
    """Fire-and-forget audit log entry for one approval notification fan-out."""
    try:
        audit_service = _get_audit_service()
        if not audit_service:
            return
        db_factory = _get_db_factory()
        if not db_factory:
            return
        audit_service.log_approval_notification_async(
            db_factory=db_factory,
            account_id=account_id,
            approval_id=approval_id,
            channel=channel,
            status=status,
            tool_name=tool_name,
            recipient_user_ids=recipient_user_ids,
            sent_count=sent_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            error=error,
            correlation_id=correlation_id,
            extra_details=extra_details,
        )
    except Exception as e:
        logger.debug(f"Failed to log approval notification to audit: {e}")


def _log_approval_tool_executed_async(
    account_id: str,
    approval_id: uuid.UUID,
    tool_name: str,
    status: str,
    duration_ms: Optional[int] = None,
    result_preview: Optional[str] = None,
    error: Optional[str] = None,
    correlation_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    extra_details: Optional[Dict[str, Any]] = None,
) -> None:
    """Fire-and-forget audit log entry for the post-approval tool execution."""
    try:
        audit_service = _get_audit_service()
        if not audit_service:
            return
        db_factory = _get_db_factory()
        if not db_factory:
            return
        exec_id: Optional[uuid.UUID] = None
        if execution_id:
            try:
                exec_id = uuid.UUID(execution_id)
            except (ValueError, TypeError):
                # execution_id may be a non-UUID flow id; omit it from audit metadata.
                pass
        audit_service.log_approval_tool_executed_async(
            db_factory=db_factory,
            account_id=account_id,
            approval_id=approval_id,
            tool_name=tool_name,
            status=status,
            duration_ms=duration_ms,
            result_preview=result_preview,
            error=error,
            correlation_id=correlation_id,
            execution_id=exec_id,
            extra_details=extra_details,
        )
    except Exception as e:
        logger.debug(f"Failed to log approval tool execution to audit: {e}")


# Thread pool for running sync database operations
_sync_db_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

# Push first; email dual-channel users only if still pending after this delay.
APPROVAL_EMAIL_STAGGER_SECONDS = 60

# Retain strong references to delayed-email tasks so they are not GC'd mid-sleep.
# Cap prevents unbounded growth if completions stall under load.
_MAX_DELAYED_EMAIL_TASKS = 500
_delayed_email_tasks: Set["asyncio.Task[Any]"] = set()


def _retain_delayed_email_task(task: "asyncio.Task[Any]") -> None:
    """Keep a strong reference to a delayed-email task until it completes.

    Evicts done tasks first. If the set is still at capacity, cancels the
    oldest retained task so a stuck completion callback cannot grow forever.
    """
    done = {t for t in _delayed_email_tasks if t.done()}
    if done:
        _delayed_email_tasks.difference_update(done)

    if len(_delayed_email_tasks) >= _MAX_DELAYED_EMAIL_TASKS:
        # Prefer cancelling an unfinished task over dropping the new one:
        # the new schedule is the live approval; oldest may be orphaned.
        oldest = next(iter(_delayed_email_tasks))
        logger.warning(
            "Delayed-email task set at cap (%s); cancelling oldest task",
            _MAX_DELAYED_EMAIL_TASKS,
        )
        oldest.cancel()
        _delayed_email_tasks.discard(oldest)

    _delayed_email_tasks.add(task)

    def _discard(finished: "asyncio.Task[Any]") -> None:
        _delayed_email_tasks.discard(finished)

    task.add_done_callback(_discard)


async def _send_delayed_approval_email(
    *,
    request_id: uuid.UUID,
    user_ids: List[uuid.UUID],
    delay_seconds: int,
    base_url: str,
    correlation_id: Optional[str],
    account_id: str,
    tool_name: Optional[str],
) -> None:
    """Send staggered approval emails after a delay if still pending.

    Opens a fresh DB session at fire time so status reflects commits from
    other sessions. Never raises into the event loop.

    Args:
        request_id: Approval request to re-check.
        user_ids: Dual-channel users waiting for delayed email.
        delay_seconds: Seconds to wait before the status re-check.
        base_url: Application base URL for approval links.
        correlation_id: Optional audit correlation id.
        account_id: Account id for audit logging.
        tool_name: Tool name for audit logging.
    """
    try:
        await asyncio.sleep(delay_seconds)

        from preloop.models.db.session import get_async_db_session

        async with get_async_db_session() as db:
            current_request = await get_approval_request_async(db, request_id)
            # Only "pending" should receive delayed email; any other status
            # (terminal or unexpected) skips. Checking terminal separately
            # was redundant with the pending equality check.
            if not current_request or current_request.status != "pending":
                reason = (
                    f"status_{current_request.status}"
                    if current_request
                    else "not_found"
                )
                logger.info(
                    "Skipping delayed email for approval %s: %s",
                    request_id,
                    reason,
                )
                _log_approval_notification_async(
                    account_id=account_id,
                    approval_id=request_id,
                    channel="email",
                    status="skipped",
                    tool_name=tool_name,
                    recipient_user_ids=list(user_ids),
                    skipped_count=len(user_ids),
                    error="resolved_before_email",
                    correlation_id=correlation_id,
                )
                return

            if (
                current_request.expires_at
                and datetime.utcnow() > current_request.expires_at
            ):
                logger.info(
                    "Skipping delayed email for approval %s: expired",
                    request_id,
                )
                _log_approval_notification_async(
                    account_id=account_id,
                    approval_id=request_id,
                    channel="email",
                    status="skipped",
                    tool_name=tool_name,
                    recipient_user_ids=list(user_ids),
                    skipped_count=len(user_ids),
                    error="resolved_before_email",
                    correlation_id=correlation_id,
                )
                return

            service = ApprovalService(db, base_url)
            workflow = current_request.approval_workflow
            if workflow is None:
                logger.warning(
                    "Delayed email for %s missing workflow; skipping send",
                    request_id,
                )
                _log_approval_notification_async(
                    account_id=account_id,
                    approval_id=request_id,
                    channel="email",
                    status="skipped",
                    tool_name=tool_name,
                    recipient_user_ids=list(user_ids),
                    skipped_count=len(user_ids),
                    error="missing_workflow",
                    correlation_id=correlation_id,
                )
                return

            result = await service._send_email_notification(
                current_request, workflow, user_ids=user_ids
            )
            sent_raw = result.get("sent")
            failed_raw = result.get("failed")
            sent_count = sent_raw if isinstance(sent_raw, int) else 0
            failed_count = failed_raw if isinstance(failed_raw, int) else 0
            if sent_count > 0 and failed_count == 0:
                status = "sent"
            elif sent_count > 0:
                status = "partial"
            elif result.get("success") is False:
                status = "failed"
            else:
                status = "sent"
            _log_approval_notification_async(
                account_id=account_id,
                approval_id=request_id,
                channel="email",
                status=status,
                tool_name=tool_name or current_request.tool_name,
                recipient_user_ids=list(user_ids),
                sent_count=sent_count,
                failed_count=failed_count,
                error=str(result.get("error")) if result.get("error") else None,
                correlation_id=correlation_id,
            )
            logger.info(
                "Delayed email for approval %s: %s",
                request_id,
                result,
            )
    except asyncio.CancelledError:
        logger.info("Delayed email task cancelled for approval %s", request_id)
    except Exception as exc:
        logger.error(
            "Failed delayed email for approval %s: %s",
            request_id,
            exc,
            exc_info=True,
        )


async def _send_android_push_transport(
    *,
    token: str,
    title: str,
    body: str,
    data: Dict[str, Any],
    priority: str,
    fcm_available: bool,
    use_proxy: bool,
) -> Dict[str, Any]:
    """Send one Android push via FCM or the push proxy; return the result dict.

    The single place that knows both transports' call signatures. The main and
    escalation paths previously each spelled these calls out, and the copies
    drifted: the escalation copy passed ``device_token=`` to
    ``send_fcm_notification`` (whose parameter is ``token``), so every
    escalation send raised TypeError. Keeping transport dispatch here makes
    that class of divergence unrepeatable.

    Callers classify the result themselves — the main path prunes dead tokens,
    escalation only counts — so policy stays local while transport is shared.

    Returns:
        The transport's result dict; ``{"success": False, ...}`` when no
        transport is configured.
    """
    from preloop.services.push_notifications import send_fcm_notification
    from preloop.services.push_proxy import send_push_via_proxy

    if fcm_available:
        return await send_fcm_notification(
            token=token,
            title=title,
            body=body,
            data=data,
            priority=priority,
        )
    if use_proxy:
        return await send_push_via_proxy(
            platform="android",
            device_token=token,
            title=title,
            body=body,
            data=data,
            priority=priority,
        )
    return {"success": False, "error": "no_push_transport_configured"}


class ApprovalService:
    """Service for handling approval requests."""

    def __init__(self, db: AsyncSession, base_url: str):
        """Initialize approval service.

        Args:
            db: Database session
            base_url: Base URL for generating approval links
        """
        self.db = db
        self.base_url = base_url

    async def _broadcast_approval_update(
        self,
        approval_request: ApprovalRequest,
        event_type: str,
        extra_data: Optional[Dict[str, Any]] = None,
    ):
        """Broadcast approval request update via NATS/WebSocket.

        Args:
            approval_request: The approval request
            event_type: Type of event (created, approved, declined, expired, vote_received)
            extra_data: Optional additional data to include in the broadcast
        """
        try:
            task_publisher = await get_task_publisher()
            if not task_publisher or not task_publisher.nc:
                logger.warning("NATS not available for broadcasting approval update")
                return

            from preloop.utils.redaction import redact_dict

            # Prepare update message (redact tool_args for broadcast/display)
            update_data = {
                "type": f"approval_{event_type}",
                "approval_request_id": str(approval_request.id),
                "account_id": str(approval_request.account_id),
                "tool_configuration_id": str(approval_request.tool_configuration_id),
                "approval_workflow_id": str(approval_request.approval_workflow_id),
                "execution_id": approval_request.execution_id,
                "tool_name": approval_request.tool_name,
                "summary": approval_request.summary,
                "tool_args": redact_dict(approval_request.tool_args or {}),
                "agent_reasoning": approval_request.agent_reasoning,
                "managed_agent_name": approval_request.managed_agent_name,
                # Why the call was gated. Rule names and expressions are
                # operator-authored policy text, not call arguments, so they
                # are not redacted; tool_args above still are.
                "rule_context": approval_request.rule_context,
                "status": approval_request.status,
                "requested_at": approval_request.requested_at.isoformat(),
                "resolved_at": (
                    approval_request.resolved_at.isoformat()
                    if approval_request.resolved_at
                    else None
                ),
                "expires_at": (
                    approval_request.expires_at.isoformat()
                    if approval_request.expires_at
                    else None
                ),
                "timestamp": datetime.utcnow().isoformat(),
            }

            # Include extra data if provided (for quorum progress, etc.)
            if extra_data:
                update_data.update(extra_data)

            # Publish to NATS subject for approval updates
            subject = "approval-updates"
            await task_publisher.nc.publish(
                subject, json.dumps(update_data, default=str).encode()
            )

            logger.info(
                f"Broadcasted approval {event_type} for request {approval_request.id}"
            )

        except Exception as e:
            logger.error(f"Failed to broadcast approval update: {e}", exc_info=True)

    async def create_approval_request(
        self,
        account_id: str,
        tool_configuration_id: uuid.UUID,
        approval_workflow_id: uuid.UUID,
        tool_name: str,
        tool_args: Dict[str, Any],
        agent_reasoning: Optional[str] = None,
        execution_id: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        managed_agent_id: Optional[uuid.UUID] = None,
        runtime_session_id: Optional[uuid.UUID] = None,
        managed_agent_name: Optional[str] = None,
        rule_context: Optional[Dict[str, Any]] = None,
    ) -> ApprovalRequest:
        """Create a new approval request.

        Args:
            account_id: The account creating the request
            tool_configuration_id: Tool configuration ID
            approval_workflow_id: Approval workflow ID
            tool_name: Name of the tool being executed
            tool_args: Arguments passed to the tool
            agent_reasoning: Agent's reasoning for the tool call
            execution_id: Flow execution ID (if applicable)
            timeout_seconds: How long to wait for approval (default: 5 minutes)
            rule_context: Snapshot of the policy rule that required this
                approval (see services/approval_rule_context.py). Stored as
                given so a later edit to the rule cannot rewrite the reason a
                past approval was asked for. None when no rule was evaluated
                (e.g. the request_approval builtin), in which case surfaces
                omit the explanation rather than invent one.

        Returns:
            Created approval request
        """
        # Calculate expiration time
        timeout = timeout_seconds or 300  # Default: 5 minutes
        expires_at = datetime.utcnow() + timedelta(seconds=timeout)

        # Create approval request
        approval_request = ApprovalRequest(
            id=uuid.uuid4(),
            account_id=account_id,
            tool_configuration_id=tool_configuration_id,
            approval_workflow_id=approval_workflow_id,
            execution_id=execution_id,
            tool_name=tool_name,
            tool_args=tool_args,
            agent_reasoning=agent_reasoning,
            managed_agent_id=managed_agent_id,
            runtime_session_id=runtime_session_id,
            managed_agent_name=managed_agent_name,
            rule_context=rule_context,
            status="pending",
            requested_at=datetime.utcnow(),
            expires_at=expires_at,
        )

        self.db.add(approval_request)
        await self.db.commit()
        await self.db.refresh(approval_request)

        # Broadcast creation event
        await self._broadcast_approval_update(approval_request, "created")

        # Log to audit trail (fire-and-forget)
        # Read correlation_id from context var if available (set by _call_tool)
        corr_id = None
        try:
            from preloop.services.dynamic_fastmcp import _correlation_id_var

            corr_id = _correlation_id_var.get(None)
        except Exception:
            # Correlation id is optional telemetry; ignore lookup failures.
            pass
        from preloop.utils.redaction import redact_dict

        _log_approval_lifecycle_async(
            account_id=account_id,
            approval_id=approval_request.id,
            event="created",
            tool_name=tool_name,
            execution_id=execution_id,
            correlation_id=corr_id,
            extra_details={
                "approval_workflow_id": str(approval_workflow_id),
                "tool_args": redact_dict(tool_args),
                "timeout_seconds": timeout,
                **({"rule_context": rule_context} if rule_context else {}),
            },
        )

        # Note: Notifications are sent via send_notifications() which is called
        # by create_and_notify(). Do NOT send notifications here to avoid duplicates.

        return approval_request

    async def get_approval_request(
        self, request_id: uuid.UUID
    ) -> Optional[ApprovalRequest]:
        """Get an approval request by ID.

        Args:
            request_id: Approval request ID

        Returns:
            Approval request or None if not found
        """
        return await get_approval_request_async(self.db, request_id=request_id)

    async def get_approval_request_for_update(
        self, request_id: uuid.UUID
    ) -> Optional[ApprovalRequest]:
        """Get an approval request by ID with row-level locking.

        Uses SELECT ... FOR UPDATE to prevent concurrent modifications.
        This should be used when updating the responses field for voting
        to avoid lost updates from concurrent votes.

        Args:
            request_id: Approval request ID

        Returns:
            Approval request or None if not found
        """
        return await get_approval_request_for_update_async(
            self.db, request_id=request_id
        )

    async def update_approval_request(
        self, request_id: uuid.UUID, update: ApprovalRequestUpdate
    ) -> Optional[ApprovalRequest]:
        """Update an approval request.

        Args:
            request_id: Approval request ID
            update: Update data

        Returns:
            Updated approval request or None if not found
        """
        approval_request = await self.get_approval_request(request_id)
        if not approval_request:
            return None

        # Update fields
        for field, value in update.model_dump(exclude_unset=True).items():
            setattr(approval_request, field, value)

        await self.db.commit()
        await self.db.refresh(approval_request)

        return approval_request

    async def _record_event(
        self,
        approval_request_id: uuid.UUID,
        account_id: uuid.UUID,
        event_type: str,
        detail: str,
        comment: Optional[str] = None,
        actor_id: Optional[uuid.UUID] = None,
    ) -> None:
        """Record an event in the approval event log for async polling."""
        try:
            from preloop.models.models.approval_event import ApprovalEvent

            event = ApprovalEvent(
                approval_request_id=approval_request_id,
                account_id=account_id,
                event_type=event_type,
                detail=detail,
                comment=comment,
                actor_id=actor_id,
            )
            self.db.add(event)
            await self.db.flush()
        except Exception as e:
            logger.warning(f"Failed to record approval event: {e}")

    _TERMINAL_STATUSES = frozenset({"approved", "declined", "cancelled", "expired"})

    async def _reject_if_not_actionable(
        self, approval_request: ApprovalRequest
    ) -> Optional[ApprovalRequest]:
        """Return the request if it can no longer be decided, else None.

        Must be called while holding the row lock. Guards approve/decline
        against two hazards the ``FOR UPDATE`` lock alone does not cover:

        * Double-decide (TOCTOU): the request was already resolved by a
          concurrent caller; re-deciding would flip a settled outcome (e.g.
          an already-approved-and-executed request being declined).
        * Stale approval: the request's deadline has passed. A caller that
          already timed out must not be able to approve it later via its
          token. Transition it to ``expired`` and return it.
        """
        if approval_request.status in self._TERMINAL_STATUSES:
            logger.info(
                "Approval request %s already %s; ignoring further decision",
                approval_request.id,
                approval_request.status,
            )
            return approval_request
        if (
            approval_request.expires_at
            and datetime.utcnow() > approval_request.expires_at
        ):
            logger.info(
                "Approval request %s is past expiry; marking expired",
                approval_request.id,
            )
            update = ApprovalRequestUpdate(
                status="expired", resolved_at=datetime.utcnow()
            )
            expired = await self.update_approval_request(approval_request.id, update)
            return expired or approval_request
        return None

    async def approve_request(
        self,
        request_id: uuid.UUID,
        comment: Optional[str] = None,
        user_id: Optional[uuid.UUID] = None,
    ) -> Optional[ApprovalRequest]:
        """Approve an approval request.

        If quorum (approvals_required > 1) is configured, this records an approval vote
        and only resolves the request when quorum is met.

        Uses row-level locking (SELECT ... FOR UPDATE) to prevent concurrent
        vote updates from overwriting each other.

        Args:
            request_id: Approval request ID
            comment: Optional comment from approver
            user_id: ID of the user approving (for quorum tracking)

        Returns:
            Updated approval request or None if not found
        """
        # Get approval request with row-level lock to prevent concurrent vote updates
        approval_request = await self.get_approval_request_for_update(request_id)
        if not approval_request:
            return None

        # Do not re-decide an already-resolved or expired request.
        guarded = await self._reject_if_not_actionable(approval_request)
        if guarded is not None:
            return guarded

        # Get the approval workflow to check quorum requirements
        approval_workflow = approval_request.approval_workflow
        approvals_required = (
            approval_workflow.approvals_required if approval_workflow else 1
        )

        # Record this vote
        responses = list(approval_request.responses or [])

        # Handle the vote based on whether we have user_id
        if user_id:
            user_id_str = str(user_id)
            already_voted = any(r.get("user_id") == user_id_str for r in responses)
            if already_voted:
                logger.warning(f"User {user_id} already voted on request {request_id}")
                return approval_request

            # Add the vote with user tracking
            responses.append(
                {
                    "user_id": user_id_str,
                    "decision": "approved",
                    "comment": comment,
                    "timestamp": datetime.utcnow().isoformat(),
                }
            )
        else:
            # No user_id (e.g., public token-based approval)
            # For quorum=1, resolve immediately.
            if approvals_required == 1:
                # Immediate resolution for single-approval workflows
                await self._record_event(
                    approval_request_id=request_id,
                    account_id=approval_request.account_id,
                    event_type="vote_received",
                    detail="Approval vote received (token-based)",
                    comment=comment,
                )
                await self._record_event(
                    approval_request_id=request_id,
                    account_id=approval_request.account_id,
                    event_type="approval_complete",
                    detail="Approval request approved",
                )
                update = ApprovalRequestUpdate(
                    status="approved",
                    approver_comment=comment,
                    resolved_at=datetime.utcnow(),
                )
                updated_request = await self.update_approval_request(request_id, update)
                if updated_request:
                    await self._broadcast_approval_update(updated_request, "approved")
                    _log_approval_lifecycle_async(
                        account_id=str(updated_request.account_id),
                        approval_id=updated_request.id,
                        event="approved",
                        tool_name=updated_request.tool_name,
                        approver_id=None,
                        reason=comment,
                        execution_id=updated_request.execution_id,
                    )
                return updated_request
            else:
                # For quorum > 1 without user_id, only allow ONE anonymous vote per request
                # to prevent a single actor from satisfying quorum by voting multiple times.
                # Check for ANY anonymous vote (approve OR decline) to prevent double-voting.
                anonymous_already_voted = any(
                    r.get("user_id") == "anonymous" for r in responses
                )
                if anonymous_already_voted:
                    logger.warning(
                        f"Duplicate anonymous vote attempt for request {request_id}. "
                        "Anonymous user already voted. Use authenticated endpoints for additional votes."
                    )
                    return approval_request

                # Add the single allowed anonymous vote
                responses.append(
                    {
                        "user_id": "anonymous",
                        "decision": "approved",
                        "comment": comment,
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                )
                logger.warning(
                    f"Anonymous approval for request {request_id} with quorum={approvals_required}. "
                    "Additional votes require authenticated endpoints."
                )

        # Count approvals
        approval_count = sum(1 for r in responses if r.get("decision") == "approved")

        # Record vote event
        voter_display = str(user_id) if user_id else "anonymous"
        await self._record_event(
            approval_request_id=request_id,
            account_id=approval_request.account_id,
            event_type="vote_received",
            detail=f"Approval vote received from {voter_display} ({approval_count}/{approvals_required})",
            comment=comment,
            actor_id=user_id,
        )

        # Check if quorum is met
        if approval_count >= approvals_required:
            # Quorum met - resolve as approved
            update = ApprovalRequestUpdate(
                status="approved",
                approver_comment=comment,
                resolved_at=datetime.utcnow(),
            )
            # Update responses in the database
            approval_request.responses = responses
            await self.db.commit()

            await self._record_event(
                approval_request_id=request_id,
                account_id=approval_request.account_id,
                event_type="approval_complete",
                detail=f"Approval request approved (quorum {approval_count}/{approvals_required} met)",
            )

            updated_request = await self.update_approval_request(request_id, update)
            if updated_request:
                await self._broadcast_approval_update(updated_request, "approved")
                _log_approval_lifecycle_async(
                    account_id=str(updated_request.account_id),
                    approval_id=updated_request.id,
                    event="approved",
                    tool_name=updated_request.tool_name,
                    approver_id=user_id,
                    reason=comment,
                    execution_id=updated_request.execution_id,
                    extra_details={
                        "approval_count": approval_count,
                        "approvals_required": approvals_required,
                    },
                )
            return updated_request
        else:
            # Quorum not met yet - just record the vote and broadcast progress
            approval_request.responses = responses
            await self.db.commit()
            await self.db.refresh(approval_request)

            # Broadcast vote received event (not full approval yet)
            await self._broadcast_approval_update(
                approval_request,
                "vote_received",
                extra_data={
                    "approval_count": approval_count,
                    "approvals_required": approvals_required,
                },
            )

            logger.info(
                f"Approval vote recorded for {request_id}: {approval_count}/{approvals_required}"
            )
            return approval_request

    async def decline_request(
        self,
        request_id: uuid.UUID,
        comment: Optional[str] = None,
        user_id: Optional[uuid.UUID] = None,
    ) -> Optional[ApprovalRequest]:
        """Decline an approval request.

        With quorum, a decline is recorded as a vote. The request is declined when:
        - All potential approvers have voted and quorum cannot be reached, OR
        - The number of declines makes it impossible to reach quorum

        Uses row-level locking (SELECT ... FOR UPDATE) to prevent concurrent
        vote updates from overwriting each other.

        Args:
            request_id: Approval request ID
            comment: Optional comment from approver
            user_id: ID of the user declining (for quorum tracking)

        Returns:
            Updated approval request or None if not found
        """
        # Get approval request with row-level lock to prevent concurrent vote updates
        approval_request = await self.get_approval_request_for_update(request_id)
        if not approval_request:
            return None

        # Do not re-decide an already-resolved or expired request.
        guarded = await self._reject_if_not_actionable(approval_request)
        if guarded is not None:
            return guarded

        # Get the approval workflow to check quorum requirements
        approval_workflow = approval_request.approval_workflow
        approvals_required = (
            approval_workflow.approvals_required if approval_workflow else 1
        )

        # Record this vote
        responses = list(approval_request.responses or [])

        # Handle the vote based on whether we have user_id
        if user_id:
            user_id_str = str(user_id)
            already_voted = any(r.get("user_id") == user_id_str for r in responses)
            if already_voted:
                logger.warning(f"User {user_id} already voted on request {request_id}")
                return approval_request

            # Add the vote with user tracking
            responses.append(
                {
                    "user_id": user_id_str,
                    "decision": "declined",
                    "comment": comment,
                    "timestamp": datetime.utcnow().isoformat(),
                }
            )
        else:
            # No user_id (e.g., public token-based decline)
            # For quorum=1, resolve immediately.
            if approvals_required == 1:
                # Immediate resolution for single-approval workflows
                await self._record_event(
                    approval_request_id=request_id,
                    account_id=approval_request.account_id,
                    event_type="vote_received",
                    detail="Decline vote received (token-based)",
                    comment=comment,
                )
                await self._record_event(
                    approval_request_id=request_id,
                    account_id=approval_request.account_id,
                    event_type="approval_complete",
                    detail="Approval request declined",
                )
                update = ApprovalRequestUpdate(
                    status="declined",
                    approver_comment=comment,
                    resolved_at=datetime.utcnow(),
                )
                updated_request = await self.update_approval_request(request_id, update)
                if updated_request:
                    await self._broadcast_approval_update(updated_request, "declined")
                    _log_approval_lifecycle_async(
                        account_id=str(updated_request.account_id),
                        approval_id=updated_request.id,
                        event="denied",
                        tool_name=updated_request.tool_name,
                        approver_id=None,
                        reason=comment,
                        execution_id=updated_request.execution_id,
                    )
                return updated_request
            else:
                # For quorum > 1 without user_id, only allow ONE anonymous vote per request
                # to prevent a single actor from forcing a decline by voting multiple times.
                # Check for ANY anonymous vote (approve OR decline) to prevent double-voting.
                anonymous_already_voted = any(
                    r.get("user_id") == "anonymous" for r in responses
                )
                if anonymous_already_voted:
                    logger.warning(
                        f"Duplicate anonymous vote attempt for request {request_id}. "
                        "Anonymous user already voted. Use authenticated endpoints for additional votes."
                    )
                    return approval_request

                # Add the single allowed anonymous vote
                responses.append(
                    {
                        "user_id": "anonymous",
                        "decision": "declined",
                        "comment": comment,
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                )
                logger.warning(
                    f"Anonymous decline for request {request_id} with quorum={approvals_required}. "
                    "Additional votes require authenticated endpoints."
                )

        # Count votes
        approval_count = sum(1 for r in responses if r.get("decision") == "approved")
        decline_count = sum(1 for r in responses if r.get("decision") == "declined")

        # Record decline vote event
        voter_display = str(user_id) if user_id else "anonymous"
        await self._record_event(
            approval_request_id=request_id,
            account_id=approval_request.account_id,
            event_type="vote_received",
            detail=f"Decline vote received from {voter_display} ({decline_count} decline(s))",
            comment=comment,
            actor_id=user_id,
        )

        # Get actual total approvers count (only reliable for direct user approvers)
        total_approvers, is_exact = self._count_total_approvers(approval_workflow)

        # Determine if we should resolve as declined
        should_decline = False
        remaining_voters = 0

        if is_exact:
            remaining_voters = total_approvers - len(responses)
            # Check if quorum is still possible
            can_reach_quorum = (approval_count + remaining_voters) >= approvals_required
            all_voted = len(responses) >= total_approvers
            should_decline = not can_reach_quorum or (
                all_voted and approval_count < approvals_required
            )
        else:
            # When we can't get an exact count (teams involved), use simpler rules:
            # 1. If approvals_required == 1, any decline resolves immediately
            # 2. For higher quorum, decline if decline_count >= approvals_required
            #    (meaning enough people have explicitly declined to block approval)
            if approvals_required == 1:
                should_decline = decline_count >= 1
            else:
                # If as many people have declined as required for approval,
                # it's a clear signal the request should be declined
                should_decline = decline_count >= approvals_required

        if should_decline:
            # Cannot reach quorum - resolve as declined
            update = ApprovalRequestUpdate(
                status="declined",
                approver_comment=comment,
                resolved_at=datetime.utcnow(),
            )
            # Update responses in the database
            approval_request.responses = responses
            await self.db.commit()

            await self._record_event(
                approval_request_id=request_id,
                account_id=approval_request.account_id,
                event_type="approval_complete",
                detail=f"Approval request declined (quorum cannot be reached: {decline_count} decline(s))",
                comment=comment,
            )

            updated_request = await self.update_approval_request(request_id, update)
            if updated_request:
                await self._broadcast_approval_update(updated_request, "declined")
                _log_approval_lifecycle_async(
                    account_id=str(updated_request.account_id),
                    approval_id=updated_request.id,
                    event="denied",
                    tool_name=updated_request.tool_name,
                    approver_id=user_id,
                    reason=comment,
                    execution_id=updated_request.execution_id,
                    extra_details={
                        "decline_count": decline_count,
                        "approvals_required": approvals_required,
                    },
                )
            return updated_request
        else:
            # Still possible to reach quorum - just record the vote
            approval_request.responses = responses
            await self.db.commit()
            await self.db.refresh(approval_request)

            # Broadcast vote received event
            extra_data = {"decline_count": decline_count}
            if is_exact:
                extra_data["remaining_voters"] = remaining_voters

            await self._broadcast_approval_update(
                approval_request,
                "vote_received",
                extra_data=extra_data,
            )

            if is_exact:
                logger.info(
                    f"Decline vote recorded for {request_id}: {decline_count} declines, "
                    f"{remaining_voters} remaining voters"
                )
            else:
                logger.info(
                    f"Decline vote recorded for {request_id}: {decline_count} declines "
                    "(team approvers involved, exact count unknown)"
                )
            return approval_request

    def _count_total_approvers(
        self, approval_workflow: ApprovalWorkflow
    ) -> tuple[int, bool]:
        """Count total potential approvers for a workflow.

        Args:
            approval_workflow: The approval workflow

        Returns:
            Tuple of (count, is_exact):
            - count: Total number of potential approvers
            - is_exact: True if count is exact (only direct user approvers),
                       False if teams are involved (count is unreliable)
        """
        if not approval_workflow:
            return (1, True)

        total = 0
        is_exact = True

        # Count direct user approvers (exact count)
        if approval_workflow.approver_user_ids:
            total += len(approval_workflow.approver_user_ids)

        # Team approvers make the count inexact
        # We can't reliably count team members without querying the database
        if (
            approval_workflow.approver_team_ids
            and len(approval_workflow.approver_team_ids) > 0
        ):
            is_exact = False
            # Don't add an estimate - the count is unreliable for decision-making

        return (max(total, 1), is_exact)

    async def _resolve_bypass(
        self,
        approval_workflow: ApprovalWorkflow,
        account_id: str,
        managed_agent_id: Optional[uuid.UUID],
        required_mode: str,
    ) -> Optional[ApprovalBypass]:
        """Return an active bypass covering *every* approver, or None.

        The unanimity rule is deliberate and is the main safety property here.
        A bypass belongs to one user. If a workflow requires several approvers,
        one of them muting their phone must not disable the gate for everybody
        else — that would let a single person silently revoke a control other
        people are relying on. So the bypass only takes effect when every
        approver on the workflow has an active bypass of at least the required
        strength. In the common single-approver case (an individual governing
        their own agents, which is the case this feature was built for) that
        reduces to "the one approver muted it".

        When no approvers can be resolved the request is treated as *not*
        bypassed. Failing closed is the only safe direction here.

        Args:
            approval_workflow: Workflow whose approvers gate the request.
            account_id: Owning account.
            managed_agent_id: Agent raising the request, if known.
            required_mode: Minimum strength; ``auto_approve`` matches only
                auto-approve bypasses, ``mute_notifications`` matches either.

        Returns:
            The bypass to attribute the decision to, or None.
        """
        try:
            approver_user_ids = await self._get_all_approver_user_ids(approval_workflow)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Could not resolve approvers for bypass check on workflow %s: %s; "
                "failing closed (no bypass applied)",
                approval_workflow.id,
                exc,
            )
            return None

        if not approver_user_ids:
            return None

        try:
            account_uuid = uuid.UUID(str(account_id))
        except (ValueError, AttributeError, TypeError):
            logger.warning(
                "Invalid account_id %r for bypass check; failing closed", account_id
            )
            return None

        winning: Optional[ApprovalBypass] = None
        for approver_id in approver_user_ids:
            bypass = await get_active_bypass_async(
                self.db,
                account_id=account_uuid,
                user_id=approver_id,
                managed_agent_id=managed_agent_id,
            )
            if bypass is None:
                return None
            if (
                required_mode == ApprovalBypassMode.AUTO_APPROVE
                and bypass.mode != ApprovalBypassMode.AUTO_APPROVE
            ):
                return None
            if winning is None:
                winning = bypass

        return winning

    async def _auto_approve_via_bypass(
        self,
        approval_request: ApprovalRequest,
        bypass: ApprovalBypass,
    ) -> ApprovalRequest:
        """Approve a request under a time-boxed bypass.

        Args:
            approval_request: The pending request to resolve.
            bypass: The bypass authorizing the auto-approval.

        Returns:
            The updated, approved approval request.
        """
        scope = "all agents" if bypass.managed_agent_id is None else "this agent"
        reason = (
            f"Auto-approved without review: an approval bypass for {scope} is "
            f"active until {bypass.expires_at.isoformat()}Z"
        )
        return await self._auto_approve_without_review(
            approval_request,
            reason_code=AutoApprovedReason.BYPASS,
            reason=reason,
            bypass=bypass,
        )

    async def _auto_approve_without_review(
        self,
        approval_request: ApprovalRequest,
        *,
        reason_code: str,
        reason: str,
        bypass: Optional[ApprovalBypass] = None,
    ) -> ApprovalRequest:
        """Approve a request with no human involved, recording it as such.

        Shared by every "nobody looked at this" path: time-boxed bypasses and
        the standing ``native_tool_approvals: "off"`` governance setting.

        The request row is kept — deliberately. Suppressing the notification is
        what relieves the user; suppressing the record would only destroy the
        evidence of what ran while nobody was watching, which is the opposite
        of what a governance product should do. The row is marked so no
        surface can mistake it for a human decision.

        Args:
            approval_request: The pending request to resolve.
            reason_code: An :class:`AutoApprovedReason` value stored in
                ``auto_approved_reason``; this is what makes the request
                machine-identifiable as unsupervised.
            reason: Human-readable explanation shown to operators.
            bypass: The authorizing bypass, when a time-boxed one exists.
                None for standing configured bypasses, which are authorized by
                the account's governance config rather than by a bypass row.

        Returns:
            The updated, approved approval request.
        """
        update = ApprovalRequestUpdate(
            status="approved",
            approver_comment=reason,
            resolved_at=datetime.utcnow(),
            decided_by_ai=False,
            auto_approved_reason=reason_code,
            auto_approval_bypass_id=bypass.id if bypass is not None else None,
        )
        updated_request = await self.update_approval_request(
            approval_request.id, update
        )
        if updated_request is None:  # pragma: no cover - defensive
            return approval_request

        if bypass is not None:
            try:
                await record_bypass_use_async(self.db, bypass_id=bypass.id)
            except Exception as exc:  # pragma: no cover - counter is advisory
                logger.warning(
                    "Failed to increment bypass %s usage counter: %s", bypass.id, exc
                )

        await self._broadcast_approval_update(updated_request, "approved")

        # Audit with no approver_id: nobody approved this. The [BYPASS] prefix
        # mirrors the [AI] convention so the audit timeline is scannable.
        _log_approval_lifecycle_async(
            account_id=str(updated_request.account_id),
            approval_id=updated_request.id,
            event="approved",
            tool_name=updated_request.tool_name,
            approver_id=None,
            reason=f"[BYPASS] {reason}",
            execution_id=updated_request.execution_id,
        )

        logger.info(
            "Approval request %s auto-approved without review "
            "(reason=%s, bypass=%s, tool=%s, agent=%s)",
            updated_request.id,
            reason_code,
            bypass.id if bypass is not None else None,
            updated_request.tool_name,
            updated_request.managed_agent_id,
        )
        return updated_request

    async def _auto_approve_request(
        self,
        request_id: uuid.UUID,
        reason: str,
        decided_by_ai: bool = True,
        ai_model: Optional[str] = None,
        ai_confidence: Optional[float] = None,
    ) -> Optional[ApprovalRequest]:
        """Auto-approve an approval request (typically by AI).

        Args:
            request_id: Approval request ID
            reason: Reason for the approval
            decided_by_ai: Whether this was decided by AI
            ai_model: The AI model that made the decision
            ai_confidence: The confidence score of the AI decision

        Returns:
            Updated approval request or None if not found
        """
        update = ApprovalRequestUpdate(
            status="approved",
            approver_comment=reason,
            resolved_at=datetime.utcnow(),
            decided_by_ai=decided_by_ai,
            ai_model=ai_model,
            ai_confidence=ai_confidence,
            ai_reasoning=reason,
        )
        updated_request = await self.update_approval_request(request_id, update)

        if updated_request:
            await self._broadcast_approval_update(updated_request, "approved")

            # Log to audit trail (fire-and-forget)
            _log_approval_lifecycle_async(
                account_id=str(updated_request.account_id),
                approval_id=updated_request.id,
                event="approved",
                tool_name=updated_request.tool_name,
                approver_id=None,  # AI decision, no human approver
                reason=f"[AI] {reason}" if decided_by_ai else reason,
                execution_id=updated_request.execution_id,
            )

        return updated_request

    async def _auto_deny_request(
        self,
        request_id: uuid.UUID,
        reason: str,
        decided_by_ai: bool = True,
        ai_model: Optional[str] = None,
        ai_confidence: Optional[float] = None,
    ) -> Optional[ApprovalRequest]:
        """Auto-deny an approval request (typically by AI).

        Args:
            request_id: Approval request ID
            reason: Reason for the denial
            decided_by_ai: Whether this was decided by AI
            ai_model: The AI model that made the decision
            ai_confidence: The confidence score of the AI decision

        Returns:
            Updated approval request or None if not found
        """
        update = ApprovalRequestUpdate(
            status="declined",
            approver_comment=reason,
            resolved_at=datetime.utcnow(),
            decided_by_ai=decided_by_ai,
            ai_model=ai_model,
            ai_confidence=ai_confidence,
            ai_reasoning=reason,
        )
        updated_request = await self.update_approval_request(request_id, update)

        if updated_request:
            await self._broadcast_approval_update(updated_request, "declined")

            # Log to audit trail (fire-and-forget)
            _log_approval_lifecycle_async(
                account_id=str(updated_request.account_id),
                approval_id=updated_request.id,
                event="denied",
                tool_name=updated_request.tool_name,
                approver_id=None,  # AI decision, no human approver
                reason=f"[AI] {reason}" if decided_by_ai else reason,
                execution_id=updated_request.execution_id,
            )

        return updated_request

    async def post_webhook_notification(
        self, approval_request: ApprovalRequest, approval_workflow: ApprovalWorkflow
    ) -> bool:
        """Post webhook notification for approval request.

        Args:
            approval_request: The approval request
            approval_workflow: The approval workflow with webhook config

        Returns:
            True if successful, False otherwise
        """
        cfg = _approval_config_dict(approval_workflow)
        webhook_url = cfg.get("webhook_url")
        if not isinstance(webhook_url, str) or not webhook_url.strip():
            webhook_url = None
        else:
            webhook_url = webhook_url.strip()
        bot_target = mattermost_bot_target(approval_workflow)

        if not webhook_url and not bot_target:
            error_msg = (
                mattermost_bot_config_error(approval_workflow)
                or "No webhook URL configured in approval workflow"
            )
            await self.update_approval_request(
                approval_request.id,
                ApprovalRequestUpdate(webhook_error=error_msg),
            )
            return False

        # Generate public approval links with token (no authentication required)
        token = approval_request.approval_token
        # All links go to the same approval page - user decides there
        approve_url = urljoin(
            self.base_url, f"/approval/{approval_request.id}?token={token}"
        )
        decline_url = urljoin(
            self.base_url, f"/approval/{approval_request.id}?token={token}"
        )
        view_url = urljoin(
            self.base_url, f"/approval/{approval_request.id}?token={token}"
        )

        # Format tool arguments for display (redact sensitive fields)
        from preloop.utils.redaction import redact_dict

        tool_args_redacted = redact_dict(approval_request.tool_args or {})
        tool_args_formatted = json.dumps(tool_args_redacted, indent=2)
        ask_text = (approval_request.summary or "").strip() or None
        headline = ask_text or f"Approval Required: {approval_request.tool_name}"

        # Create message based on approval type. Bot-token Mattermost
        # posts use the same chat markdown even when approval_type is
        # still ``standard`` (incoming DM webhooks 403).
        use_chat_payload = (
            approval_workflow.approval_type in ["slack", "mattermost"]
            or bot_target is not None
        )
        if use_chat_payload:
            # Build message text with all details — summary first when present
            if ask_text:
                message_text = f"⚠️ **{ask_text}**\n\n"
            else:
                message_text = (
                    f"⚠️ **Approval Required: {approval_request.tool_name}**\n\n"
                )
            message_text += f"**Tool:** `{approval_request.tool_name}`\n"
            message_text += f"**Status:** {approval_request.status.upper()}\n\n"

            if approval_request.agent_reasoning:
                message_text += (
                    f"**Agent Reasoning:**\n{approval_request.agent_reasoning}\n\n"
                )

            message_text += f"**Arguments:**\n```json\n{tool_args_formatted}\n```\n\n"
            message_text += "**Actions:**\n"
            message_text += f"• [✅ Approve]({approve_url})\n"
            message_text += f"• [❌ Decline]({decline_url})\n"
            message_text += f"• [👁️ View Details]({view_url})\n"

            # Mattermost/Slack compatible message with attachments
            message = {
                "text": message_text,
                "attachments": [
                    {
                        "color": "#f2c744",
                        "fallback": headline,
                        "title": f"⚠️ {headline}",
                        "title_link": view_url,
                        "fields": [
                            {
                                "title": "Tool",
                                "value": approval_request.tool_name,
                                "short": True,
                            },
                            {
                                "title": "Status",
                                "value": approval_request.status.upper(),
                                "short": True,
                            },
                        ],
                        "text": f"**Arguments:**\n```json\n{tool_args_formatted}\n```",
                        "actions": [
                            {
                                "type": "button",
                                "text": "✅ Approve",
                                "url": approve_url,
                                "style": "primary",
                            },
                            {
                                "type": "button",
                                "text": "❌ Decline",
                                "url": decline_url,
                                "style": "danger",
                            },
                            {
                                "type": "button",
                                "text": "👁️ View Details",
                                "url": view_url,
                            },
                        ],
                    }
                ],
            }

            # Add reasoning field if available
            if approval_request.agent_reasoning:
                message["attachments"][0]["fields"].insert(
                    0,
                    {
                        "title": "Agent Reasoning",
                        "value": approval_request.agent_reasoning,
                        "short": False,
                    },
                )
        else:
            # Generic webhook message (redact tool_args for external webhooks)
            message = {
                "type": "approval_request",
                "request_id": str(approval_request.id),
                "tool_name": approval_request.tool_name,
                "summary": ask_text,
                "tool_args": tool_args_redacted,
                "agent_reasoning": approval_request.agent_reasoning,
                "status": approval_request.status,
                "requested_at": approval_request.requested_at.isoformat(),
                "expires_at": (
                    approval_request.expires_at.isoformat()
                    if approval_request.expires_at
                    else None
                ),
                "actions": {
                    "approve": approve_url,
                    "decline": decline_url,
                    "view": view_url,
                },
            }

        # Incoming webhook when configured; otherwise Mattermost bot Posts API.
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                if webhook_url:
                    response = await client.post(
                        webhook_url,
                        json=message,
                        headers={"Content-Type": "application/json"},
                    )
                else:
                    assert bot_target is not None
                    base_url, token, channel_id = bot_target
                    post_body: Dict[str, Any] = {
                        "channel_id": channel_id,
                        "message": (
                            message["text"]
                            if isinstance(message, dict) and "text" in message
                            else json.dumps(message)
                        ),
                    }
                    if isinstance(message, dict) and message.get("attachments"):
                        post_body["props"] = {"attachments": message["attachments"]}
                    response = await client.post(
                        f"{base_url}/api/v4/posts",
                        json=post_body,
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {token}",
                        },
                    )
                response.raise_for_status()

            # Mark as posted
            await self.update_approval_request(
                approval_request.id,
                ApprovalRequestUpdate(webhook_posted_at=datetime.utcnow()),
            )
            return True

        except Exception as e:
            error_msg = f"Failed to post webhook: {str(e)}"
            await self.update_approval_request(
                approval_request.id,
                ApprovalRequestUpdate(webhook_error=error_msg),
            )
            return False

    async def create_and_notify(
        self,
        account_id: str,
        tool_configuration_id: uuid.UUID,
        approval_workflow: ApprovalWorkflow,
        tool_name: str,
        tool_args: Dict[str, Any],
        agent_reasoning: Optional[str] = None,
        execution_id: Optional[str] = None,
        user_id: Optional[uuid.UUID] = None,
        managed_agent_id: Optional[uuid.UUID] = None,
        runtime_session_id: Optional[uuid.UUID] = None,
        managed_agent_name: Optional[str] = None,
        standing_bypass_reason: Optional[str] = None,
        rule_context: Optional[Dict[str, Any]] = None,
    ) -> ApprovalRequest:
        """Create approval request and send notifications through configured channels.

        For AI-driven approval workflows, immediately evaluates the request using AI
        and auto-approves/denies based on the result and confidence threshold.

        Args:
            account_id: The account creating the request
            tool_configuration_id: Tool configuration ID
            approval_workflow: The approval workflow to use
            tool_name: Name of the tool being executed
            tool_args: Arguments passed to the tool
            agent_reasoning: Agent's reasoning for the tool call
            execution_id: Flow execution ID (if applicable)
            user_id: ID of the user who initiated the tool call
            standing_bypass_reason: When set, the caller has already established
                that a standing governance setting disables approval for this
                request (currently only ``native_tool_approvals: "off"``). The
                request is still created and recorded, then immediately
                auto-approved without notifying anyone. Must be an
                :class:`AutoApprovedReason` value.
            rule_context: Snapshot of the policy rule that required this
                approval, built by services/approval_rule_context.py. Passed
                straight through to the row; None when no rule was evaluated.

        Returns:
            Created approval request (may be already resolved if AI-driven)
        """
        # Store raw tool_args for execution; redact only for logging/notifications
        # (see ARCHITECTURE.md Redaction Policy)
        # Create approval request first
        approval_request = await self.create_approval_request(
            account_id=account_id,
            tool_configuration_id=tool_configuration_id,
            approval_workflow_id=approval_workflow.id,
            tool_name=tool_name,
            tool_args=tool_args,
            agent_reasoning=agent_reasoning,
            execution_id=execution_id,
            timeout_seconds=approval_workflow.timeout_seconds,
            managed_agent_id=managed_agent_id,
            runtime_session_id=runtime_session_id,
            managed_agent_name=managed_agent_name,
            rule_context=rule_context,
        )

        # Generate user-facing summary before any notifications fire.
        try:
            from preloop.models.db.session import get_db_session
            from preloop.services.approval_summary import generate_approval_summary

            sync_db = next(get_db_session())
            try:
                summary = await generate_approval_summary(
                    sync_db,
                    account_id=account_id,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    agent_reasoning=agent_reasoning,
                    managed_agent_name=managed_agent_name,
                )
            finally:
                sync_db.close()
            if summary:
                approval_request = await self.update_approval_request(
                    approval_request.id,
                    ApprovalRequestUpdate(summary=summary),
                )
        except Exception as summary_error:
            logger.warning(
                "Failed to attach approval summary for %s: %s",
                approval_request.id,
                summary_error,
                exc_info=True,
            )

        # Standing configured bypass. The operator has turned approvals off for
        # this subject in governance config, which is a durable setting rather
        # than a time-boxed escape hatch — so it is deliberately NOT expressed
        # as an ApprovalBypass row (those are always time-boxed, and inventing
        # a never-expiring one would make that guarantee a lie). The request is
        # still created, recorded and audited; only the notification and the
        # human gate are skipped.
        if standing_bypass_reason:
            return await self._auto_approve_without_review(
                approval_request,
                reason_code=standing_bypass_reason,
                reason=(
                    "Auto-approved without review: native tool approvals are "
                    "switched off for this agent in Preloop's governance settings"
                ),
            )

        # Time-boxed bypass check. Runs BEFORE the AI branch: the operator has
        # made an explicit, expiring, recorded decision to stop being asked, and
        # that outranks (and is far cheaper than) an LLM evaluation.
        try:
            bypass = await self._resolve_bypass(
                approval_workflow,
                account_id,
                managed_agent_id,
                required_mode=ApprovalBypassMode.AUTO_APPROVE,
            )
        except Exception as bypass_error:  # pragma: no cover - defensive
            # Fail closed: an error resolving the bypass must never silently
            # turn into an auto-approval.
            logger.error(
                "Bypass resolution failed for %s: %s; enforcing approval",
                approval_request.id,
                bypass_error,
                exc_info=True,
            )
            bypass = None

        if bypass is not None:
            return await self._auto_approve_via_bypass(approval_request, bypass)

        # Check if this is an AI-driven approval workflow
        if approval_workflow.approval_mode == "ai_driven":
            # Evaluate using AI
            logger.info(
                f"Evaluating approval request {approval_request.id} using AI "
                f"(workflow: {approval_workflow.name})"
            )

            ai_service = get_ai_approval_service()
            ai_result = await ai_service.evaluate(
                tool_name=tool_name,
                tool_args=tool_args,
                workflow=approval_workflow,
                context={
                    "execution_id": str(execution_id) if execution_id else None,
                    "user_id": str(user_id) if user_id else None,
                    "account_id": account_id,
                    "agent_reasoning": agent_reasoning,
                },
            )

            logger.info(
                f"AI evaluation for {approval_request.id}: "
                f"decision={ai_result.decision}, confidence={ai_result.confidence:.2f}"
            )

            # Apply the decision based on confidence threshold
            if ai_result.confidence >= approval_workflow.ai_confidence_threshold:
                if ai_result.decision == "approve":
                    # Auto-approve the request
                    logger.info(
                        f"AI auto-approving request {approval_request.id} "
                        f"(confidence: {ai_result.confidence:.2f})"
                    )
                    return await self._auto_approve_request(
                        request_id=approval_request.id,
                        reason=ai_result.reasoning,
                        decided_by_ai=True,
                        ai_model=ai_result.model_used,
                        ai_confidence=ai_result.confidence,
                    )
                elif ai_result.decision == "deny":
                    # Auto-deny the request
                    logger.info(
                        f"AI auto-denying request {approval_request.id} "
                        f"(confidence: {ai_result.confidence:.2f})"
                    )
                    return await self._auto_deny_request(
                        request_id=approval_request.id,
                        reason=ai_result.reasoning,
                        decided_by_ai=True,
                        ai_model=ai_result.model_used,
                        ai_confidence=ai_result.confidence,
                    )

            # If uncertain or low confidence, apply fallback behavior
            logger.info(
                f"AI uncertain or low confidence ({ai_result.confidence:.2f} < "
                f"{approval_workflow.ai_confidence_threshold}), "
                f"applying fallback: {approval_workflow.ai_fallback_behavior}"
            )

            if approval_workflow.ai_fallback_behavior == "approve":
                return await self._auto_approve_request(
                    request_id=approval_request.id,
                    reason=f"Fallback approval (AI uncertain): {ai_result.reasoning}",
                    decided_by_ai=True,
                    ai_model=ai_result.model_used,
                    ai_confidence=ai_result.confidence,
                )
            elif approval_workflow.ai_fallback_behavior == "deny":
                return await self._auto_deny_request(
                    request_id=approval_request.id,
                    reason=f"Fallback denial (AI uncertain): {ai_result.reasoning}",
                    decided_by_ai=True,
                    ai_model=ai_result.model_used,
                    ai_confidence=ai_result.confidence,
                )
            else:  # escalate (default)
                # Update the request with AI info but keep it pending for human review
                await self.update_approval_request(
                    approval_request.id,
                    ApprovalRequestUpdate(
                        ai_model=ai_result.model_used,
                        ai_confidence=ai_result.confidence,
                        ai_reasoning=f"Escalated to human review: {ai_result.reasoning}",
                    ),
                )

                # If escalation_workflow_id is set, load and use that workflow for notifications
                notification_workflow = approval_workflow
                if approval_workflow.escalation_workflow_id:
                    from preloop.models.crud.approval_workflow import (
                        get_approval_workflow_async,
                    )

                    escalation_workflow = await get_approval_workflow_async(
                        self.db, workflow_id=approval_workflow.escalation_workflow_id
                    )
                    if escalation_workflow:
                        logger.info(
                            f"Using escalation workflow '{escalation_workflow.name}' "
                            f"(id={escalation_workflow.id}) for human review notifications"
                        )
                        notification_workflow = escalation_workflow

                        # Update the approval request to reference the escalation workflow
                        # so approvers from that workflow are used
                        await self.update_approval_request(
                            approval_request.id,
                            ApprovalRequestUpdate(
                                approval_workflow_id=escalation_workflow.id,
                            ),
                        )
                    else:
                        logger.warning(
                            f"Escalation workflow {approval_workflow.escalation_workflow_id} "
                            f"not found, using original workflow for notifications"
                        )

                # Refresh the approval request to get updated fields
                approval_request = await self.get_approval_request(approval_request.id)

                # Send notifications for human review using the appropriate workflow
                await self.send_notifications(approval_request, notification_workflow)

                return approval_request

        # Standard (human) approval - send notifications
        await self.send_notifications(approval_request, approval_workflow)

        return approval_request

    async def send_notifications(
        self,
        approval_request: ApprovalRequest,
        approval_workflow: ApprovalWorkflow,
    ) -> Dict[str, Any]:
        """Send notifications for an approval request based on user preferences.

        Notification channels are determined by each user's notification
        preferences, not by the workflow. Push goes out immediately. Email
        for dual-channel users with ``stagger_email`` enabled is delayed
        until the request is still pending after
        ``APPROVAL_EMAIL_STAGGER_SECONDS``. Email-only users and users with
        stagger off still get email immediately. If push is unavailable,
        email falls back to immediate for every email-enabled approver.

        For webhook-based policies (slack, mattermost, webhook), those are
        sent as configured in the workflow since they are not per-user.

        Args:
            approval_request: The approval request to notify about
            approval_workflow: The approval workflow with approver configuration

        Returns:
            Dict with results per notification channel
        """
        results: Dict[str, Any] = {}

        # Guard against duplicate notifications - check if request is too old to be new
        # If request was created more than 30 seconds ago, skip notifications
        # (this handles cases where send_notifications is called multiple times)
        try:
            if approval_request.requested_at:
                request_age = (
                    datetime.utcnow() - approval_request.requested_at
                ).total_seconds()
                if request_age > 30:
                    logger.warning(
                        f"Skipping notifications for approval request {approval_request.id} - "
                        f"request is {request_age:.1f}s old (likely duplicate call)"
                    )
                    return {"skipped": True, "reason": "request_too_old"}
        except (TypeError, AttributeError):
            # In tests, requested_at might be a mock - just continue
            pass

        # Notification mute. Distinct from auto-approval: the request stays
        # pending and keeps blocking the agent, we simply stop buzzing people.
        # This is the control for "I am being spammed but I still want the
        # gate", and it is the safer of the two escape hatches.
        try:
            mute = await self._resolve_bypass(
                approval_workflow,
                str(approval_request.account_id),
                approval_request.managed_agent_id,
                required_mode=ApprovalBypassMode.MUTE_NOTIFICATIONS,
            )
        except Exception as mute_error:  # pragma: no cover - defensive
            logger.warning(
                "Mute resolution failed for %s: %s; notifying normally",
                approval_request.id,
                mute_error,
            )
            mute = None

        if mute is not None:
            logger.info(
                "Suppressing notifications for approval request %s under bypass "
                "%s (mode=%s); the request still requires approval",
                approval_request.id,
                mute.id,
                mute.mode,
            )
            return {
                "skipped": True,
                "reason": "notifications_muted",
                "bypass_id": str(mute.id),
                "expires_at": mute.expires_at.isoformat(),
            }

        # Recover correlation_id from the originating MCP context so the audit
        # entries we emit below land in the same timeline group.
        correlation_id = _current_correlation_id()
        approver_user_ids: List[uuid.UUID] = []
        try:
            approver_user_ids = await self._get_all_approver_user_ids(approval_workflow)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"Failed to resolve approver list for audit: {exc}")

        partition = await self._partition_approvers_for_stagger(approver_user_ids)

        # Push first so watch/mobile get the head start.
        try:
            push_result = await self._send_push_notification(
                approval_request, approval_workflow
            )
            results["mobile_push"] = push_result
        except Exception as e:
            logger.error(f"Failed to send push notifications: {str(e)}")
            results["mobile_push"] = {"success": False, "error": str(e)}

        self._audit_notification_result(
            approval_request=approval_request,
            channel="mobile_push",
            channel_result=results["mobile_push"],
            recipient_user_ids=partition["push_capable"] or approver_user_ids,
            correlation_id=correlation_id,
        )

        push_unavailable = results["mobile_push"].get("success") is False or bool(
            results["mobile_push"].get("no_devices")
        )

        if push_unavailable:
            # Never degrade delivery when push cannot work.
            immediate_email_ids = list(partition["all_email"])
            delayed_email_ids: List[uuid.UUID] = []
        else:
            immediate_email_ids = list(partition["email_only"]) + list(
                partition["both_immediate"]
            )
            delayed_email_ids = list(partition["both_stagger"])

        # Immediate email: recipients from the partition, or a no-op/legacy
        # call when nothing is staggered (empty workflow / push-only).
        send_immediate_email = bool(immediate_email_ids) or not delayed_email_ids
        if send_immediate_email:
            if immediate_email_ids:
                email_user_ids: Optional[List[uuid.UUID]] = immediate_email_ids
            elif approver_user_ids:
                # Approvers exist but none are due immediate email (e.g. push-only).
                email_user_ids = []
            else:
                # Preserve legacy "no approvers configured" reporting.
                email_user_ids = None
            try:
                email_result = await self._send_email_notification(
                    approval_request,
                    approval_workflow,
                    user_ids=email_user_ids,
                )
                results["email"] = email_result
            except Exception as e:
                logger.error(f"Failed to send email notifications: {str(e)}")
                results["email"] = {"success": False, "error": str(e)}

            self._audit_notification_result(
                approval_request=approval_request,
                channel="email",
                channel_result=results["email"],
                recipient_user_ids=immediate_email_ids or approver_user_ids,
                correlation_id=correlation_id,
            )

        if delayed_email_ids:
            delay_seconds = APPROVAL_EMAIL_STAGGER_SECONDS
            task = asyncio.create_task(
                _send_delayed_approval_email(
                    request_id=approval_request.id,
                    user_ids=delayed_email_ids,
                    delay_seconds=delay_seconds,
                    base_url=self.base_url,
                    correlation_id=correlation_id,
                    account_id=str(approval_request.account_id),
                    tool_name=approval_request.tool_name,
                )
            )
            _retain_delayed_email_task(task)
            results["email_delayed"] = {
                "scheduled": len(delayed_email_ids),
                "delay_seconds": delay_seconds,
            }
            self._audit_notification_result(
                approval_request=approval_request,
                channel="email",
                channel_result=results["email_delayed"],
                recipient_user_ids=delayed_email_ids,
                correlation_id=correlation_id,
            )

        # Handle webhook-based notifications (these are workflow-level, not per-user)
        # Derive notification channels from approval_type (the model field).
        # A ``standard`` workflow with Mattermost bot credentials still
        # posts: DMs reject incoming webhooks, so type alone is not enough.
        workflow_channels = (
            [approval_workflow.approval_type] if approval_workflow.approval_type else []
        )
        if not any(
            channel in ("slack", "mattermost", "webhook")
            for channel in workflow_channels
        ) and (
            mattermost_bot_target(approval_workflow)
            or mattermost_bot_config_error(approval_workflow)
        ):
            workflow_channels.append("mattermost")
        for channel in workflow_channels:
            if channel in ["slack", "mattermost", "webhook"]:
                try:
                    result = await self.post_webhook_notification(
                        approval_request, approval_workflow
                    )
                    results[channel] = {"success": result}
                except Exception as e:
                    logger.error(f"Failed to send {channel} notification: {str(e)}")
                    results[channel] = {"success": False, "error": str(e)}

                self._audit_notification_result(
                    approval_request=approval_request,
                    channel=channel,
                    channel_result=results[channel],
                    recipient_user_ids=None,
                    correlation_id=correlation_id,
                )

        return results

    async def _partition_approvers_for_stagger(
        self, approver_user_ids: List[uuid.UUID]
    ) -> Dict[str, List[uuid.UUID]]:
        """Partition approvers by email/push eligibility and stagger preference.

        Args:
            approver_user_ids: Approver user IDs to classify.

        Returns:
            Dict with keys ``push_capable``, ``email_only``, ``both_stagger``,
            ``both_immediate``, and ``all_email``.
        """

        def _partition() -> Dict[str, List[uuid.UUID]]:
            from preloop.models.crud import notification_preferences
            from preloop.models.db.session import get_db_session

            push_capable: List[uuid.UUID] = []
            email_only: List[uuid.UUID] = []
            both_stagger: List[uuid.UUID] = []
            both_immediate: List[uuid.UUID] = []
            all_email: List[uuid.UUID] = []

            sync_db = next(get_db_session())
            try:
                for user_id in approver_user_ids:
                    prefs = notification_preferences.get_by_user(sync_db, user_id)
                    if not prefs:
                        continue

                    has_email = bool(prefs.enable_email)
                    has_tokens = bool(prefs.get_device_tokens())
                    has_push = bool(prefs.enable_mobile_push and has_tokens)
                    stagger_on = bool(getattr(prefs, "stagger_email", True))

                    if has_push:
                        push_capable.append(user_id)
                    if has_email:
                        all_email.append(user_id)
                        if has_push:
                            if stagger_on:
                                both_stagger.append(user_id)
                            else:
                                both_immediate.append(user_id)
                        else:
                            email_only.append(user_id)
            finally:
                sync_db.close()

            logger.info(
                "Partitioned %s approvers: %s push-capable, %s email-only, "
                "%s both-stagger, %s both-immediate",
                len(approver_user_ids),
                len(push_capable),
                len(email_only),
                len(both_stagger),
                len(both_immediate),
            )
            return {
                "push_capable": push_capable,
                "email_only": email_only,
                "both_stagger": both_stagger,
                "both_immediate": both_immediate,
                "all_email": all_email,
            }

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_sync_db_executor, _partition)

    def _audit_notification_result(
        self,
        approval_request: ApprovalRequest,
        channel: str,
        channel_result: Dict[str, Any],
        recipient_user_ids: Optional[List[uuid.UUID]],
        correlation_id: Optional[str],
    ) -> None:
        """Persist a single channel fan-out result to the audit log.

        Translates the heterogeneous per-channel result dicts into a normalised
        ``status`` (sent / partial / failed / skipped / no_devices / scheduled)
        and forwards the structured counts to ``_log_approval_notification_async``.
        """
        try:
            sent = channel_result.get("sent")
            failed = channel_result.get("failed")
            skipped = channel_result.get("skipped")
            error = channel_result.get("error")
            success = channel_result.get("success")
            scheduled = channel_result.get("scheduled")

            if scheduled is not None:
                status = "scheduled"
            elif channel_result.get("no_devices"):
                status = "no_devices"
            elif success is False and (sent or 0) == 0:
                status = "failed"
            elif (failed or 0) > 0 and (sent or 0) > 0:
                status = "partial"
            elif success is True or (sent or 0) > 0:
                status = "sent"
            elif skipped:
                status = "skipped"
            else:
                status = "sent" if success else "failed"

            _log_approval_notification_async(
                account_id=str(approval_request.account_id),
                approval_id=approval_request.id,
                channel=channel,
                status=status,
                tool_name=approval_request.tool_name,
                recipient_user_ids=list(recipient_user_ids)
                if recipient_user_ids
                else None,
                sent_count=sent if isinstance(sent, int) else None,
                failed_count=failed if isinstance(failed, int) else None,
                skipped_count=skipped if isinstance(skipped, int) else None,
                error=str(error) if error else None,
                correlation_id=correlation_id,
                extra_details=(
                    {"delay_seconds": channel_result.get("delay_seconds")}
                    if scheduled is not None
                    else None
                ),
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"Failed to persist notification audit for {channel}: {exc}")

    async def _get_all_approver_user_ids(
        self, approval_workflow: ApprovalWorkflow
    ) -> List[uuid.UUID]:
        """Get all approver user IDs, expanding team memberships (async version).

        Args:
            approval_workflow: The approval workflow

        Returns:
            List of user IDs who can approve
        """
        from preloop.models.models.team import TeamMembership
        from sqlalchemy import select

        user_ids: Set[uuid.UUID] = set()

        # Add direct user approvers
        if approval_workflow.approver_user_ids:
            user_ids.update(approval_workflow.approver_user_ids)

        # Expand team approvers to individual users
        if approval_workflow.approver_team_ids:
            for team_id in approval_workflow.approver_team_ids:
                result = await self.db.execute(
                    select(TeamMembership.user_id).where(
                        TeamMembership.team_id == team_id
                    )
                )
                team_member_ids = result.scalars().all()
                user_ids.update(team_member_ids)

        return list(user_ids)

    async def _send_email_notification(
        self,
        approval_request: ApprovalRequest,
        approval_workflow: ApprovalWorkflow,
        user_ids: Optional[List[uuid.UUID]] = None,
    ) -> Dict[str, Any]:
        """Send email notification for approval request.

        Args:
            approval_request: The approval request
            approval_workflow: The approval workflow
            user_ids: Optional explicit recipient set. When None, all
                workflow approvers are considered.

        Returns:
            Dict with send result
        """
        from preloop.utils.email import send_approval_request_email
        from preloop.models.models.user import User
        from sqlalchemy import select

        # Get all approver user IDs (including team members) unless a subset
        # was provided by the stagger partitioner.
        if user_ids is not None:
            approver_user_ids = list(user_ids)
            if not approver_user_ids:
                return {"success": True, "sent": 0, "failed": 0, "skipped": 0}
        else:
            approver_user_ids = await self._get_all_approver_user_ids(approval_workflow)
            if not approver_user_ids:
                logger.warning(
                    f"No approvers configured for approval workflow "
                    f"{approval_workflow.id}"
                )
                return {"success": False, "error": "No approvers configured"}

        # Send email to each approver who has email notifications enabled
        from preloop.models.crud import notification_preferences
        from preloop.models.db.session import get_db_session

        # Batch fetch notification preferences in executor to avoid blocking event loop
        def _get_email_disabled_users(user_ids: List[uuid.UUID]) -> Set[uuid.UUID]:
            """Fetch users with email notifications disabled (runs in thread pool)."""
            disabled_users: Set[uuid.UUID] = set()
            sync_db = next(get_db_session())
            try:
                for uid in user_ids:
                    prefs = notification_preferences.get_by_user(sync_db, uid)
                    if prefs and not prefs.enable_email:
                        disabled_users.add(uid)
            finally:
                sync_db.close()
            return disabled_users

        loop = asyncio.get_running_loop()
        email_disabled_users = await loop.run_in_executor(
            _sync_db_executor, _get_email_disabled_users, approver_user_ids
        )

        sent_count = 0
        failed_count = 0
        skipped_count = 0

        for user_id in approver_user_ids:
            try:
                # Check if user has email notifications disabled (already fetched)
                if user_id in email_disabled_users:
                    logger.debug(
                        f"Skipping email for user {user_id} - email notifications disabled"
                    )
                    skipped_count += 1
                    continue

                # Get user email using async query
                result = await self.db.execute(select(User).where(User.id == user_id))
                user = result.scalar_one_or_none()

                if not user or not user.email:
                    logger.warning(f"User {user_id} not found or has no email")
                    failed_count += 1
                    continue

                # Generate approval URL with token
                token = approval_request.approval_token
                approval_url = (
                    f"{self.base_url}/approval/{approval_request.id}?token={token}"
                )

                # Send email (redact tool_args for display)
                from preloop.utils.redaction import redact_dict

                await send_approval_request_email(
                    user_email=user.email,
                    tool_name=approval_request.tool_name,
                    tool_args=redact_dict(approval_request.tool_args or {}),
                    approval_url=approval_url,
                    agent_reasoning=approval_request.agent_reasoning,
                    summary=approval_request.summary,
                )

                sent_count += 1
                logger.info(f"Sent approval email to {user.email}")

            except Exception as e:
                logger.error(
                    f"Failed to send email to user {user_id}: {str(e)}", exc_info=True
                )
                failed_count += 1

        return {
            "success": failed_count == 0,
            "sent": sent_count,
            "failed": failed_count,
            "skipped": skipped_count,
        }

    def _get_all_approver_user_ids_sync(
        self, approval_workflow: ApprovalWorkflow, sync_db
    ) -> List[uuid.UUID]:
        """Get all approver user IDs, expanding team memberships (sync version).

        Args:
            approval_workflow: The approval workflow
            sync_db: Synchronous database session

        Returns:
            List of user IDs who can approve
        """
        from preloop.models.models.team import TeamMembership

        user_ids: Set[uuid.UUID] = set()

        # Add direct user approvers
        if approval_workflow.approver_user_ids:
            user_ids.update(approval_workflow.approver_user_ids)

        # Expand team approvers to individual users
        if approval_workflow.approver_team_ids:
            for team_id in approval_workflow.approver_team_ids:
                team_members = (
                    sync_db.query(TeamMembership.user_id)
                    .filter(TeamMembership.team_id == team_id)
                    .all()
                )
                user_ids.update(member.user_id for member in team_members)

        return list(user_ids)

    async def _send_push_notification(
        self,
        approval_request: ApprovalRequest,
        approval_workflow: ApprovalWorkflow,
    ) -> Dict[str, Any]:
        """Send mobile push notification for approval request.

        This method supports two modes:
        1. Native push: Uses APNs/FCM directly (enterprise with credentials)
        2. Proxy push: Routes through preloop.ai proxy (OSS with API key)

        Args:
            approval_request: The approval request
            approval_workflow: The approval workflow

        Returns:
            Dict with send result
        """
        from preloop.models.crud import notification_preferences
        from preloop.services.push_notifications import (
            get_apns_service,
            NotificationPayloadBuilder,
            is_fcm_configured,
        )
        from preloop.services.push_proxy import (
            is_push_proxy_configured,
            send_push_via_proxy,
        )

        apns_service = get_apns_service()
        fcm_available = is_fcm_configured()
        use_proxy = not apns_service and is_push_proxy_configured()

        if not apns_service and not fcm_available and not use_proxy:
            logger.debug(
                "Push notifications not available (no APNs/FCM config, no proxy config)"
            )
            return {"success": False, "error": "Push notifications not configured"}

        # Run sync database operations in thread pool to avoid blocking event loop
        loop = asyncio.get_event_loop()

        def _get_approvers_and_tokens():
            """Sync function to get approvers and their device tokens."""
            from preloop.models.db.session import get_db_session

            # Get a fresh sync database session (not from async session)
            sync_db = next(get_db_session())
            try:
                # Get all approver user IDs (including team members)
                approver_user_ids = self._get_all_approver_user_ids_sync(
                    approval_workflow, sync_db
                )

                if not approver_user_ids:
                    return [], [], []

                # Collect user tokens for both iOS and Android
                ios_tokens_list = []
                android_tokens_list = []
                for user_id in approver_user_ids:
                    prefs = notification_preferences.get_by_user(sync_db, user_id)
                    if not prefs or not prefs.enable_mobile_push:
                        continue
                    if getattr(prefs, "notify_when_needed", True) is False:
                        continue

                    ios_tokens = prefs.get_device_tokens(platform="ios")
                    if ios_tokens:
                        for token in ios_tokens:
                            ios_tokens_list.append((user_id, token))

                    android_tokens = prefs.get_device_tokens(platform="android")
                    if android_tokens:
                        for token in android_tokens:
                            android_tokens_list.append((user_id, token))

                if _operator_present_at_machine(sync_db, approval_request):
                    logger.info(
                        "Skipping approval push for %s: operator is present "
                        "at the machine",
                        approval_request.id,
                    )
                    return approver_user_ids, [], []

                return approver_user_ids, ios_tokens_list, android_tokens_list
            finally:
                sync_db.close()

        approver_user_ids, ios_tokens, android_tokens = await loop.run_in_executor(
            _sync_db_executor, _get_approvers_and_tokens
        )

        if not approver_user_ids:
            logger.warning(
                f"No approvers configured for workflow {approval_workflow.id}"
            )
            return {"success": False, "error": "No approvers configured"}

        if not ios_tokens and not android_tokens:
            logger.info(
                f"No push-enabled devices for approvers in workflow {approval_workflow.id}"
            )
            return {"success": True, "sent": 0, "failed": 0, "no_devices": True}

        # Derive priority safely - ApprovalRequest doesn't have a priority field
        # Default to "medium" for normal requests
        priority_str = getattr(approval_request, "priority", None) or "medium"

        # Build notification payload (redact tool_args for push display)
        from preloop.utils.redaction import redact_dict

        payload = NotificationPayloadBuilder.new_approval_request(
            request_id=str(approval_request.id),
            tool_name=approval_request.tool_name,
            priority=priority_str,
            expires_at=approval_request.expires_at,
            agent_reasoning=approval_request.agent_reasoning,
            tool_args=redact_dict(approval_request.tool_args or {}),
            summary=approval_request.summary,
            rule_context=approval_request.rule_context,
        )

        apns_priority = 10 if priority_str in ["urgent", "high"] else 5
        fcm_priority = "high" if priority_str in ["urgent", "high"] else "normal"

        sent_count = 0
        failed_count = 0
        invalid_tokens = []
        failure_details: List[Dict[str, Any]] = []

        def _mask_device_token(token: str) -> str:
            """Return a short, non-sensitive token identifier for logs/emails."""
            cleaned = (token or "").strip()
            if not cleaned:
                return "<blank>"
            if len(cleaned) <= 12:
                return cleaned
            return f"{cleaned[:8]}...{cleaned[-4:]}"

        def _record_failure(
            *,
            platform: str,
            token: str,
            reason: str,
            status_code: Optional[int] = None,
            pruned: bool = False,
        ) -> None:
            failure_details.append(
                {
                    "platform": platform,
                    "token": _mask_device_token(token),
                    "status_code": status_code,
                    "reason": reason,
                    "pruned": pruned,
                }
            )

        def _is_prunable_apns_failure(
            *, token: str, status_code: int, error_reason: Optional[str]
        ) -> bool:
            cleaned = (token or "").strip()
            reason = (error_reason or "").strip()
            return (
                not cleaned
                or status_code == 410
                or reason
                in {
                    "BadDeviceToken",
                    "DeviceTokenNotForTopic",
                    "MissingDeviceToken",
                    "Unregistered",
                }
            )

        # Extract notification details for FCM
        notification_title = (
            payload.get("aps", {}).get("alert", {}).get("title", "Approval Required")
        )
        notification_body = (
            payload.get("aps", {})
            .get("alert", {})
            .get("body", "A request needs your attention")
        )
        notification_data = payload.get("data", {})

        # Send to iOS devices
        for user_id, token in ios_tokens:
            token = (token or "").strip()
            if not token:
                logger.warning(
                    "Skipping blank iOS push token for approval %s (user=%s)",
                    approval_request.id,
                    user_id,
                )
                invalid_tokens.append((user_id, token, "ios"))
                _record_failure(
                    platform="ios",
                    token=token,
                    status_code=400,
                    reason="MissingDeviceToken",
                    pruned=True,
                )
                continue
            try:
                if apns_service:
                    # Use native APNs (enterprise mode with credentials)
                    (
                        success,
                        status_code,
                        error_reason,
                    ) = await apns_service.send_notification(
                        device_token=token, payload=payload, priority=apns_priority
                    )

                    if success:
                        sent_count += 1
                    elif _is_prunable_apns_failure(
                        token=token,
                        status_code=status_code,
                        error_reason=error_reason,
                    ):
                        # Token is no longer valid or was stored blank/invalid.
                        invalid_tokens.append((user_id, token, "ios"))
                        _record_failure(
                            platform="ios",
                            token=token,
                            status_code=status_code,
                            reason=error_reason or "unknown_apns_error",
                            pruned=True,
                        )
                    else:
                        logger.warning(
                            f"APNs notification failed: status={status_code}, reason={error_reason}"
                        )
                        _record_failure(
                            platform="ios",
                            token=token,
                            status_code=status_code,
                            reason=error_reason or "unknown_apns_error",
                        )
                        failed_count += 1
                else:
                    # Use push proxy (OSS mode with API key)
                    result = await send_push_via_proxy(
                        platform="ios",
                        device_token=token,
                        title=notification_title,
                        body=notification_body,
                        data=notification_data,
                        priority="high" if apns_priority == 10 else "normal",
                    )

                    if result.get("success"):
                        sent_count += 1
                    else:
                        logger.warning(f"iOS push proxy failed: {result.get('error')}")
                        _record_failure(
                            platform="ios",
                            token=token,
                            reason=result.get("error") or "push_proxy_error",
                        )
                        failed_count += 1

            except Exception as e:
                logger.error(
                    f"iOS push notification error for token {token[:8]}...: {e}"
                )
                _record_failure(platform="ios", token=token, reason=str(e))
                failed_count += 1

        # Send to Android devices
        for user_id, token in android_tokens:
            token = (token or "").strip()
            if not token:
                logger.warning(
                    "Skipping blank Android push token for approval %s (user=%s)",
                    approval_request.id,
                    user_id,
                )
                invalid_tokens.append((user_id, token, "android"))
                _record_failure(
                    platform="android",
                    token=token,
                    reason="BlankDeviceToken",
                    pruned=True,
                )
                continue
            try:
                result = await _send_android_push_transport(
                    token=token,
                    title=notification_title,
                    body=notification_body,
                    data=notification_data,
                    priority=fcm_priority,
                    fcm_available=fcm_available,
                    use_proxy=use_proxy,
                )

                if result.get("success"):
                    sent_count += 1
                elif fcm_available and result.get(
                    "should_prune", result.get("invalid_token")
                ):
                    # Token is dead (unregistered, malformed, or minted by
                    # a different Firebase project). Drop it instead of
                    # retrying it on every future approval forever.
                    invalid_tokens.append((user_id, token, "android"))
                    logger.warning(
                        "Pruning dead Android push token: reason=%s "
                        "project_id=%s remediation=%s",
                        result.get("error_reason"),
                        result.get("project_id"),
                        result.get("remediation"),
                    )
                    _record_failure(
                        platform="android",
                        token=token,
                        reason=result.get("error_reason")
                        or result.get("error")
                        or "invalid_token",
                        pruned=True,
                    )
                elif fcm_available:
                    # Server-side or transient fault: keep the token.
                    logger.warning(
                        "FCM notification failed (token kept): reason=%s "
                        "retryable=%s error=%s",
                        result.get("error_reason"),
                        result.get("retryable"),
                        result.get("error"),
                    )
                    _record_failure(
                        platform="android",
                        token=token,
                        reason=result.get("error_reason")
                        or result.get("error")
                        or "unknown_fcm_error",
                    )
                    failed_count += 1
                else:
                    logger.warning(f"Android push proxy failed: {result.get('error')}")
                    _record_failure(
                        platform="android",
                        token=token,
                        reason=result.get("error") or "push_proxy_error",
                    )
                    failed_count += 1

            except Exception as e:
                logger.error(
                    f"Android push notification error for token {token[:8]}...: {e}"
                )
                _record_failure(platform="android", token=token, reason=str(e))
                failed_count += 1

        # Remove invalid tokens in background
        if invalid_tokens:

            def _remove_invalid_tokens():
                from preloop.models.db.session import get_db_session

                # Get a fresh sync database session (not from async session)
                sync_db = next(get_db_session())
                removed = 0
                failed = 0
                try:
                    # Commit per token so one bad row cannot roll back the
                    # whole batch and keep every other dead token alive until
                    # the next send.
                    for user_id, token, _ in invalid_tokens:
                        try:
                            notification_preferences.remove_device_token(
                                sync_db, user_id, token
                            )
                            sync_db.commit()
                            removed += 1
                        except Exception as e:
                            sync_db.rollback()
                            failed += 1
                            logger.error(
                                "Failed to remove invalid token %s... (user=%s): %s",
                                token[:8],
                                user_id,
                                e,
                            )
                finally:
                    sync_db.close()
                if failed:
                    logger.warning(
                        "Invalid-token pruning: removed=%d failed=%d "
                        "(failures retry on the next send that hits them)",
                        removed,
                        failed,
                    )
                else:
                    logger.info(f"Removed {removed} invalid device tokens")

            def _log_prune_outcome(fut: "concurrent.futures.Future") -> None:
                # The executor future was previously discarded, so a crash
                # before the function's own try block (session factory,
                # executor shutdown) vanished without a log line.
                if fut.cancelled():
                    logger.warning("Invalid-token pruning was cancelled")
                    return
                exc = fut.exception()
                if exc is not None:
                    logger.error(
                        "Invalid-token pruning crashed before completing: %s",
                        exc,
                    )

            # Run token cleanup in background
            prune_future = loop.run_in_executor(
                _sync_db_executor, _remove_invalid_tokens
            )
            prune_future.add_done_callback(_log_prune_outcome)

        # Notify admins if push notifications failed (and we had devices to send to)
        if failed_count > 0 and (ios_tokens or android_tokens):
            try:
                task_publisher = await get_task_publisher()
                if task_publisher:
                    failure_lines = [
                        (
                            f"- {detail['platform']} token={detail['token']} "
                            f"status={detail['status_code']} "
                            if detail.get("status_code") is not None
                            else f"- {detail['platform']} token={detail['token']} "
                        )
                        + f"reason={detail['reason']}"
                        + (" (token pruned)" if detail.get("pruned") else "")
                        for detail in failure_details
                        if not detail.get("pruned")
                    ]
                    if not failure_lines:
                        failure_lines = [
                            "- Non-prunable failures occurred, but no per-device details were captured."
                        ]
                    await task_publisher.publish_task(
                        "notify_admins",
                        subject="Push Notification Delivery Failed",
                        message=(
                            f"Push notifications failed for approval request.\n\n"
                            f"Request ID: {approval_request.id}\n"
                            f"Tool: {approval_request.tool_name}\n"
                            f"Sent: {sent_count}, Failed: {failed_count}\n"
                            f"iOS devices: {len(ios_tokens)}, Android devices: {len(android_tokens)}\n"
                            f"Invalid tokens pruned: {len(invalid_tokens)}\n\n"
                            f"Failure details:\n"
                            f"{chr(10).join(failure_lines)}\n\n"
                            f"Other devices may still have received the notification."
                        ),
                    )
            except Exception as e:
                logger.error(f"Failed to notify admins about push failure: {e}")

        error_summary = None
        if failed_count > 0:
            error_summary = (
                "; ".join(
                    detail["reason"]
                    for detail in failure_details
                    if not detail.get("pruned") and detail.get("reason")
                )
                or "Push delivery failed"
            )

        return {
            "success": failed_count == 0,
            "sent": sent_count,
            "failed": failed_count,
            "invalid_tokens_removed": len(invalid_tokens),
            "failure_details": failure_details,
            "error": error_summary,
        }

    async def wait_for_approval(
        self, request_id: uuid.UUID, poll_interval: float = 1.0
    ) -> ApprovalRequest:
        """Wait for an approval request to be resolved.

        This will poll the database until the request is approved, declined,
        expired, or cancelled. If escalation is configured and the request
        expires, escalation will be triggered and the timeout extended.

        Args:
            request_id: Approval request ID
            poll_interval: How often to poll (seconds)

        Returns:
            Final approval request

        Raises:
            TimeoutError: If request expires before being resolved (after escalation if configured)
        """
        while True:
            approval_request = await self.get_approval_request(request_id)
            if not approval_request:
                raise ValueError(f"Approval request {request_id} not found")

            # Check if resolved
            if approval_request.status in ["approved", "declined", "cancelled"]:
                return approval_request

            # Check if expired
            if (
                approval_request.expires_at
                and datetime.utcnow() > approval_request.expires_at
            ):
                # Get the approval workflow to check for escalation configuration
                approval_workflow = approval_request.approval_workflow

                # Debug logging for escalation check
                logger.info(
                    f"Checking escalation for request {request_id}: "
                    f"approval_workflow={approval_workflow}, "
                    f"approval_workflow_id={approval_request.approval_workflow_id}, "
                    f"escalation_user_ids={getattr(approval_workflow, 'escalation_user_ids', None) if approval_workflow else None}, "
                    f"escalation_team_ids={getattr(approval_workflow, 'escalation_team_ids', None) if approval_workflow else None}, "
                    f"escalation_triggered_at={approval_request.escalation_triggered_at}"
                )

                # Check if escalation is configured and hasn't been triggered yet
                has_escalation = approval_workflow and (
                    approval_workflow.escalation_user_ids
                    or approval_workflow.escalation_team_ids
                )
                escalation_already_triggered = (
                    approval_request.escalation_triggered_at is not None
                )

                logger.info(
                    f"Escalation decision for request {request_id}: "
                    f"has_escalation={has_escalation}, "
                    f"escalation_already_triggered={escalation_already_triggered}"
                )

                if has_escalation and not escalation_already_triggered:
                    # Trigger escalation
                    logger.info(
                        f"Triggering escalation for approval request {request_id}"
                    )

                    # Mark escalation as triggered
                    approval_request.escalation_triggered_at = datetime.utcnow()

                    # Extend the timeout - give escalation recipients the same amount of time
                    original_timeout = approval_workflow.timeout_seconds or 300
                    new_expires_at = datetime.utcnow() + timedelta(
                        seconds=original_timeout
                    )
                    approval_request.expires_at = new_expires_at

                    await self.db.commit()
                    await self.db.refresh(approval_request)

                    # Send escalation notifications
                    await self._send_escalation_notifications(
                        approval_request, approval_workflow
                    )

                    # Broadcast escalation event
                    await self._broadcast_approval_update(
                        approval_request,
                        "escalated",
                        extra_data={"new_expires_at": new_expires_at.isoformat()},
                    )

                    # Continue polling - don't expire yet
                    await asyncio.sleep(poll_interval)
                    continue

                # No escalation configured or already escalated - expire the request
                expired_request = await self.update_approval_request(
                    request_id, ApprovalRequestUpdate(status="expired")
                )
                # Broadcast expiration event
                if expired_request:
                    await self._broadcast_approval_update(expired_request, "expired")

                    # Log to audit trail (fire-and-forget)
                    _log_approval_lifecycle_async(
                        account_id=str(expired_request.account_id),
                        approval_id=expired_request.id,
                        event="expired",
                        tool_name=expired_request.tool_name,
                        reason="Request timed out without response",
                        execution_id=expired_request.execution_id,
                    )

                raise TimeoutError(
                    f"Approval request {request_id} expired without response"
                )

            # Wait before polling again
            await asyncio.sleep(poll_interval)

    async def _send_escalation_notifications(
        self,
        approval_request: ApprovalRequest,
        approval_workflow: ApprovalWorkflow,
    ) -> Dict[str, Any]:
        """Send notifications to escalation targets.

        Uses run_in_executor for sync DB/email operations to avoid blocking the event loop.

        Args:
            approval_request: The approval request that timed out
            approval_workflow: The approval workflow with escalation configuration

        Returns:
            Dict with notification results
        """
        from preloop.models.crud import notification_preferences
        from preloop.services.push_notifications import (
            get_apns_service,
            NotificationPayloadBuilder,
            is_fcm_configured,
        )
        from preloop.services.push_proxy import (
            is_push_proxy_configured,
            send_push_via_proxy,
        )

        logger.info(
            f"Sending escalation notifications for request {approval_request.id}"
        )

        await self._record_event(
            approval_request_id=approval_request.id,
            account_id=approval_request.account_id,
            event_type="escalation_triggered",
            detail="Approval request escalated due to timeout - notifying escalation contacts",
        )

        loop = asyncio.get_event_loop()

        # Capture values needed in sync function
        request_id = str(approval_request.id)
        tool_name = approval_request.tool_name
        approval_token = approval_request.approval_token
        base_url = self.base_url

        def _get_escalation_users_and_send_emails():
            """Sync function to get escalation users, their tokens, and send emails."""
            from preloop.models.db.session import get_db_session
            from preloop.models.crud import crud_team, crud_user
            from preloop.utils.email import send_escalation_email

            sync_db = next(get_db_session())
            try:
                # Collect escalation user IDs
                escalation_user_ids = set()

                if approval_workflow.escalation_user_ids:
                    escalation_user_ids.update(approval_workflow.escalation_user_ids)

                if approval_workflow.escalation_team_ids:
                    for team_id in approval_workflow.escalation_team_ids:
                        team_members = crud_team.get_team_members(sync_db, team_id)
                        escalation_user_ids.update(m.user_id for m in team_members)

                if not escalation_user_ids:
                    return set(), [], []

                # Send emails and collect device tokens
                ios_tokens_list = []
                android_tokens_list = []
                emails_sent = 0

                for user_id in escalation_user_ids:
                    user = crud_user.get(sync_db, id=user_id)
                    if user and user.email:
                        try:
                            send_escalation_email(
                                user_email=user.email,
                                tool_name=tool_name,
                                request_id=request_id,
                                approval_token=approval_token,
                                base_url=base_url,
                            )
                            emails_sent += 1
                        except Exception as e:
                            logger.error(
                                f"Failed to send escalation email to {user.email}: {e}"
                            )

                    # Get device tokens
                    prefs = notification_preferences.get_by_user(sync_db, user_id)
                    if prefs and prefs.enable_mobile_push:
                        ios_tokens = prefs.get_device_tokens(platform="ios")
                        if ios_tokens:
                            for token in ios_tokens:
                                ios_tokens_list.append((user_id, token))

                        android_tokens = prefs.get_device_tokens(platform="android")
                        if android_tokens:
                            for token in android_tokens:
                                android_tokens_list.append((user_id, token))

                logger.info(f"Sent {emails_sent} escalation emails")
                return escalation_user_ids, ios_tokens_list, android_tokens_list
            finally:
                sync_db.close()

        # Run sync operations in thread pool
        escalation_user_ids, ios_tokens, android_tokens = await loop.run_in_executor(
            _sync_db_executor, _get_escalation_users_and_send_emails
        )

        if not escalation_user_ids:
            logger.warning("No escalation targets configured")
            return {"success": False, "error": "No escalation targets"}

        # Send push notifications
        apns_service = get_apns_service()
        fcm_available = is_fcm_configured()
        use_proxy = not apns_service and is_push_proxy_configured()

        if not ios_tokens and not android_tokens:
            logger.info("No push-enabled devices for escalation users")
            return {
                "success": True,
                "escalation_users": len(escalation_user_ids),
                "push_sent": 0,
            }

        # Build escalation notification payload (redact tool_args for display)
        from preloop.utils.redaction import redact_dict

        payload = NotificationPayloadBuilder.new_approval_request(
            request_id=request_id,
            tool_name=tool_name,
            priority="high",  # Escalations are always high priority
            expires_at=approval_request.expires_at,
            agent_reasoning=f"ESCALATED: {approval_request.agent_reasoning or 'Original approvers did not respond'}",
            tool_args=redact_dict(approval_request.tool_args or {}),
            summary=approval_request.summary,
            rule_context=approval_request.rule_context,
        )

        sent_count = 0
        failed_count = 0

        # Extract notification details for FCM
        notification_title = (
            payload.get("aps", {})
            .get("alert", {})
            .get("title", "ESCALATED: Approval Required")
        )
        notification_body = (
            payload.get("aps", {})
            .get("alert", {})
            .get("body", "Original approvers did not respond")
        )
        notification_data = payload.get("data", {})

        # Send to iOS devices
        for _user_id, token in ios_tokens:
            try:
                if apns_service:
                    (
                        success,
                        status_code,
                        error_reason,
                    ) = await apns_service.send_notification(
                        device_token=token,
                        payload=payload,
                        priority=10,  # High priority
                    )
                    if success:
                        sent_count += 1
                    else:
                        logger.warning(
                            f"Escalation APNs failed: status={status_code}, reason={error_reason}"
                        )
                        failed_count += 1
                elif use_proxy:
                    result = await send_push_via_proxy(
                        device_token=token,
                        platform="ios",
                        title=notification_title,
                        body=notification_body,
                        data=notification_data,
                    )
                    if result.get("success"):
                        sent_count += 1
                    else:
                        failed_count += 1
            except Exception as e:
                logger.error(f"Escalation iOS push error: {e}")
                failed_count += 1

        # Send to Android devices. Transport dispatch is shared with the main
        # push path via _send_android_push_transport — this branch previously
        # hand-copied the calls and drifted (`device_token=` against a `token`
        # parameter), which is how escalation pushes came to fail silently.
        # Dead tokens are not pruned here; the main path prunes them on the
        # next regular send.
        for _user_id, token in android_tokens:
            try:
                result = await _send_android_push_transport(
                    token=token,
                    title=notification_title,
                    body=notification_body,
                    data=notification_data,
                    priority="high",
                    fcm_available=fcm_available,
                    use_proxy=use_proxy,
                )
                # The transport returns a dict, which is always truthy — even
                # for a failed send. Check the flag, as the main path does.
                if result.get("success"):
                    sent_count += 1
                else:
                    failed_count += 1
            except Exception as e:
                logger.error(f"Escalation Android push error: {e}")
                failed_count += 1

        logger.info(
            f"Escalation push notifications: sent={sent_count}, failed={failed_count}"
        )

        return {
            "success": True,
            "escalation_users": len(escalation_user_ids),
            "push_sent": sent_count,
            "push_failed": failed_count,
        }
