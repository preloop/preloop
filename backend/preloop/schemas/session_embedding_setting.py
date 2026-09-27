"""Request and response shapes for one account's session embedding setting.

The read side publishes the whole setting, including the degraded marker,
the deployment default cap and how far the corpus has been embedded, because
an account looking at this surface is asking two questions at once: what am
I sending to a provider, and why has it stopped.

The write side is what the console's opt in card saves: ``enabled``,
``scope``, ``daily_cap_usd`` and, when turning embedding on, the provider,
model and endpoint the text is posted to. Every field is optional so a
client can change one thing without restating the rest. Naming a provider is
still the opt in: provider details are only accepted when the save leaves
the setting enabled, and they go through the CRUD layer where the host
policy lives, so the console cannot store an endpoint nobody validated.

``scope`` and ``provider`` are ``Literal``, so an unknown value is a 422 from
the validation layer rather than a row that quietly embeds either everything
or nothing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The two answers to "how much of a session may be embedded". Mirrors
#: ``EMBEDDING_SCOPES`` on the model; kept as a ``Literal`` here so FastAPI
#: rejects anything else before a request reaches the CRUD layer.
SessionEmbeddingScope = Literal["summaries_only", "full"]

#: Providers the worker can build. Mirrors ``EMBEDDING_PROVIDERS`` on the
#: model for the same reason as the scope.
SessionEmbeddingProvider = Literal["openai_compatible", "local"]

#: One sentence of help text, published with the setting so a console does
#: not have to invent its own wording for the trade off.
SCOPE_HELP_TEXT = (
    "summaries_only embeds each session's generated title and summary, which "
    "is about one short chunk per session; full also embeds transcripts, "
    "which is roughly forty times the vectors and the provider spend for the "
    "same sessions. Keyword search covers the whole corpus either way."
)


class SessionEmbeddingCorpus(BaseModel):
    """How much of this account's corpus semantic search can read.

    The same counts search uses to explain a thin semantic answer, published
    so a user who opted in yesterday can see the backlog rather than guess.
    """

    model_config = ConfigDict(protected_namespaces=())

    vectors: int = Field(0, description="Chunks carrying a vector of any model.")
    model_vectors: int = Field(
        0, description="Chunks embedded with the model configured now."
    )
    pending: int = Field(
        0, description="Chunks inside the current scope still waiting for one."
    )
    embedded_through: Optional[datetime] = Field(
        None, description="Newest chunk embedded with the configured model."
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
    deployment_daily_cap_usd: float = Field(
        ..., description="The deployment default cap a null account cap uses."
    )
    deployment_embedding_enabled: bool = Field(
        ...,
        description=(
            "False when the deployment kill switch has turned embedding off "
            "for every account, whatever this setting says."
        ),
    )
    degraded_reason: Optional[str] = Field(
        None, description="Why the last run did less than it wanted to."
    )
    degraded_at: Optional[datetime] = Field(
        None, description="When that degraded state was recorded."
    )
    corpus: SessionEmbeddingCorpus = Field(
        default_factory=SessionEmbeddingCorpus,
        description="How far embedding has got through this account's corpus.",
    )


class SessionEmbeddingSettingUpdate(BaseModel):
    """What the console's opt in card saves. Absent fields are left alone."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    enabled: Optional[bool] = Field(
        None,
        description=(
            "Turn embedding on or off. Turning it on needs a model, either in "
            "this body or already on the setting from an earlier opt in."
        ),
    )
    scope: Optional[SessionEmbeddingScope] = Field(
        None, description="summaries_only (default) or full."
    )
    daily_cap_usd: Optional[float] = Field(
        None,
        ge=0,
        description=(
            "Account cap in USD. An explicit null clears it back to the "
            "deployment default; leaving the field out keeps the current cap."
        ),
    )
    provider: Optional[SessionEmbeddingProvider] = Field(
        None, description="Provider family; only accepted when enabling."
    )
    model_identifier: Optional[str] = Field(
        None,
        max_length=255,
        description="Model name as the provider knows it; only when enabling.",
    )
    base_url: Optional[str] = Field(
        None,
        max_length=500,
        description="https endpoint for openai_compatible; only when enabling.",
    )

    @model_validator(mode="after")
    def _says_something(self) -> "SessionEmbeddingSettingUpdate":
        """An empty save is a client bug, not a successful no-op."""
        if not self.model_fields_set:
            raise ValueError("the update must change at least one field")
        return self

    @property
    def names_provider(self) -> bool:
        """Whether this body carries any of the opt in's provider details."""
        return bool(
            {"provider", "model_identifier", "base_url"} & self.model_fields_set
        )
