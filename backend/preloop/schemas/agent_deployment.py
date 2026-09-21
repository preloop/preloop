"""Validated inputs for operator initiated agent deployment."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr, model_validator


class AgentDeploymentSSH(BaseModel):
    """An explicit SSH destination and its independently verified host key."""

    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-zA-Z_][a-zA-Z0-9_-]*$"
    )
    host_key: str = Field(min_length=20, max_length=16384)
    password: SecretStr | None = None
    private_key: SecretStr | None = None

    @model_validator(mode="after")
    def require_one_credential(self) -> "AgentDeploymentSSH":
        """Do not silently fall back to a server's personal SSH identities."""
        if bool(self.password) == bool(self.private_key):
            raise ValueError("Provide exactly one SSH password or private key")
        return self


class AgentDeploymentRequest(BaseModel):
    """A single deployment; reuse its idempotency key when retrying."""

    idempotency_key: UUID
    runtime: Literal["hermes", "openclaw"]
    model_id: UUID
    target: Literal["ssh", "gcp"]
    ssh: AgentDeploymentSSH | None = None
    compute_size: Literal["standard", "performance", "high-mem"] = "standard"

    @model_validator(mode="after")
    def validate_target(self) -> "AgentDeploymentRequest":
        """Reject unused credentials rather than retaining them unnecessarily."""
        if (self.target == "ssh") != (self.ssh is not None):
            raise ValueError("SSH configuration is required only for an SSH target")
        return self
