"""Fleet event selection follows the server capability without changing delivery."""

from unittest.mock import MagicMock, patch

import pytest

from preloop.api.endpoints.event_webhooks import get_webhook_catalogue
from preloop.schemas.webhook_endpoint import (
    WebhookEndpointCreate,
    WebhookEndpointUpdate,
)
from tests.conftest import maybe_await


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False, None])
async def test_catalogue_gates_fleet_event(enabled: bool | None) -> None:
    """Only a server advertising fleet coverage offers the fleet event."""
    manager = MagicMock()
    manager.get_enabled_features.return_value = {
        "features": {} if enabled is None else {"fleet_coverage": enabled}
    }
    with patch("preloop.plugins.base.get_plugin_manager", return_value=manager):
        catalogue = await maybe_await(
            get_webhook_catalogue(
                account=MagicMock(), current_user=MagicMock(), db=MagicMock()
            )
        )
    events = {item.name: item.description for item in catalogue.event_types}
    assert ("fleet.finding" in events) is (enabled is True)
    assert "approval.created" in events
    if enabled:
        assert events["fleet.finding"]


def test_fleet_event_can_be_selected_on_create_and_update() -> None:
    """The event offered in the catalogue is accepted by both write schemas."""
    create = WebhookEndpointCreate(
        url="https://receiver.example.com/events", event_types=["fleet.finding"]
    )
    update = WebhookEndpointUpdate(event_types=["fleet.finding", "fleet.finding"])
    assert create.event_types == ["fleet.finding"]
    assert update.event_types == ["fleet.finding"]
