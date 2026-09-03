"""Schemas for reading and fetching one model's effective price.

Prices are quoted per one million tokens because that is how every provider
publishes them; the stored override rows stay per one thousand, so the
conversion happens here, once, rather than in each client.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

#: Where an effective price came from, in resolution order.
PricingSource = Literal["override", "model_config", "catalog", "none"]


class AIModelPrice(BaseModel):
    """One price, in USD per million tokens."""

    input_per_1m: Optional[float] = Field(
        default=None, description="Price of a million input tokens, in USD."
    )
    output_per_1m: Optional[float] = Field(
        default=None, description="Price of a million output tokens, in USD."
    )
    cached_input_per_1m: Optional[float] = Field(
        default=None,
        description=(
            "Price of a million cached input tokens, in USD. Absent when the "
            "source says nothing about caching, which is not the same as free."
        ),
    )
    blended_per_1m: Optional[float] = Field(
        default=None,
        description=(
            "Single price applied to every token, in USD per million. Set by "
            "overrides that do not separate input from output."
        ),
    )
    request_price: Optional[float] = Field(
        default=None, description="Flat price added per request, in USD."
    )


class AIModelPricingResponse(BaseModel):
    """The price this account is actually charged for one model, and why."""

    ai_model_id: str
    model_alias: Optional[str] = None
    provider_name: Optional[str] = None
    source: PricingSource = Field(
        description=(
            "override: an account price override. model_config: pricing "
            "configured on the model itself. catalog: the bundled or live "
            "provider price list. none: nothing prices this model, so its "
            "requests land unpriced."
        )
    )
    price: AIModelPrice = Field(default_factory=AIModelPrice)
    currency: str = "USD"
    override_id: Optional[str] = None
    effective_from: Optional[datetime] = None
    effective_until: Optional[datetime] = None
    catalog_key: Optional[str] = Field(
        default=None,
        description="The price list entry that matched, when the source is the catalog.",
    )
    fetch_supported: bool = Field(
        default=False,
        description="True when this provider publishes prices Preloop can read.",
    )
    fetch_provider_label: Optional[str] = Field(
        default=None,
        description="Provider name to show when a price fetch is not offered.",
    )


class AIModelPriceQuote(BaseModel):
    """A price read from the provider, for confirmation. Nothing is saved."""

    ai_model_id: str
    provider_name: Optional[str] = None
    source_url: str
    model_key: str = Field(description="The provider's own identifier for the model.")
    price: AIModelPrice
    currency: str = "USD"
    fetched_at: datetime
