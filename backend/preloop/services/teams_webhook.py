"""Bounded Teams Workflows approval notices, independent of bot connections."""

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

    def shorten(value: str, limit: int) -> str:
        return value if len(value) <= limit else value[: limit - 1] + "…"

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
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": shorten(headline, 1000),
                            "wrap": True,
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": "Tool", "value": shorten(tool, 256)},
                                {
                                    "title": "Status",
                                    "value": shorten(status.upper(), 64),
                                },
                            ],
                        },
                    ],
                    "actions": [
                        {"type": "Action.OpenUrl", "title": "Review", "url": review_url}
                    ],
                },
            }
        ],
    }
    # Default ASCII escaping is a conservative bound, including supplementary
    # Unicode characters. The outbox may serialize a smaller UTF-8 body.
    if len(json.dumps(message).encode("utf-8")) > MAX_TEAMS_WEBHOOK_BYTES:
        raise ValueError("Teams approval notice exceeds 28 KB")
    return message
