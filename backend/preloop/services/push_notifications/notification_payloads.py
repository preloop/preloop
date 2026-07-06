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

        Returns:
            APNs payload dictionary.
        """
        # Customize alert based on priority
        if priority == "urgent":
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

        # Format tool name nicely
        tool_display = tool_name.replace("_", " ").title()

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
            "type": "new_approval_request",
            "approval_request_id": request_id,
            "tool_name": tool_name,
            "priority": priority,
        }
        if expires_at:
            custom_data["expires_at"] = expires_at.isoformat()

        payload = {
            "aps": {
                "alert": {
                    "title": title,
                    "subtitle": tool_display,
                    "body": body,
                },
                "sound": sound,
                "badge": 1,  # iOS will auto-increment
                "category": "APPROVAL_REQUEST",
                "thread-id": "approval-requests",
                "interruption-level": interruption_level,
                "relevance-score": 1.0 if priority in ["urgent", "high"] else 0.5,
            },
            # Custom data at top level for iOS backward compatibility
            **custom_data,
            # Data block for Android/FCM deep-linking
            "data": custom_data,
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
