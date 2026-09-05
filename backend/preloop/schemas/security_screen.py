"""Schemas for the QM security-screen proxy contract endpoint.

Request and response shapes follow QM's documented external security-screen
proxy contract
(https://github.com/yc-software/qm/blob/main/docs/deploy-directory.md).
"""

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

from preloop.services.security_screen import MAX_TEXT_LENGTH


class SecurityScreenRequest(BaseModel):
    """One screened content chunk POSTed by a QM deployment.

    Attributes:
        text: Untrusted content to score. QM sends overlapping chunks of at
            most 1,600 characters; the cap here is a defensive bound only.
        hook: Screening hook, ``user_input`` or ``tool_response``. Unknown
            values are accepted for forward compatibility.
        metadata: Optional caller metadata (surface, origin, and chunk
            coordinates under the ``qm`` and provider-label namespaces).
            Treated as opaque.
    """

    model_config = ConfigDict(extra="ignore")

    text: str = Field(
        ...,
        max_length=MAX_TEXT_LENGTH,
        description="Untrusted content to score",
    )
    hook: str = Field(
        "user_input",
        max_length=100,
        description="Screening hook: user_input or tool_response",
    )
    metadata: Optional[Dict[str, Any]] = Field(
        None,
        description="Opaque caller metadata (chunk coordinates, surface)",
    )


class SecurityScreenResponse(BaseModel):
    """Screening verdict in the shape QM's proxy contract expects.

    Attributes:
        score: Finite score in [0, 1]; the chunk resolves to Strict on the
            caller side when the score is at or above the threshold.
        threshold: Finite threshold in [0, 1] this deployment screens at.
        primary_outcome: Optional lowercase label naming the dominant rule
            category; omitted for benign content.
    """

    score: float = Field(..., ge=0.0, le=1.0, description="Score in [0, 1]")
    threshold: float = Field(
        ..., ge=0.0, le=1.0, description="Strictness threshold in [0, 1]"
    )
    primary_outcome: Optional[str] = Field(
        None, description="Lowercase label of the dominant rule category"
    )
