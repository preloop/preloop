"""Schemas for the publication-gate verification policy (issue #428).

The trusted test profile travels inside the flow configuration, so it is a
flow schema, not a service-side detail: the API validates it when a flow is
created or updated, the console can render it, and the publication gate
re-derives required checks from it at publication time. It is *trusted*
precisely because it never comes from the repository under test — an agent
cannot narrow required checks or edit the profile inside its own PR.
"""

import re
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class VerificationCommand(BaseModel):
    """One required check: a shell command plus why it exists."""

    id: str = Field(description="Stable identifier used in evidence records")
    command: str = Field(
        description="Shell command executed in the repository working tree"
    )
    reason: str = Field(description="Why this check exists; recorded with the evidence")
    scope: str = Field(
        default="unknown",
        description=(
            "Coarse impact area (docs, migration, frontend, backend, api, "
            "shared, unknown) used for reporting and narrow-diff assertions"
        ),
    )
    timeout_seconds: int = Field(
        default=900,
        ge=1,
        le=3600,
        description="Wall-clock budget for one execution of this command",
    )

    @field_validator("id")
    @classmethod
    def valid_command_id(cls, value: str) -> str:
        """Keep check identities unambiguous and safe as log filenames."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", value):
            raise ValueError("Check ID must be a nonempty safe identifier")
        return value

    @field_validator("command")
    @classmethod
    def nonempty_command(cls, value: str) -> str:
        """Blank shell commands are not verification."""
        if not value.strip() or "\x00" in value or len(value.encode("utf-8")) > 16384:
            raise ValueError("Check command must be nonempty and bounded")
        return value


class ProfileRule(BaseModel):
    """Changed-file patterns that pull in a set of required checks."""

    id: str = Field(description="Stable rule identifier recorded in evidence")
    description: str = Field(description="What kind of change this rule covers")
    path_globs: List[str] = Field(
        description=(
            "fnmatch patterns matched against changed paths "
            "(e.g. 'backend/preloop/models/alembic/versions/*')"
        )
    )
    commands: List[VerificationCommand] = Field(
        default_factory=list,
        description="Checks required when this rule matches",
    )


class VerificationProfile(BaseModel):
    """Versioned, trusted set of required checks for a repository.

    Ships with the flow configuration, not inside the repository under test.
    Inexpensive ``always`` hooks are required on every change; rules match
    the changed-file diff; ``unknown_default`` covers unknown impact so a
    diff never resolves to an empty required list.
    """

    version: Literal["v1"] = "v1"
    profile_id: str = Field(description="Stable profile name, e.g. 'default'")
    description: str = Field(default="")
    # Inexpensive hooks required on every change (lint, format, fast unit
    # suites).
    always: List[VerificationCommand] = Field(default_factory=list)
    # Impact-specific rules matched against the changed-file list.
    rules: List[ProfileRule] = Field(default_factory=list)
    # Used when no rule matches: unknown impact cannot resolve to an empty
    # test list.
    unknown_default: List[VerificationCommand] = Field(default_factory=list)


class VerificationPolicy(BaseModel):
    """Publication gate policy inside ``git_clone_config`` (issue #428).

    ``mode: "gate"`` requires the runner-controlled verifier to allow
    publication (push / pull-request creation) before the agent's commits
    leave the workspace. There is no draft-publication exception: unverified
    work stays unpushed.
    """

    mode: Literal["off", "gate"] = "gate"
    profile: VerificationProfile = Field(
        description="Versioned trusted test profile driving required checks"
    )
    image: Optional[str] = Field(
        default=None,
        description="Digest-pinned generic toolchain image for credential-isolated checks. Must contain dependencies required by the trusted profile.",
    )
    # Overall wall-clock budget for the verifier inside the post-execution
    # block; per-check timeouts come from the profile commands.
    gate_budget_seconds: int = Field(
        default=3600,
        ge=30,
        le=14400,
        description=(
            "Wall-clock budget for the whole verification run; a check that "
            "cannot start inside it is blocked, not failed"
        ),
    )


class ResolvedVerificationPolicy(BaseModel):
    """Effective policy as computed for one flow, for display and gating.

    Existing saved flows predate the gate: they resolve to ``mode="off"``
    with an explicit ``reason`` so consoles can *show* the effective policy
    instead of leaving behaviour implicit. Flows are never silently moved to
    the gate by a schema default; enabling it is a configuration act.
    """

    mode: Literal["off", "gate"] = "off"
    reason: str = Field(
        default="no verification policy configured; publication is not gated",
    )
    profile: Optional[VerificationProfile] = None
    gate_budget_seconds: int = Field(default=3600, ge=30, le=14400)
