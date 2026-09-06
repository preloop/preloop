"""Pydantic schemas for the account kill switch (org-level halt)."""

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from preloop.models.models.account_halt import HALT_SCOPES

HaltScope = Literal["gateway", "tools", "flows"]

#: One-tap presets offered by the console. The first entry is the full
#: emergency stop; the others exist for staged recovery.
HALT_ALL_SCOPES: List[str] = list(HALT_SCOPES)


class KillSwitchActivateRequest(BaseModel):
    """Request body for activating the kill switch."""

    scopes: List[HaltScope] = Field(
        default_factory=lambda: list(HALT_SCOPES),
        description=(
            "Which traffic classes to halt. Defaults to all of them (the "
            "emergency stop). Scope names: gateway, tools, flows."
        ),
    )
    reason: Optional[str] = Field(
        None,
        max_length=500,
        description="Why the halt was activated (recorded for audit)",
    )


class KillSwitchDeactivateRequest(BaseModel):
    """Request body for a staged or full re-enable."""

    reason: Optional[str] = Field(
        None, max_length=500, description="Reason for recovery, recorded for audit"
    )

    scopes: List[HaltScope] = Field(
        default_factory=lambda: list(HALT_SCOPES),
        description=(
            "Which traffic classes to re-enable. Pass a subset for staged "
            "recovery (e.g. only ['gateway'] first)."
        ),
    )


class KillSwitchScopeState(BaseModel):
    """Render-ready state for one halted scope."""

    scope: HaltScope
    activated_by_user_id: Optional[UUID] = None
    activated_by_username: Optional[str] = None
    activated_at: Optional[datetime] = None
    reason: Optional[str] = None


class KillSwitchStatus(BaseModel):
    """Aggregate kill-switch state used to drive the console banner.

    Any authenticated user can read this: knowing that agent activity is
    halted is not privileged information, and hiding it would defeat the
    point of the banner.
    """

    active: bool = Field(..., description="Whether any scope is currently halted")
    scopes: List[KillSwitchScopeState] = Field(
        default_factory=list,
        description="Currently-halted scopes with their activation audit data",
    )

    @property
    def halted_scopes(self) -> List[str]:
        """Names of the currently-halted scopes."""
        return [entry.scope for entry in self.scopes]
