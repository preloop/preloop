"""In-session delivery of pending ask_user / request_approval questions.

When an agent session raises an ``ask_user`` (or ``request_approval``)
builtin call, the question is fanned out to the remote approval channels
(mobile, console, email). If the asking session's runtime also has an
active Agent Control connection (hermes-preloop / openclaw-preloop runtime
plugins, or a future sidecar), this module ALSO delivers the question into
the session's own surface as an auditable operator-notice turn, so the
human driving the session does not have to switch devices (issue #130).

Governance model — read before changing:

* The in-session delivery is a **notice with an actionable deep link**, not
  an answer channel. Anything that comes back over Agent Control
  (``command_result`` / ``agent_reply``) is produced by the *agent process*
  and MUST NOT be treated as the human's answer; doing so would let an
  agent answer its own question. Answers only enter through the governed
  approval endpoints (console, mobile, public token), preserving the single
  approval record, quorum semantics, and audit trail.
* First-answer-wins across channels is enforced by the approval record
  itself (``ApprovalService._reject_if_not_actionable`` guards
  double-decide); late answers on other surfaces receive an
  "already resolved" response.
* Delivery is strictly best-effort: any failure here must never block or
  fail the approval flow. Remote channels have already been notified.

The command envelope reuses the exact operator ``send_message`` plumbing
(persisted in ``agent_control_command`` before delivery, local WebSocket
first, NATS fan-out to peer pods as fallback) so runtime plugins need no
changes and delivery is audited end-to-end.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, List, Optional

from preloop.models.crud import (
    crud_agent_control_command,
    crud_managed_agent,
    crud_runtime_session,
    crud_runtime_session_activity,
)
from preloop.models.db.session import get_db_session
from preloop.schemas.agent_control import AgentControlEnvelope

logger = logging.getLogger(__name__)

#: Metadata marker so runtime plugins / auditors can recognise these turns.
QUESTION_NOTICE_KIND = "preloop_question_notice"

#: Fallback TTL for the persisted command when the approval request has no
#: explicit expiry (mirrors the default 300s approval timeout).
_DEFAULT_QUESTION_TTL_SECONDS = 300


def approval_console_url(base_url: str, request_id: Any) -> str:
    """Return the authenticated web-console deep link for one approval.

    Deliberately token-free: unlike the tokenized ``approval_url`` sent via
    trusted notification channels, this link is safe to show to the agent
    because acting on it requires an authenticated console session — the
    agent cannot use it to self-approve.
    """
    return f"{base_url.rstrip('/')}/approval/{request_id}"


def approval_mobile_deep_link(request_id: Any) -> str:
    """Return the mobile deep link (``preloop://approve/<id>``).

    Registered by the iOS app (``PreloopApp.swift``) and mirrored on
    Android; both navigate to the matching approval request detail view.
    """
    return f"preloop://approve/{request_id}"


def format_question_notice(
    *,
    tool_name: str,
    arguments: dict,
    console_url: str,
    mobile_link: str,
    request_id: Any,
) -> str:
    """Render the in-session operator notice for a pending question.

    The text is injected into the agent's conversation as the next
    user/operator turn by the runtime plugin, so it addresses both the human
    (who should answer via the deep link) and the agent (which must not
    answer on the human's behalf).
    """
    lines: List[str] = []
    # ``is_question`` is a server-controlled marker: it is injected into the
    # approval arguments by the server-side ask_user builtin
    # (initialize_mcp.py), never taken from agent-supplied input. It only
    # affects presentation (question wording vs. approval wording); an
    # unexpected caller setting it cannot change any governance behavior.
    if tool_name == "ask_user" or arguments.get("is_question"):
        question = str(arguments.get("question") or "").strip()
        lines.append("[Preloop] Question for the human operator:")
        if question:
            lines.append(f"  {question}")
        options = [str(o) for o in (arguments.get("options") or []) if str(o).strip()]
        if options:
            lines.append("  Options: " + " | ".join(options))
        context_text = str(arguments.get("context") or "").strip()
        if context_text:
            lines.append(f"  Context: {context_text}")
    else:
        operation = str(arguments.get("operation") or tool_name).strip()
        lines.append("[Preloop] Approval needed from the human operator:")
        lines.append(f"  Operation: {operation}")
        context_text = str(arguments.get("context") or "").strip()
        if context_text:
            lines.append(f"  Context: {context_text}")
    lines.append(f"  Answer here: {console_url}")
    lines.append(f"  On mobile: {mobile_link}")
    lines.append(
        "  (Agent: do NOT answer this yourself — relay it to the human. The "
        "answer must come through the Preloop approval surface; this session "
        f"is waiting on request {request_id}.)"
    )
    return "\n".join(lines)


def _agent_control_manager() -> Any:
    """Return the process-wide Agent Control connection manager.

    Imported lazily to avoid a service -> endpoint import cycle at module
    load; kept as a helper so tests can patch it.
    """
    from preloop.api.endpoints.agent_control import agent_control_manager

    return agent_control_manager


async def _publish_command_to_peers(envelope: AgentControlEnvelope) -> Optional[str]:
    """Best-effort NATS fan-out to the pod holding the agent's WebSocket."""
    from preloop.api.endpoints.agent_control import _publish_command

    return await _publish_command(envelope)


async def deliver_question_to_session(
    *,
    account_id: str,
    approval_request_id: Any,
    tool_name: str,
    arguments: dict,
    console_url: str,
    mobile_link: str,
    runtime_session_id: Optional[str],
    managed_agent_id: Optional[str],
    user_id: Optional[str] = None,
    expires_at: Optional[datetime] = None,
) -> bool:
    """Deliver a pending question into the asking session, if reachable.

    Returns True when the notice was handed to a live local Agent Control
    WebSocket or published for a peer pod to deliver; False when the asking
    session has no reachable control channel (headless run, plugin not
    installed, agent offline). Never raises: in-band delivery is additive
    and the remote channels were already notified.
    """
    if not runtime_session_id and not managed_agent_id:
        return False
    try:
        return await _deliver(
            account_id=str(account_id),
            approval_request_id=approval_request_id,
            tool_name=tool_name,
            arguments=arguments,
            console_url=console_url,
            mobile_link=mobile_link,
            runtime_session_id=runtime_session_id,
            managed_agent_id=managed_agent_id,
            user_id=user_id,
            expires_at=expires_at,
        )
    except Exception:
        logger.warning(
            "In-session question delivery failed for approval %s",
            approval_request_id,
            exc_info=True,
        )
        return False


async def _deliver(
    *,
    account_id: str,
    approval_request_id: Any,
    tool_name: str,
    arguments: dict,
    console_url: str,
    mobile_link: str,
    runtime_session_id: Optional[str],
    managed_agent_id: Optional[str],
    user_id: Optional[str],
    expires_at: Optional[datetime],
) -> bool:
    db = next(get_db_session())
    try:
        agent = None
        if managed_agent_id:
            agent = crud_managed_agent.get_for_account(
                db, account_id=account_id, agent_id=str(managed_agent_id)
            )
        if agent is None and runtime_session_id:
            session = crud_runtime_session.get_account_session(
                db,
                account_id=account_id,
                runtime_session_id=str(runtime_session_id),
            )
            if session is not None:
                agent = crud_managed_agent.get_by_source(
                    db,
                    account_id=account_id,
                    session_source_type=session.session_source_type,
                    session_source_id=session.session_source_id,
                )
        # ``runtime_session_id`` on the managed agent is the live-binding
        # marker: it is set while a control WebSocket is up (possibly on a
        # peer pod) and cleared on disconnect.
        if agent is None or agent.runtime_session_id is None:
            return False

        manager = _agent_control_manager()

        text = format_question_notice(
            tool_name=tool_name,
            arguments=arguments,
            console_url=console_url,
            mobile_link=mobile_link,
            request_id=approval_request_id,
        )
        envelope = AgentControlEnvelope(
            type="command",
            name="send_message",
            message_id=str(uuid.uuid4()),
            account_id=agent.account_id,
            managed_agent_id=agent.id,
            runtime_session_id=agent.runtime_session_id,
            session_source_type=agent.session_source_type,
            session_source_id=agent.session_source_id,
            timestamp=datetime.now(UTC),
            payload={
                "text": text,
                "metadata": {
                    "kind": QUESTION_NOTICE_KIND,
                    "approval_request_id": str(approval_request_id),
                    "tool_name": tool_name,
                    "source": "preloop_system",
                },
                "input_mode": "text",
                "session_mode": "current",
                "target_session_id": None,
                "start_new_session": False,
                "voice": {},
            },
        )

        # Persist before delivery (same contract as operator commands), with
        # the approval expiry as TTL so an already-expired question is never
        # redelivered to a reconnecting agent.
        command_expiry = expires_at or (
            datetime.now(UTC) + timedelta(seconds=_DEFAULT_QUESTION_TTL_SECONDS)
        )
        crud_agent_control_command.create_command(
            db,
            account_id=agent.account_id,
            managed_agent_id=agent.id,
            runtime_session_id=agent.runtime_session_id,
            command_id=envelope.message_id,
            envelope=envelope.model_dump(mode="json"),
            source="preloop_system",
            created_by_user_id=user_id,
            expires_at=command_expiry,
        )

        subject: Optional[str] = None
        # send_to_agent returns False when this pod does not hold the
        # agent's WebSocket; NATS fan-out below covers peer pods.
        delivered = await manager.send_to_agent(
            managed_agent_id=str(agent.id), envelope=envelope
        )
        if delivered:
            crud_agent_control_command.mark_delivered(
                db,
                account_id=str(agent.account_id),
                command_id=envelope.message_id,
                managed_agent_id=str(agent.id),
                delivered_at=datetime.now(UTC),
            )
        else:
            # The agent's WebSocket may be held by a peer pod: fan out via
            # NATS and leave the row pending; the holding pod marks delivery.
            subject = await _publish_command_to_peers(envelope)

        reached = delivered or subject is not None
        # Record the audited turn on the ASKING session (not the agent's
        # current binding) so the question shows up in that session's history.
        history_session_id = runtime_session_id or (
            str(agent.runtime_session_id) if agent.runtime_session_id else None
        )
        if history_session_id is not None:
            crud_runtime_session_activity.log_agent_control_message(
                db,
                account_id=agent.account_id,
                runtime_session_id=history_session_id,
                message=text,
                status="delivered"
                if delivered
                else ("queued" if reached else "failed"),
                metadata={
                    "command_id": envelope.message_id,
                    "managed_agent_id": str(agent.id),
                    "kind": QUESTION_NOTICE_KIND,
                    "approval_request_id": str(approval_request_id),
                    "local_delivery": delivered,
                    "published": subject is not None,
                    "subject": subject,
                },
            )
        return reached
    finally:
        db.close()
