"""The verification contract behind the publication gate (issue #428).

An implementation flow's honest ``result.json`` is a claim, not evidence: a
prompt asks the agent to test, but nothing forced it. This module defines the
runner-side contract that turns "the agent says it tested" into "the runner
verified the exact commit that is about to be published":

* **Trusted test profile** (:class:`VerificationProfile`) — a *versioned*
  mapping from changed-file patterns to required checks. It travels in the
  flow configuration (never read from the repository under test), so the
  agent cannot narrow required checks or edit the profile to bypass the gate
  within its own PR.
* **Selection** (:func:`select_required_checks`) — required checks for a
  diff, derived from the profile. Inexpensive hooks always run; migrations,
  frontend and API changes pull in their focused suites; unknown impact uses
  the profile's conservative default, never an empty list.
* **Evidence** (:class:`VerificationEvidence`) — what the runner-controlled
  verifier recorded: commands, exit codes, log references, environment
  digest, profile version, and the exact commit/tree that was verified.
* **Publication decision** (:func:`evaluate_publication`) — fail-closed.
  Missing, failed, foreign-produced, wrong-commit or stale evidence refuses
  publication; only evidence for the current commit and tree allows it.

The publisher (``agents/container.py`` post-execution path) consults the
decision before pushing or opening a pull request: the in-container
verifier runs :func:`preloop.utils.verification_selection.evaluate_from_raw`
(the same fail-closed function this module wraps) and only an ALLOW
verdict lets the push and pull-request creation proceed. There is no
opt-in draft-publication exception in this preset: unverified work
stays unpushed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, Field

from preloop.models.schemas.verification import (
    ResolvedVerificationPolicy,
    VerificationCommand,
    VerificationProfile,
)
from preloop.utils.verification_selection import (
    VERIFICATION_PRODUCER,
    VERIFIER_VERSION,
    evaluate_from_raw,
    select_from_raw,
)

VERIFICATION_STATUS_PASSED = "passed"
VERIFICATION_STATUS_FAILED = "failed"
VERIFICATION_STATUS_BLOCKED = "blocked"

# Agent-visible scopes. Selection reasons and profile fixtures use these so
# operators can read *why* a check was required.
SCOPE_DOCS = "docs"
SCOPE_MIGRATION = "migration"
SCOPE_FRONTEND = "frontend"
SCOPE_BACKEND = "backend"
SCOPE_API = "api"
SCOPE_SHARED = "shared"
SCOPE_UNKNOWN = "unknown"


class CheckSelection(BaseModel):
    """One selected required check plus the reason it was selected."""

    command: VerificationCommand
    selected_by: List[str] = Field(
        description=(
            "Rule ids (or 'always' / 'unknown_default') that required this "
            "check; a check selected by several rules keeps every reason"
        )
    )


class SelectedChecks(BaseModel):
    """Required checks for one diff, with recorded selection reasons."""

    changed_files: List[str] = Field(default_factory=list)
    checks: List[CheckSelection] = Field(default_factory=list)
    matched_rule_ids: List[str] = Field(default_factory=list)
    used_unknown_default: bool = False

    @property
    def command_ids(self) -> List[str]:
        return [selection.command.id for selection in self.checks]

    @property
    def commands(self) -> List[VerificationCommand]:
        return [selection.command for selection in self.checks]


class CheckRecord(BaseModel):
    """Runner-recorded outcome of one executed (or reused) check."""

    id: str
    command: str
    scope: str = SCOPE_UNKNOWN
    exit_code: Optional[int] = Field(
        default=None,
        description="None means the check never ran (blocked or skipped)",
    )
    log_file: Optional[str] = Field(
        default=None,
        description="Path of the captured log inside the evidence pack",
    )
    duration_seconds: Optional[float] = None
    reused: bool = Field(
        default=False,
        description="True when the verdict reuses matching earlier evidence",
    )
    skipped_reason: Optional[str] = Field(
        default=None,
        description=(
            "Explicit, non-empty when the check did not run (e.g. an "
            "unavailable database); optional skipped checks are visible, "
            "never silently dropped"
        ),
    )

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


class EnvironmentDigest(BaseModel):
    """Runner-observed environment the checks ran in."""

    python_version: str = ""
    git_version: str = ""
    os_name: str = ""
    telemetry_disabled: bool = False
    extra: Dict[str, str] = Field(default_factory=dict)


class VerificationEvidence(BaseModel):
    """Verifier-produced proof bound to one exact commit and tree."""

    producer: str = VERIFICATION_PRODUCER
    verifier_version: int = VERIFIER_VERSION
    profile_id: str
    profile_version: str
    commit_sha: str = Field(description="Full SHA of the verified commit")
    tree_hash: str = Field(description="git tree hash of the verified state")
    clean_tree: bool
    status: Literal["passed", "failed", "blocked"]
    checks: List[CheckRecord] = Field(default_factory=list)
    environment: EnvironmentDigest = Field(default_factory=EnvironmentDigest)
    changed_files: List[str] = Field(default_factory=list)
    base_ref: str = ""
    profile_digest: str = ""
    started_at: str = ""
    finished_at: str = ""


class PublicationDecision(BaseModel):
    """Semantic gate decision; this is not a credential attestation."""

    allowed: bool
    status: Literal["passed", "failed", "blocked"]
    reason: str


EffectiveVerificationPolicy = ResolvedVerificationPolicy


def configured_verification_commands(
    profile: VerificationProfile,
) -> List[VerificationCommand]:
    """Enumerate the complete profile contract for environment capability checks.

    A command ID may appear in multiple selection rules only when its full
    command contract agrees. Otherwise selection order would weaken checks.
    """
    by_id: Dict[str, VerificationCommand] = {}
    commands = [
        *profile.always,
        *(command for rule in profile.rules for command in rule.commands),
        *profile.unknown_default,
    ]
    for command in commands:
        if command.id in by_id and by_id[command.id] != command:
            raise ValueError("Conflicting verification command ID: " + command.id)
        by_id.setdefault(command.id, command)
    return list(by_id.values())


def resolve_verification_policy(
    git_config: Optional[Mapping[str, Any]],
) -> EffectiveVerificationPolicy:
    """Resolve saved policy; invalid configured gates fail closed.

    Missing policy preserves legacy behavior. An explicit malformed policy is
    never silently downgraded to off, even if an old saved record bypassed API
    validation.
    """
    raw = (git_config or {}).get("verification")
    if raw is None:
        return EffectiveVerificationPolicy()
    if isinstance(raw, BaseModel):
        raw = raw.model_dump()
    if not isinstance(raw, Mapping):
        raise ValueError("Invalid verification policy: expected an object")
    if raw.get("mode") == "off":
        return EffectiveVerificationPolicy(
            reason="verification policy disabled by configuration"
        )
    if raw.get("mode") != "gate":
        raise ValueError("Unknown verification mode; publication blocked")
    profile = VerificationProfile.model_validate(raw.get("profile"))
    configured_verification_commands(profile)
    try:
        budget = int(raw.get("gate_budget_seconds", 3600))
    except (TypeError, ValueError):
        budget = 3600
    return EffectiveVerificationPolicy(
        mode="gate",
        profile=profile,
        gate_budget_seconds=max(30, min(budget, 14400)),
        reason="publication is gated on the profile's required checks",
    )


def select_required_checks(
    profile: VerificationProfile, changed_files: Sequence[str]
) -> SelectedChecks:
    """Derive required checks for a changed-file list.

    The union of matching rules wins; a file matching several rules requires
    every matched rule's checks. Inexpensive ``always`` hooks are required on
    every diff. When no rule matches (unknown impact) the profile's
    conservative ``unknown_default`` is used — selection never returns an
    empty list for a non-empty profile.

    Delegates to the shared stdlib implementation
    (:func:`preloop.utils.verification_selection.select_from_raw`) so the
    runner-side contract and the in-container verifier execute the exact
    same selection code.
    """

    raw = select_from_raw(profile.model_dump(), changed_files)

    selections: List[CheckSelection] = []
    for entry in raw["checks"]:
        selections.append(
            CheckSelection(
                command=VerificationCommand.model_validate(entry["command"]),
                selected_by=list(entry["selected_by"]),
            )
        )

    return SelectedChecks(
        changed_files=list(changed_files),
        checks=selections,
        matched_rule_ids=[str(rid) for rid in raw["matched_rule_ids"]],
        used_unknown_default=bool(raw["used_unknown_default"]),
    )


def evaluate_publication(
    evidence: Optional[Mapping[str, Any]],
    *,
    profile: VerificationProfile,
    commit_sha: str,
    tree_hash: str,
    clean_tree: bool = True,
    changed_files: Sequence[str] = (),
) -> PublicationDecision:
    """Validate evidence semantics against current inputs, never its origin.

    A caller must independently authenticate a runner/controller channel.
    The producer string in JSON is diagnostic, not proof of that origin.

    Delegates to :func:`preloop.utils.verification_selection.evaluate_from_raw`
    so the publisher's in-container verifier and this typed wrapper cannot
    drift: there is one fail-closed decision.
    """

    def deny(
        reason: str, status: Literal["failed", "blocked"] = "blocked"
    ) -> PublicationDecision:
        return PublicationDecision(allowed=False, status=status, reason=reason)

    if not evidence:
        return deny("verification evidence missing")
    try:
        parsed = VerificationEvidence.model_validate(evidence)
    except ValueError:
        return deny("verification evidence is malformed")
    raw = evaluate_from_raw(
        parsed.model_dump(),
        profile=profile.model_dump(),
        commit_sha=commit_sha,
        tree_hash=tree_hash,
        clean_tree=clean_tree,
        changed_files=changed_files,
    )
    return PublicationDecision.model_validate(raw)
