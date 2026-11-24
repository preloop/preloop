"""Bounded Teams Workflows notices, independent of bot connections."""

from __future__ import annotations

import json
from typing import Any

MAX_TEAMS_WEBHOOK_BYTES = 28 * 1024


def approval_card(
    headline: str, tool: str, status: str, review_url: str
) -> dict[str, Any]:
    """Build a Workflows card with one review action within the wire limit.

    Long display text is shortened; the review URL is never truncated. Reject a
    URL that alone makes the card too large instead of queueing a broken link.
    """

    return _notice_card(
        [
            {"type": "TextBlock", "text": _shorten(headline, 1000), "wrap": True},
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Tool", "value": _shorten(tool, 256)},
                    {"title": "Status", "value": _shorten(status.upper(), 64)},
                ],
            },
        ],
        review_url,
        "Review",
        "Teams approval notice exceeds 28 KB",
    )


def policy_notice_card(headline: str, text: str, notices_url: str) -> dict[str, Any]:
    """Build a bounded informational card with no approval decision actions."""
    return _notice_card(
        [
            {"type": "TextBlock", "text": _shorten(headline, 400), "wrap": True},
            {
                "type": "TextBlock",
                "text": "This notify rule did not block the call.",
                "wrap": True,
            },
            {"type": "TextBlock", "text": _shorten(text, 1400), "wrap": True},
        ],
        notices_url,
        "See all notices",
        "Teams policy notice exceeds 28 KB",
    )


def _shorten(value: str, limit: int) -> str:
    """Shorten display text without truncating action URLs."""
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _notice_card(
    body: list[dict[str, Any]], url: str, action_title: str, size_error: str
) -> dict[str, Any]:
    """Wrap card content in the Workflows envelope and enforce its wire limit."""
    message = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": body,
                    "actions": [
                        {"type": "Action.OpenUrl", "title": action_title, "url": url}
                    ],
                },
            }
        ],
    }
    # Default ASCII escaping is a conservative bound, including supplementary
    # Unicode characters. The outbox may serialize a smaller UTF-8 body.
    if len(json.dumps(message).encode("utf-8")) > MAX_TEAMS_WEBHOOK_BYTES:
        raise ValueError(size_error)
    return message
