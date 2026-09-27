"""Direct tests for the validation logic in ``preloop.schemas.webhook_endpoint``."""

from typing import Type, Union

import pytest
from pydantic import ValidationError

from preloop.schemas.webhook_endpoint import (
    WebhookEndpointCreate,
    WebhookEndpointUpdate,
)
from preloop.services.event_webhooks.events import EVENT_TYPES_V1

SchemaType = Type[Union[WebhookEndpointCreate, WebhookEndpointUpdate]]
BOTH_SCHEMAS = [WebhookEndpointCreate, WebhookEndpointUpdate]


@pytest.mark.parametrize("schema", BOTH_SCHEMAS)
class TestWebhookUrl:
    """The URL must be absolute http(s); surrounding whitespace is trimmed."""

    @pytest.mark.parametrize(
        "url", ["https://hooks.example.com/in", "http://localhost:8080/hook"]
    )
    def test_http_urls_are_accepted(self, schema: SchemaType, url: str) -> None:
        assert schema(url=url).url == url

    def test_whitespace_is_trimmed(self, schema: SchemaType) -> None:
        assert schema(url="  https://hooks.example.com/in  ").url == (
            "https://hooks.example.com/in"
        )

    @pytest.mark.parametrize(
        "url", ["", "ftp://hooks.example.com", "hooks.example.com", "/relative"]
    )
    def test_other_urls_are_rejected(self, schema: SchemaType, url: str) -> None:
        with pytest.raises(ValidationError, match="url must start with"):
            schema(url=url)


@pytest.mark.parametrize("schema", BOTH_SCHEMAS)
class TestWebhookEventTypes:
    """Event types are checked against the v1 catalogue and canonicalized."""

    def test_types_are_deduplicated_in_catalogue_order(
        self, schema: SchemaType
    ) -> None:
        first, second = EVENT_TYPES_V1[0], EVENT_TYPES_V1[1]

        endpoint = schema(
            url="https://hooks.example.com/in",
            event_types=[second, first, second],
        )

        assert endpoint.event_types == [first, second]

    def test_unknown_types_are_listed_sorted(self, schema: SchemaType) -> None:
        with pytest.raises(ValidationError) as excinfo:
            schema(
                url="https://hooks.example.com/in",
                event_types=["zz.unknown", EVENT_TYPES_V1[0], "aa.unknown"],
            )

        assert "unknown event types: aa.unknown, zz.unknown" in str(excinfo.value)


class TestWebhookDefaults:
    """Create defaults to every event; update leaves omitted fields unset."""

    def test_create_with_no_types_means_every_event(self) -> None:
        endpoint = WebhookEndpointCreate(url="https://hooks.example.com/in")

        assert endpoint.event_types == []
        assert endpoint.active is True

    def test_update_omitted_fields_stay_none(self) -> None:
        update = WebhookEndpointUpdate()

        assert update.url is None
        assert update.event_types is None
        assert update.model_dump(exclude_unset=True) == {}
