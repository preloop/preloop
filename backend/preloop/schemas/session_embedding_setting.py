"""Request and response shapes for one account's session embedding setting.

The read side publishes the whole setting, including the degraded marker,
because an account looking at this surface is asking two questions at once:
what am I sending to a provider, and why has it stopped. The write side is
deliberately narrower than the read side: it carries ``scope`` and nothing
more. Turning embedding on names a provider, a model and an endpoint the
account's text is posted to, which is a larger decision with its own
validation in the CRUD layer, and widening this body to carry it would make
an opt in look like a preference toggle.

``scope`` is a ``Literal``, so an unknown value is a 422 from the validation
layer rather than a row that quietly embeds either everything or nothing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

#: The two answers to "how much of a session may be embedded". Mirrors
#: ``EMBEDDING_SCOPES`` on the model; kept as a ``Literal`` here so FastAPI
#: rejects anything else before a request reaches the CRUD layer.
SessionEmbeddingScope = Literal["summaries_only", "full"]

#: One sentence of help text, published with the setting so a console does
#: not have to invent its own wording for the trade off.
SCOPE_HELP_TEXT = (
    "summaries_only embeds each session's generated title and summary, which "
    "is about one short chunk per session; full also embeds transcripts, "
    "which is roughly forty times the vectors and the provider spend for the "
    "same sessions. Keyword search covers the whole corpus either way."
)


class SessionEmbeddingSettingResponse(BaseModel):
    """What one account's embedding setting says right now."""

    model_config = ConfigDict(protected_namespaces=())

    enabled: bool = Field(
        ..., description="Whether this account has opted in to embedding."
    )
    scope: SessionEmbeddingScope = Field(
        ..., description="How much of a session is embedded."
    )
    scope_help: str = Field(
        SCOPE_HELP_TEXT, description="One sentence on what the scopes cost."
    )
    provider: str = Field(..., description="Provider family the vectors come from.")
    model_identifier: Optional[str] = Field(
        None, description="Model name as the provider knows it."
    )
    base_url: Optional[str] = Field(
        None, description="Endpoint the text is posted to, when there is one."
    )
    dimensions: int = Field(..., description="Width of the stored vectors.")
    daily_cap_usd: Optional[float] = Field(
        None, description="Account cap in USD; null falls back to the deployment."
    )
    degraded_reason: Optional[str] = Field(
        None, description="Why the last run did less than it wanted to."
    )
    degraded_at: Optional[datetime] = Field(
        None, description="When that degraded state was recorded."
    )


class SessionEmbeddingSettingUpdate(BaseModel):
    """The one thing this endpoint lets an account change."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    scope: SessionEmbeddingScope = Field(
        ..., description="summaries_only (default) or full."
    )
