"""Public credential identity compatibility shared by model and flow reads."""

from typing import Optional

from pydantic import BaseModel, Field


class ExecutorEligibility(BaseModel):
    """Credential compatibility, separate from runtime health and availability."""

    eligible: bool
    reason: Optional[str] = Field(
        None, description="Stable denial code when ineligible"
    )
    message: Optional[str] = Field(
        None, description="Safe remediation or execution prerequisite"
    )
