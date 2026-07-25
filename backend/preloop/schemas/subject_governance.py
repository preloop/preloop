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
            'enforced for this subject. "off" makes the server auto-approve '
            "escalated (ask) calls instead of asking a human; the calls are "
            "still recorded as approval requests marked "
            "auto_approved_reason='native_tool_approvals_off' and audited with "
            "no approver, so the audit trail stays complete. Unlike a "
            "time-boxed approval bypass this setting does not expire; it stays "
            'off until changed. Absent/None means "enforce".'
        ),
    )


class SubjectGovernanceResponse(BaseModel):
    subject_type: str
    subject_id: str
    config: SubjectGovernanceConfig


class AccountGovernanceDefaults(BaseModel):
    """Account-wide governance defaults inherited by every managed agent.

    A per-agent subject-governance config with an explicit value overrides
    these; an absent/None per-agent value inherits. The final fallback when
    both are unset is "enforce" (fail safe).
    """

    native_tool_approvals: Optional[Literal["enforce", "off"]] = Field(
        None,
        description=(
            "Account default for native tool-call approvals. Agents without "
            "an explicit per-agent setting inherit this; absent/None means "
            '"enforce".'
        ),
    )
    approval_workflow_id: Optional[str] = Field(
        None,
        description=(
            "Account default approval workflow for native tool-call "
            "approvals. Agents without a per-agent pin inherit this; absent "
            "falls back to the account's default workflow."
        ),
    )


class AccountGovernanceDefaultsResponse(BaseModel):
    defaults: AccountGovernanceDefaults
    # How many managed agents carry an explicit override, so the console can
    # say "N agents override this" next to the default editor.
    override_agent_ids: List[str] = Field(default_factory=list)
