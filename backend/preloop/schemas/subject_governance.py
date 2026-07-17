"""Schemas for subject-scoped governance configuration."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

NATIVE_TOOL_APPROVALS_ENFORCE = "enforce"
NATIVE_TOOL_APPROVALS_OFF = "off"


class SubjectGovernanceConfig(BaseModel):
    allowed_models: List[str] = Field(default_factory=list)
    model_budgets: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    tool_rules: Dict[str, List[Dict[str, Any]]] = Field(default_factory=dict)
    tool_enabled_overrides: Dict[str, bool] = Field(default_factory=dict)
    approval_workflow_id: Optional[str] = Field(
        None,
        description=(
            "Approval workflow that governs this subject's native tool-call "
            "approvals (agents permission-check). Falls back to the account "
            "default workflow when unset."
        ),
    )
    native_tool_approvals: Optional[Literal["enforce", "off"]] = Field(
        None,
        description=(
            "Whether native tool-call approvals (agents permission-check) are "
            'enforced for this subject. "off" makes the server auto-allow '
            "escalated (ask) calls instead of asking a human; absent/None "
            'means "enforce".'
        ),
    )


class SubjectGovernanceResponse(BaseModel):
    subject_type: str
    subject_id: str
    config: SubjectGovernanceConfig
