"""Notification payload templates for Apple Push Notifications."""

from typing import Dict, Any, Optional
from datetime import datetime


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
        category = "APPROVAL_REQUEST"
        if is_question:
            option_count = len(question_options)
            if 2 <= option_count <= 4:
                category = f"QUESTION_{option_count}_OPTIONS"
            else:
                category = "QUESTION_REQUEST"

        # Format tool name nicely
        tool_display = tool_name.replace("_", " ").title()
        ask_text = (summary or "").strip() or None

        if ask_text:
            # Summary is the primary ask; tool name stays as subtitle context.
            subtitle = tool_display
            body = ask_text if len(ask_text) <= 220 else ask_text[:217] + "..."
        else:
            subtitle = tool_display
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

            # Fallback if no context
            if not body_parts:
                body_parts.append(f"AI agent needs approval for {tool_display}")

            body = "\n".join(body_parts) if len(body_parts) > 1 else body_parts[0]

        # Custom data for app routing (used by both iOS and Android)
        custom_data = {
            "type": "question" if is_question else "new_approval_request",
            "approval_request_id": request_id,
            "tool_name": tool_name,
            "priority": priority,
        }
        if ask_text:
            custom_data["summary"] = ask_text
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

        return payload

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
