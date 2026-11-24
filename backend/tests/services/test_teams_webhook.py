"""Protects Workflows wire payloads; fails on drift; no_claim: tenant rendering."""

import json
from types import SimpleNamespace

import pytest

from preloop.services.event_webhooks.approval_shim import resolve_webhook_target
from preloop.services.teams_webhook import approval_card, MAX_TEAMS_WEBHOOK_BYTES


# Documented envelope: https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook
@pytest.mark.parametrize("legacy", [True, False])
def test_teams_target(legacy: bool) -> None:
    """Protects both config forms; fails on fallback; no_claim: remote delivery."""
    url = "https://example.com/workflows/test"
    workflow = SimpleNamespace(
        approval_type="teams",
        approval_config={"webhook_url": url} if legacy else {},
        channel_configs={} if legacy else {"teams": {"url": url}},
    )
    assert resolve_webhook_target(workflow) == ("teams", url)


def test_card_content_and_bound() -> None:
    """Protects exact content and size; fails on overflow; no_claim: rendering."""
    url = "https://example.com/console/approval/test"
    payload = approval_card("Approval Required", "sample_tool", "pending", url)
    card = payload["attachments"][0]["content"]
    assert payload["type"] == "message"
    assert (
        payload["attachments"][0]["contentType"]
        == "application/vnd.microsoft.card.adaptive"
    )
    assert card["body"] == [
        {"type": "TextBlock", "text": "Approval Required", "wrap": True},
        {
            "type": "FactSet",
            "facts": [
                {"title": "Tool", "value": "sample_tool"},
                {"title": "Status", "value": "PENDING"},
            ],
        },
    ]
    assert card["actions"] == [
        {"type": "Action.OpenUrl", "title": "Review", "url": url}
    ]
    bounded = approval_card("😀" * 100000, "😀" * 100000, "pending", url)
    assert len(json.dumps(bounded).encode()) <= MAX_TEAMS_WEBHOOK_BYTES
    assert bounded["attachments"][0]["content"]["body"][0]["text"].endswith("…")
    with pytest.raises(ValueError, match="^Teams approval notice exceeds 28 KB$"):
        approval_card(
            "Approval", "tool", "pending", url + "x" * MAX_TEAMS_WEBHOOK_BYTES
        )
