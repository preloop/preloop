"""AIModel model for storing model configurations."""

import uuid
from typing import TYPE_CHECKING, Dict, List, Optional

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .account import Account
    from .flow import Flow
    from .issue_set import IssueSet
    from .managed_agent_ai_model_binding import ManagedAgentAIModelBinding
    from .secret_reference import SecretReference


class AIModel(Base):
    """
    Stores AI model configurations.
    """

    __tablename__ = "ai_model"

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Provider and model details
    provider_name: Mapped[str] = mapped_column(String(255), nullable=False)
    model_identifier: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    api_endpoint: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    api_key: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )  # Stored unencrypted for now
    credentials_secret_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("secret_reference.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Ownership and sharing
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.id"), nullable=True, index=True
    )
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Additional parameters
    model_parameters: Mapped[Optional[Dict]] = mapped_column(JSONB, nullable=True)
    meta_data: Mapped[Optional[Dict]] = mapped_column(
        JSONB, nullable=True, default=dict
    )

    # Relationships
    account: Mapped[Optional["Account"]] = relationship(back_populates="ai_models")
    credentials_secret: Mapped[Optional["SecretReference"]] = relationship(
        back_populates="ai_models"
    )
    flows: Mapped[List["Flow"]] = relationship(back_populates="ai_model")
    issue_sets: Mapped[List["IssueSet"]] = relationship(back_populates="ai_model")
    managed_agent_bindings: Mapped[List["ManagedAgentAIModelBinding"]] = relationship(
        "ManagedAgentAIModelBinding",
        back_populates="ai_model",
        cascade="all, delete-orphan",
    )

    @property
    def model_kind(self) -> str:
        """Return the model service kind stored in metadata."""
        meta_data = self.meta_data if isinstance(self.meta_data, dict) else {}
        service_kind = meta_data.get("service_kind") or meta_data.get("model_kind")
        if isinstance(service_kind, str) and service_kind.strip():
            normalized = service_kind.strip().lower()
            if normalized in {"llm", "stt", "tts"}:
                return normalized
        return "llm"

    @property
    def uses_ambient_credentials(self) -> bool:
        """Whether this model uses provider-native ambient credentials."""
        meta_data = self.meta_data if isinstance(self.meta_data, dict) else {}
        provider_runtime = (
            meta_data.get("provider_runtime")
            if isinstance(meta_data.get("provider_runtime"), dict)
            else {}
        )
        return bool(provider_runtime.get("ambient_credentials"))

    @property
    def has_api_key(self) -> bool:
        """Whether this model has any configured credential source."""
        return bool(
            self.credentials_secret_id or self.api_key or self.uses_ambient_credentials
        )

    @property
    def credential_type(self) -> Optional[str]:
        """Return the logical credential type configured for the model."""
        if self.credentials_secret:
            secret_kind = (self.credentials_secret.secret_kind or "").strip().lower()
            secret_meta = (
                self.credentials_secret.meta_data
                if isinstance(self.credentials_secret.meta_data, dict)
                else {}
            )
            if secret_kind == "ai_model_credentials":
                credential_type = secret_meta.get("credential_type")
                if isinstance(credential_type, str) and credential_type.strip():
                    return credential_type.strip()
            return "api_key"
        if self.api_key:
            return "api_key"
        if self.uses_ambient_credentials:
            return "ambient_provider"
        return None

    @property
    def is_principal_bound_oauth(self) -> bool:
        """Whether this model's credential is bound to a human principal.

        Claude Code / Codex OAuth credentials authorize only that principal's
        own interactive traffic. They cannot serve Preloop's server-side
        generation (optimization, policy generation, summaries), so they must
        never be auto-selected as a default.
        """
        from preloop.services.secret_service import (
            PRINCIPAL_BOUND_OAUTH_CREDENTIAL_TYPES,
        )

        return self.credential_type in PRINCIPAL_BOUND_OAUTH_CREDENTIAL_TYPES

    @property
    def supports_server_side_generation(self) -> bool:
        """Whether Preloop can run its own generation calls with this model."""
        return self.has_api_key and not self.is_principal_bound_oauth

    @property
    def credentials_backend_type(self) -> Optional[str]:
        """Return the backend type for the configured credentials."""
        if self.credentials_secret:
            return self.credentials_secret.backend_type
        if self.api_key:
            return "legacy_plaintext"
        if self.uses_ambient_credentials:
            return "ambient_provider"
        return None

    @property
    def credentials_external_ref(self) -> Optional[str]:
        """Return the external secret reference when applicable."""
        if self.credentials_secret:
            return self.credentials_secret.external_ref
        return None

    def __repr__(self):
        return f"<AIModel(id={self.id}, name='{self.name}')>"
