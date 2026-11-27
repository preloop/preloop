"""Notification payload templates for Apple Push Notifications."""

import json
from datetime import datetime
from typing import Any, Dict, Optional

from preloop.models.schemas.approval_request import classify_approval_risk

# APNs rejects alert payloads over 4096 bytes with 413 PayloadTooLarge. Keep a
# margin for the proxy envelope and JSON encoder differences.
APNS_MAX_PAYLOAD_BYTES = 4096
PUSH_PAYLOAD_BUDGET_BYTES = 3584


def compact_runner_flow(runner_flow: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only the runner flow facts a notification card renders.

    The full snapshot (repositories, directory, labels) can exceed the APNs
    payload limit once it is duplicated into the ``data`` block. Clients fetch
    the rest from the API when the request is opened.

    Args:
        runner_flow: ``rule_context.runner_flow`` snapshot.

    Returns:
        A small dict with the same key shape.
    """
    compact: Dict[str, Any] = {}
    runner = runner_flow.get("runner")
    if isinstance(runner, dict) and runner.get("name"):
        compact["runner"] = {"name": str(runner["name"])[:80]}
    flow = runner_flow.get("flow")
    if isinstance(flow, dict):
        compact["flow"] = {
            key: str(flow[key])[:120] for key in ("id", "name") if flow.get(key)
        }
    for key in ("harness", "model", "scope"):
        if runner_flow.get(key):
            compact[key] = str(runner_flow[key])[:80]
    return compact


def payload_size(payload: Dict[str, Any]) -> int:
    """Return the encoded JSON size of a push payload in bytes.

    Args:
        payload: Push payload dict.

    Returns:
        UTF-8 byte length of the compact JSON encoding.
    """
    return len(
        json.dumps(
            payload, separators=(",", ":"), ensure_ascii=False, default=str
        ).encode("utf-8")
    )


def _fit_payload(payload: Dict[str, Any], budget: int) -> Dict[str, Any]:
    """Drop optional fields, then clip text, until the payload fits.

    Args:
        payload: Approval push payload with ``aps`` and ``data`` blocks.
        budget: Maximum encoded size in bytes.

    Returns:
        The same payload, trimmed in place.
    """
    for key in ("runner_flow", "summary", "question", "rule_name", "agent_name"):
        if payload_size(payload) <= budget:
            return payload
        payload.pop(key, None)
        if isinstance(payload.get("data"), dict):
            payload["data"].pop(key, None)
    alert = payload.get("aps", {}).get("alert", {})
    for key in ("body", "subtitle", "title"):
        while payload_size(payload) > budget and len(alert.get(key) or "") > 20:
            text = alert[key]
            alert[key] = text[: max(20, len(text) // 2)].rstrip() + "…"
            if isinstance(payload.get("data"), dict) and key in payload["data"]:
                payload["data"][key] = alert[key]
    return payload


class NotificationPayloadBuilder:
    """Builder for APNs notification payloads.

    Follows Apple's APNs payload structure with best practices.
    """

    @staticmethod
    def budget_limit_exceeded(
        *,
        limit_type: str,
        subject_type: str,
        period: str,
        limit_usd: float,
        current_spend_usd: float,
        body: str,
    ) -> Dict[str, Any]:
        """Build payload for budget soft/hard limit alerts."""
        title = f"Budget {limit_type.capitalize()} Limit Exceeded"
        custom_data = {
            "type": "budget_limit_exceeded",
            "limit_type": limit_type,
            "subject_type": subject_type,
            "period": period,
            "limit_usd": limit_usd,
            "current_spend_usd": current_spend_usd,
        }
        return {
            "aps": {
                "alert": {
                    "title": title,
                    "subtitle": f"{subject_type} · {period}",
                    "body": body,
                },
                "sound": "default",
                "category": "BUDGET_ALERT",
                "thread-id": "budget-alerts",
                "interruption-level": "time-sensitive"
                if limit_type == "hard"
                else "active",
            },
            **custom_data,
            "data": custom_data,
        }

    @staticmethod
    def new_approval_request(
        request_id: str,
        tool_name: str,
        priority: str = "medium",
        expires_at: Optional[datetime] = None,
        agent_reasoning: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
        summary: Optional[str] = None,
        rule_context: Optional[Dict[str, Any]] = None,
        agent_name: Optional[str] = None,
        runtime_session_id: Optional[str] = None,
        external_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build payload for new approval request.

        This is the PRIMARY notification type.

        Args:
            request_id: Approval request UUID.
            tool_name: Name of tool requiring approval.
            priority: 'low', 'medium', 'high', or 'urgent'.
            expires_at: Request expiration time.
            agent_reasoning: Agent's explanation (truncated to 100 chars).
            tool_args: Tool arguments to show in notification body.
            summary: Plain-language ask shown as the notification body when set.
            rule_context: Snapshot of the rule that gated the call. Only the
                rule NAME travels in a push: an expression does not fit and
                truncating one would misrepresent it. Clients fetch the full
                context from the API when the request is opened.
            agent_name: The agent that asked, when known. A push is decided on
                a phone with no page to open, so who asked belongs in the
                notification rather than only behind a tap.
            runtime_session_id: Authenticated runtime session for non-native calls.

        Returns:
            APNs payload dictionary.
        """
        # An `ask_user` call is a QUESTION, not a permission request: the
        # operator picks an option or types an answer rather than approving.
        # Clients branch on `is_question` (and on the aps category) to render
        # the right UI, including option buttons directly in the notification.
        args = tool_args or {}
        is_question = bool(args.get("is_question"))
        question_options = [
            str(option)
            for option in (args.get("options") or args.get("question_options") or [])
            if str(option).strip()
        ]
        allow_free_text = bool(args.get("allow_free_text", True))

        # Customize alert based on priority
        if is_question:
            title = "Agent question"
            if priority == "urgent":
                title = "🚨 Agent question (urgent)"
                sound = "critical.caf"
                interruption_level = "critical"
            elif priority == "high":
                title = "⚠️ Agent question"
                sound = "default"
                interruption_level = "time-sensitive"
            else:
                sound = "default"
                interruption_level = "active"
        elif priority == "urgent":
            title = "🚨 URGENT: Approval Needed"
            sound = "critical.caf"  # Must exist in app bundle
            interruption_level = "critical"
        elif priority == "high":
            title = "⚠️ High Priority Approval"
            sound = "default"
            interruption_level = "time-sensitive"
        else:
            title = "New Approval Request"
            sound = "default"
            interruption_level = "active"

        # iOS notification categories are STATIC (registered at app launch), so
        # option buttons are exposed via pre-registered per-arity categories
        # whose action ids are positional (OPTION_0..OPTION_N). The client maps
        # each id back to the label in `question_options`. iOS shows at most 4
        # actions, so cap at 4 options; beyond that the client falls back to
        # opening the app / inline text answer.
        risk_level = classify_approval_risk(tool_name, args)
        category = "APPROVAL_REQUEST"
        if is_question:
            option_count = len(question_options)
            if 2 <= option_count <= 4:
                category = f"QUESTION_{option_count}_OPTIONS"
            else:
                category = "QUESTION_REQUEST"
        elif risk_level == "low":
            category = "LOW_RISK_APPROVAL"

        # Format tool name nicely
        tool_display = tool_name.replace("_", " ").title()
        ask_text = (summary or "").strip() or None
        asker = (agent_name or "").strip() or None
        # The subtitle is the "what and who" line. Without a name it stays the
        # tool alone, which is what every notification said before.
        subtitle_text = f"{tool_display} · {asker}" if asker else tool_display

        if ask_text:
            # Summary is the primary ask; tool name stays as subtitle context.
            subtitle = subtitle_text
            body = ask_text if len(ask_text) <= 220 else ask_text[:217] + "..."
        else:
            subtitle = subtitle_text
            # Build body with tool args for context
            body_parts = []

            # Add key tool args (limit to fit notification)
            if tool_args:
                # Prioritize important args, limit total length
                arg_strs = []
                total_len = 0
                for key, value in tool_args.items():
                    # Skip internal/long args
                    if key.startswith("_") or key in ["content", "body", "message"]:
                        continue
                    # Format value (truncate if too long)
                    val_str = str(value)
                    if len(val_str) > 50:
                        val_str = val_str[:47] + "..."
                    arg_str = f"{key}: {val_str}"
                    if total_len + len(arg_str) > 150:
                        break
                    arg_strs.append(arg_str)
                    total_len += len(arg_str) + 2
                if arg_strs:
                    body_parts.append(" | ".join(arg_strs))

            # Add reasoning if no args or space remaining
            if agent_reasoning and len(" ".join(body_parts)) < 100:
                reasoning_preview = agent_reasoning[:80]
                if len(agent_reasoning) > 80:
                    reasoning_preview += "..."
                body_parts.append(reasoning_preview)

            # Fallback if no context. A named agent is named: "AI agent needs
            # approval" is what the console used to say too, and it is not a
            # thing anyone can look up.
            if not body_parts:
                body_parts.append(
                    f"{asker or 'AI agent'} needs approval for {tool_display}"
                )

            body = "\n".join(body_parts) if len(body_parts) > 1 else body_parts[0]

        from preloop.utils.approval_origin import approval_origin_text

        origin_text = approval_origin_text(args, runtime_session_id)
        if origin_text:
            subtitle = f"{subtitle} · {origin_text}"

        # Custom data for app routing (used by both iOS and Android)
        custom_data = {
            "type": "question" if is_question else "new_approval_request",
            "approval_request_id": request_id,
            "tool_name": tool_name,
            "priority": priority,
            "risk_level": risk_level,
        }
        if external_url:
            custom_data["external_url"] = external_url
            custom_data["external_decision"] = True
            category = "EXTERNAL_APPROVAL"
            body = "Decide on the external service."
        if ask_text:
            custom_data["summary"] = ask_text
        if asker:
            # Clients render "who asked" on the approval card without a fetch.
            custom_data["agent_name"] = asker
        rule_name = (rule_context or {}).get("rule_name")
        if rule_name:
            # Name only. The expression belongs on a surface that can show it
            # in full; a clipped CEL expression reads as a different rule.
            custom_data["rule_name"] = str(rule_name)
        runner_flow = (rule_context or {}).get("runner_flow")
        if isinstance(runner_flow, dict):
            # Runner owner consent: the device, flow and harness the owner
            # is asked to allow, so the card reads without a fetch.
            custom_data["runner_flow"] = compact_runner_flow(runner_flow)
        if expires_at:
            custom_data["expires_at"] = expires_at.isoformat()
        if is_question:
            # Clients render option buttons (positional OPTION_i actions map
            # back to these labels) and an inline dictated answer.
            custom_data["is_question"] = True
            custom_data["question"] = str(args.get("question") or ask_text or "")
            custom_data["question_options"] = question_options
            custom_data["allow_free_text"] = allow_free_text

        payload = {
            "aps": {
                "alert": {
                    "title": title,
                    "subtitle": subtitle,
                    "body": body,
                },
                "sound": sound,
                "badge": 1,  # iOS will auto-increment
                "category": category,
                "thread-id": "approval-requests",
                "interruption-level": interruption_level,
                "relevance-score": 1.0 if priority in ["urgent", "high"] else 0.5,
            },
            # Custom data at top level for iOS backward compatibility
            **custom_data,
            # Data block for Android/FCM deep-linking (include title/body for
            # data-only display paths)
            "data": {
                **custom_data,
                "title": title,
                "body": body,
            },
        }

        return _fit_payload(payload, PUSH_PAYLOAD_BUDGET_BYTES)

    @staticmethod
    def request_expiring_soon(
        request_id: str, tool_name: str, minutes_remaining: int
    ) -> Dict[str, Any]:
        """Build payload for expiring request reminder.

        Args:
            request_id: Approval request UUID.
            tool_name: Name of tool requiring approval.
            minutes_remaining: Minutes until expiration.

        Returns:
            APNs payload dictionary.
        """
        tool_display = tool_name.replace("_", " ").title()

        return {
            "aps": {
                "alert": {
                    "title": "⏰ Request Expiring Soon",
                    "subtitle": tool_display,
                    "body": f"Approval request expires in {minutes_remaining} minutes",
                },
                "sound": "default",
                "badge": 1,
                "category": "APPROVAL_REQUEST",
                "interruption-level": "time-sensitive",
            },
            "type": "request_expiring_soon",
            "approval_request_id": request_id,
            "tool_name": tool_name,
            "minutes_remaining": minutes_remaining,
        }

    @staticmethod
    def request_expired(request_id: str, tool_name: str) -> Dict[str, Any]:
        """Build payload for expired request.

        Args:
            request_id: Approval request UUID.
            tool_name: Name of tool requiring approval.

        Returns:
            APNs payload dictionary.
        """
        return {
            "aps": {
                "alert": {
                    "title": "Request Expired",
                    "subtitle": tool_name.replace("_", " ").title(),
                    "body": "An approval request has expired",
                },
                "sound": "default",
                "badge": 0,  # Clear badge
            },
            "type": "request_expired",
            "approval_request_id": request_id,
            "tool_name": tool_name,
        }

    @staticmethod
    def request_resolved(
        request_id: str, tool_name: str, resolved_by: str, decision: str
    ) -> Dict[str, Any]:
        """Build payload for resolved request (by another user).

        Args:
            request_id: Approval request UUID.
            tool_name: Name of tool requiring approval.
            resolved_by: Name of user who resolved the request.
            decision: Decision made ('approved' or 'declined').

        Returns:
            APNs payload dictionary.
        """
        return {
            "aps": {
                "alert": {
                    "title": "Request Resolved",
                    "subtitle": tool_name.replace("_", " ").title(),
                    "body": f"{resolved_by} {decision} this request",
                },
                "sound": "default",
                "badge": 0,
            },
            "type": "request_resolved",
            "approval_request_id": request_id,
            "tool_name": tool_name,
            "resolved_by": resolved_by,
            "decision": decision,
        }
