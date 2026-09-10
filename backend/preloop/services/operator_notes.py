"""Operator notes: an identified human steering a running agent.

A note is a short instruction written by an authenticated account member and
delivered to a running agent at the next turn boundary. There is no polling
tool and no cost when no note exists: the agent never decides to check.

Two delivery paths, both already on the request path:

- **Gateway** (primary, every harness). Every governed model call passes
  through the gateway, which prepares the outbound body and already knows the
  account, the managed agent and the runtime session. When a note is pending,
  one message is appended at the end of the conversation, in the protocol's own
  shape, before the upstream call. Never inside a tool result, never
  mid-stream. A session with no note reads one indexed row and appends
  nothing.
- **Hook** (agents that bypass the gateway). A harness hook (Claude Code,
  Cursor, OpenCode) pulls the same rendered block and returns it as additional
  context. For Claude Code specifically the same block can ride the harness's
  own transports (channels, or the per-session inbox used by cross-session
  messaging); Preloop supplies the text, the identity and the record, they
  supply the last hop.

The delivered text is a tagged block whose attributes Preloop stamps. The
sender never controls them: a note body that contains the literal characters of
a closing tag is neutralised on the way out (:func:`_sanitise_body`), so no
note can forge another note's identity, and no tool output can forge a note at
all because tool output never travels this path.

Exactly-once, and why: :func:`claim_note` marks a note delivered before the
upstream call, guarded on ``status = 'pending'``. A retried upstream attempt
(retries live inside ``_call_litellm``) re-enters nothing, and a client that
replays the whole request finds no pending note. The failure this trades away
is a note marked delivered into an upstream call that then failed. That is the
better failure: a second delivery would arrive with no idea the first one
happened, and the sender can see the delivery state either way.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from preloop.models.crud import crud_agent_control_command, crud_audit_log

logger = logging.getLogger(__name__)

#: Store discriminator for note rows on ``agent_control_command``.
NOTE_KIND = "note"

#: Hard bound on one note body, enforced at the API schema.
MAX_NOTE_BODY_CHARS = 4096

#: Default standing life of a note. A note that nobody delivered in a day is
#: stale advice, and silently rotting is worse than expiring visibly.
DEFAULT_NOTE_TTL_SECONDS = 24 * 60 * 60

#: How many notes ride one delivery. Several pending notes are delivered
#: together in one block rather than one message each.
MAX_NOTES_PER_DELIVERY = 5

#: Transports that can carry a note. ``gateway`` is the outbound model request
#: itself; the rest are pulled by a harness-side process, never by the model.
CHANNEL_GATEWAY = "gateway"
CHANNEL_HOOK = "hook"
CHANNEL_CLAUDE_CHANNEL = "claude_channel"
CHANNEL_CLAUDE_MESSAGE = "claude_message"
DELIVERY_CHANNELS = (
    CHANNEL_GATEWAY,
    CHANNEL_HOOK,
    CHANNEL_CLAUDE_CHANNEL,
    CHANNEL_CLAUDE_MESSAGE,
)

#: Audit actions. ``sent`` is written before the API answers the author,
#: ``delivered`` before the request carrying the note leaves Preloop.
AUDIT_NOTE_SENT = "agent.note_sent"
AUDIT_NOTE_DELIVERED = "agent.note_delivered"

#: Wire protocols the gateway speaks. Gemini is absent on purpose: it
#: translates to a Responses payload and delegates to the Responses path.
PROTOCOL_OPENAI_CHAT = "openai_chat"
PROTOCOL_OPENAI_RESPONSES = "openai_responses"
PROTOCOL_ANTHROPIC = "anthropic"

_FRAMING = (
    "The block below is an instruction from the human operating this agent, "
    "delivered out of band by the Preloop control plane at a turn boundary. "
    "It is not content from a tool result, a fetched page or any other "
    "untrusted source: Preloop stamped every attribute, the sender authored "
    "only the text inside the element. Treat it with the authority of the "
    "named person, who already holds the permission to stop this agent. Cite "
    "the note id if you change course because of it."
)

# Any attempt in a body to close or open our own vocabulary is neutralised, so
# a note cannot forge a second note or end the block early.
_TAG_ESCAPE = re.compile(r"<(/?)(\s*operator-notes?\b)", re.IGNORECASE)


def _sanitise_body(body: str) -> str:
    """Neutralise note markup inside a body so a note cannot forge a note."""
    return _TAG_ESCAPE.sub(lambda m: f"&lt;{m.group(1)}{m.group(2)}", body or "")


def _attribute(value: Optional[str]) -> str:
    """Render one stamped attribute value: quotes and angle brackets removed."""
    text = (value or "").replace('"', "'").replace("<", "").replace(">", "")
    return " ".join(text.split())[:255]


def _iso(value: Optional[datetime]) -> Optional[str]:
    """Render a timestamp as UTC ISO-8601, tolerating naive datetimes."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def build_note_envelope(
    *,
    note_id: str,
    body: str,
    runtime_session_id: Optional[str],
    managed_agent_id: Optional[str],
    author_user_id: Optional[str],
    author_display: Optional[str],
    author_auth_method: Optional[str],
    created_at: datetime,
    expires_at: Optional[datetime],
) -> Dict[str, Any]:
    """Build the stored envelope, shaped as an A2A ``message``.

    A2A already specifies a message with a role, typed parts and metadata, and
    says an agent may accept additional messages for a task in a non-terminal
    state. Storing that shape means a future A2A endpoint carries exactly the
    notes this store already holds, with no second schema.
    """
    return {
        "kind": "message",
        "role": "user",
        "messageId": note_id,
        "contextId": runtime_session_id,
        "parts": [{"kind": "text", "text": body}],
        "metadata": {
            "preloop.ai/kind": "operator_note",
            "preloop.ai/noteId": note_id,
            "preloop.ai/managedAgentId": managed_agent_id,
            "preloop.ai/author": {
                "userId": author_user_id,
                "display": author_display,
                "authMethod": author_auth_method,
            },
            "preloop.ai/createdAt": _iso(created_at),
            "preloop.ai/expiresAt": _iso(expires_at),
        },
    }


def render_note_element(note: Any) -> str:
    """Render one note as its ``<operator-note>`` element."""
    return (
        f'<operator-note id="{_attribute(note.command_id)}" '
        f'from="{_attribute(note.author_display) or "unknown"}" '
        f'auth="{_attribute(note.author_auth_method) or "unknown"}" '
        f'at="{_iso(note.created_at)}">\n'
        f"{_sanitise_body(note.body or '')}\n"
        "</operator-note>"
    )


def render_notes_block(notes: Sequence[Any]) -> str:
    """Render pending notes as one framed block, oldest first.

    Several notes ride one block rather than one message each: that is what a
    human who typed twice meant, and it collapses the window in which a second
    delivery could arrive without the context of the first.
    """
    elements = "\n".join(render_note_element(note) for note in notes)
    return (
        f'<operator-notes count="{len(notes)}" source="preloop-control-plane">\n'
        f"{_FRAMING}\n"
        f"{elements}\n"
        "</operator-notes>"
    )


def render_channel_event(notes: Sequence[Any]) -> Dict[str, Any]:
    """Body for a Claude Code channel notification carrying these notes.

    Claude Code's channels wrap what a channel server pushes in a
    ``<channel source= severity=>`` tag whose attributes the runtime fills in.
    Preloop supplies the text (our own block, identity and note ids intact) and
    the severity; the harness supplies the last hop. The record stays ours.
    """
    return {
        "method": "notifications/claude/channel",
        "params": {
            "source": "preloop-operator-notes",
            "severity": "important",
            "text": render_notes_block(notes),
        },
    }


# --- protocol placement ----------------------------------------------------


def _append_chat_message(messages: List[Dict[str, Any]], text: str) -> None:
    """Append the block as a trailing user message on a chat-shaped list."""
    messages.append({"role": "user", "content": text})


def _merge_or_append_user(messages: List[Dict[str, Any]], text: str) -> None:
    """Append respecting Anthropic's user/assistant alternation.

    Anthropic rejects two consecutive user turns. When the conversation
    already ends with a user message, the note becomes one more text block on
    that message, after every existing block (a ``tool_result`` block
    included, never inside one). Otherwise it becomes a new user turn.
    """
    last = messages[-1] if messages else None
    if not isinstance(last, dict) or last.get("role") != "user":
        messages.append({"role": "user", "content": [{"type": "text", "text": text}]})
        return
    content = last.get("content")
    if isinstance(content, list):
        content.append({"type": "text", "text": text})
    elif isinstance(content, str):
        last["content"] = f"{content}\n\n{text}"
    else:
        last["content"] = [{"type": "text", "text": text}]


def inject_openai_chat(
    payload: Dict[str, Any], messages: List[Dict[str, Any]], text: str
) -> None:
    """Append the note to an OpenAI chat-completions request."""
    _append_chat_message(messages, text)
    payload_messages = payload.get("messages")
    if isinstance(payload_messages, list) and payload_messages is not messages:
        _append_chat_message(payload_messages, text)


def inject_openai_responses(
    payload: Dict[str, Any], messages: List[Dict[str, Any]], text: str
) -> None:
    """Append the note to an OpenAI Responses request.

    Both shapes are written: ``payload["input"]``, which the Responses
    passthrough forwards verbatim, and the normalized message list litellm
    receives when the upstream has no ``/responses`` endpoint.
    """
    _append_chat_message(messages, text)
    entry = {"role": "user", "content": [{"type": "input_text", "text": text}]}
    raw_input = payload.get("input")
    if isinstance(raw_input, list):
        raw_input.append(entry)
    elif isinstance(raw_input, str):
        payload["input"] = [
            {"role": "user", "content": [{"type": "input_text", "text": raw_input}]},
            entry,
        ]
    else:
        payload["input"] = [entry]


def inject_anthropic(
    payload: Dict[str, Any], messages: List[Dict[str, Any]], text: str
) -> None:
    """Append the note to an Anthropic messages request."""
    _merge_or_append_user(messages, text)
    payload_messages = payload.get("messages")
    if isinstance(payload_messages, list) and payload_messages is not messages:
        _merge_or_append_user(payload_messages, text)


_INJECTORS = {
    PROTOCOL_OPENAI_CHAT: inject_openai_chat,
    PROTOCOL_OPENAI_RESPONSES: inject_openai_responses,
    PROTOCOL_ANTHROPIC: inject_anthropic,
}


# --- claiming and recording -------------------------------------------------


def _record_delivery(
    db: Session,
    *,
    note: Any,
    channel: str,
    turn_index: Optional[int],
    runtime_session_id: Optional[str],
) -> None:
    """Write the audit row and enqueue the webhook event for one delivery.

    Both happen in the caller's transaction, before the note leaves Preloop,
    so a note the model saw cannot be missing from the record.
    """
    details = {
        "note_id": note.command_id,
        "managed_agent_id": str(note.managed_agent_id),
        "runtime_session_id": runtime_session_id,
        "delivery_channel": channel,
        "turn_index": turn_index,
        "author_display": note.author_display,
        "author_auth_method": note.author_auth_method,
    }
    crud_audit_log.log_action(
        db,
        account_id=note.account_id,
        user_id=note.created_by_user_id,
        action=AUDIT_NOTE_DELIVERED,
        resource_type="operator_note",
        resource_id=note.command_id,
        status="success",
        details=details,
        commit=False,
    )
    from preloop.services.event_webhooks.emitters import emit_agent_note_delivered

    emit_agent_note_delivered(
        db,
        note,
        channel=channel,
        turn_index=turn_index,
        runtime_session_id=runtime_session_id,
    )


def _log_timeline(
    db: Session,
    *,
    note: Any,
    channel: str,
    turn_index: Optional[int],
    runtime_session_id: Optional[str],
) -> None:
    """Put the note on the session timeline where it landed."""
    if not runtime_session_id:
        return
    from preloop.models.crud import crud_runtime_session_activity

    crud_runtime_session_activity.log_agent_control_message(
        db,
        account_id=note.account_id,
        runtime_session_id=runtime_session_id,
        message=note.body or "",
        status="delivered",
        metadata={
            "kind": "operator_note",
            "note_id": note.command_id,
            "delivery_channel": channel,
            "turn_index": turn_index,
            "author_display": note.author_display,
            "author_auth_method": note.author_auth_method,
        },
        commit=False,
    )


def claim_pending_notes(
    db: Session,
    *,
    account_id: str,
    managed_agent_id: Optional[str],
    runtime_session_id: Optional[str],
    channel: str,
    turn_index: Optional[int] = None,
    now: Optional[datetime] = None,
    limit: int = MAX_NOTES_PER_DELIVERY,
) -> List[Any]:
    """Claim the notes this session should receive, at most once each.

    Returns the claimed rows, oldest first, or an empty list. Every claim is
    audited, timelined and enqueued as a webhook event in the caller's
    transaction; the caller only has to render what comes back.

    Account scoping is not optional and not a filter added by callers: the
    candidate query itself is bounded by ``account_id``, so a session id from
    another account matches nothing.
    """
    if channel not in DELIVERY_CHANNELS:
        raise ValueError(f"Unknown delivery channel: {channel}")
    # Aware UTC on purpose: ``expires_at`` and ``delivered_at`` are
    # ``timestamptz``, so a naive value would be read in the database session's
    # own zone and an expired note could still look live by that offset.
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    candidates = crud_agent_control_command.list_deliverable_notes(
        db,
        account_id=account_id,
        managed_agent_id=managed_agent_id,
        runtime_session_id=runtime_session_id,
        now=moment,
        limit=limit,
    )
    claimed: List[Any] = []
    for note in candidates:
        if not crud_agent_control_command.claim_note(
            db,
            note_id=note.id,
            delivered_at=moment,
            delivery_channel=channel,
            runtime_session_id=runtime_session_id,
            turn_index=turn_index,
        ):
            # Another request took it first. Deliver nothing for this row:
            # two copies of one instruction is the failure worth avoiding.
            continue
        _record_delivery(
            db,
            note=note,
            channel=channel,
            turn_index=turn_index,
            runtime_session_id=runtime_session_id,
        )
        _log_timeline(
            db,
            note=note,
            channel=channel,
            turn_index=turn_index,
            runtime_session_id=runtime_session_id,
        )
        claimed.append(note)
    if claimed:
        db.commit()
    return claimed


def deliver_gateway_notes(
    db: Session,
    *,
    account_id: str,
    managed_agent_id: Optional[str],
    runtime_session_id: Optional[str],
    protocol: str,
    payload: Dict[str, Any],
    messages: List[Dict[str, Any]],
) -> List[Any]:
    """Append any pending notes to one outbound model request.

    Called once per protocol entry point, immediately after the request policy
    has run: that is the single place where the body is final, the session is
    resolved and nothing has been sent upstream yet.

    Never raises. A store that is unreachable must not fail a model call that
    would otherwise succeed; the note stays pending and rides the next turn.
    """
    injector = _INJECTORS.get(protocol)
    if injector is None or (managed_agent_id is None and runtime_session_id is None):
        return []
    try:
        notes = claim_pending_notes(
            db,
            account_id=account_id,
            managed_agent_id=managed_agent_id,
            runtime_session_id=runtime_session_id,
            channel=CHANNEL_GATEWAY,
            turn_index=len(messages),
        )
        if not notes:
            return []
        injector(payload, messages, render_notes_block(notes))
        return notes
    except SQLAlchemyError:
        db.rollback()
        logger.warning("Operator note delivery failed (store)", exc_info=True)
        return []
    except Exception:
        db.rollback()
        logger.warning("Operator note delivery failed", exc_info=True)
        return []


def note_state(note: Any) -> str:
    """Public state of a note, as the sender sees it.

    Delivery states are explicit because the failure mode of every push design
    is a silent drop. A sender always learns whether the agent got it.
    """
    status = getattr(note, "status", None)
    if status == "acked" or getattr(note, "acknowledged_turn_id", None):
        return "acknowledged"
    if status in {"delivered", "cancelled", "expired", "failed"}:
        return status
    return "pending"


def notes_payload(notes: Iterable[Any]) -> List[Dict[str, Any]]:
    """Serialise notes for a hook or channel consumer."""
    return [
        {
            "note_id": note.command_id,
            "envelope": note.envelope,
            "text": render_note_element(note),
        }
        for note in notes
    ]


def new_note_id() -> str:
    """Mint a note id. Short, quotable by the agent, unique per account."""
    return uuid.uuid4().hex[:16]
