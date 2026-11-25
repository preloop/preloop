"""Pure, gated rendering of account policy and name approvals; no database I/O."""

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from preloop.schemas.extension_policy import (
    EnforcementPolicy,
    OmittedKey,
    Platform,
    RenderedOverlay,
    RenderInputs,
)
from preloop.services.extension_policy_keys import (
    DROP_IN_MINIMUM_VERSION,
    VERIFIED_KEYS,
)

PLUGIN_ID = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+$")
TARGETS = {
    "darwin": "/Library/Application Support/ClaudeCode/managed-settings.d/50-preloop-extensions.json",
    "linux": "/etc/claude-code/managed-settings.d/50-preloop-extensions.json",
    "windows": r"C:\Program Files\ClaudeCode\managed-settings.d\50-preloop-extensions.json",
}


class Approval(Protocol):
    """Only these decision fields are consumed; content pins are not enforceable."""

    agent_kind: str
    extension_kind: str
    extension_id: str
    approval_state: str
    updated_at: datetime


class RenderRefusalError(ValueError):
    """A policy cannot safely produce a deployable overlay."""

    def __init__(self, status_code: int, message: str) -> None:
        """Carry the HTTP refusal without coupling rendering to FastAPI."""
        super().__init__(message)
        self.status_code = status_code


def render_overlay(
    policy: EnforcementPolicy,
    approvals: Sequence[Approval],
    *,
    platform: Platform,
    hook_registered: bool = False,
) -> RenderedOverlay:
    """Render exact settings bytes or refuse an empty/unsafe restriction.

    Args:
        policy: Validated account policy.
        approvals: Durable name decisions, not per-device effective states.
        platform: Destination operating system.
        hook_registered: Active hook bundle exists for this account and platform.

    Returns:
        Canonical settings and manifest inputs; no installation claim.

    Raises:
        RenderRefusalError: Empty policy, marketplace lockout or missing hook bundle.
    """
    candidates: dict[str, object] = {}
    omitted: list[OmittedKey] = []
    rows = [a for a in approvals if a.agent_kind == "claude_code"]
    options = policy.options
    if options.block_unapproved_plugins:
        denied: dict[str, bool] = {}
        for row in rows:
            if row.extension_kind != "plugin" or row.approval_state != "unapproved":
                continue
            if not PLUGIN_ID.fullmatch(row.extension_id):
                omitted.append(
                    OmittedKey(
                        key="enabledPlugins",
                        subject=row.extension_id,
                        reason="not a marketplace plugin id",
                    )
                )
            else:
                denied[row.extension_id] = False
        if denied:
            candidates["enabledPlugins"] = denied
    if options.restrict_marketplaces:
        entries = sorted(policy.marketplaces, key=lambda m: m.name)
        allowed = [m for m in entries if m.state == "allowed"]
        if not allowed:
            raise RenderRefusalError(
                422,
                "An empty marketplace allow-list locks out every source, including the official marketplace; add an allowed marketplace first",
            )
        candidates["strictKnownMarketplaces"] = [
            m.source.model_dump(exclude_none=True, by_alias=True) for m in allowed
        ]
        candidates["blockedMarketplaces"] = [
            m.source.model_dump(exclude_none=True, by_alias=True)
            for m in entries
            if m.state == "blocked"
        ]
        registered = {
            m.name: {"source": m.source.model_dump(exclude_none=True, by_alias=True)}
            for m in allowed
            if m.register_on_devices
        }
        if registered:
            candidates["extraKnownMarketplaces"] = registered
    if options.managed_hooks_only:
        if not hook_registered:
            raise RenderRefusalError(
                409,
                f"Register and deploy the claude-code managed hook bundle for {platform} before enabling managed_hooks_only; user-installed Preloop hooks would be blocked",
            )
        candidates["allowManagedHooksOnly"] = True
    if options.block_user_project_skills:
        candidates["strictPluginOnlyCustomization"] = ["skills"]
    if options.disable_skill_shell:
        candidates["disableSkillShellExecution"] = True
    if options.disable_sideload_flags:
        candidates["disableSideloadFlags"] = True
    settings = {}
    for key, value in candidates.items():
        if key in VERIFIED_KEYS:
            settings[key] = value
        else:
            omitted.append(OmittedKey(key=key, reason="shape unverified"))
    if not settings:
        raise RenderRefusalError(409, "nothing to enforce")
    encoded = json.dumps(settings, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    minimum = max(
        [DROP_IN_MINIMUM_VERSION]
        + [VERIFIED_KEYS[k].minimum_version for k in settings],
        key=lambda v: tuple(map(int, v.split("."))),
    )
    return RenderedOverlay(
        settings_json=encoded,
        policy_revision_hash=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        keys=sorted(settings),
        omitted=sorted(omitted, key=lambda o: (o.key, o.subject or "", o.reason)),
        inputs=RenderInputs(
            policy_revision=policy.revision,
            approval_count=len(rows),
            approvals_updated_at=max((a.updated_at for a in rows), default=None),
        ),
        platform=platform,
        target_path=TARGETS[platform],
        minimum_claude_code_version=minimum,
    )
