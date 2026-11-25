"""Verified Claude Code shapes, checked against vendor docs on 2026-11-25.

The minimum is our conservative supported floor, not a claim about the release
that introduced each key. The reference does not date every key or drop-in
support. Validate older clients independently before lowering this floor.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class VerifiedKey:
    """Evidence required before the renderer may emit a setting."""

    shape: str
    doc_url: str
    checked_on: str = "2026-11-25"
    minimum_version: str = "2.1.332"


REFERENCE = "https://code.claude.com/docs/en/settings-reference"
VERIFIED_KEYS = {
    "enabledPlugins": VerifiedKey(
        "object[plugin@marketplace, boolean]", REFERENCE + "#enabledplugins"
    ),
    "strictKnownMarketplaces": VerifiedKey(
        "array[marketplace source]", REFERENCE + "#strictknownmarketplaces"
    ),
    "blockedMarketplaces": VerifiedKey(
        "array[marketplace source]", REFERENCE + "#blockedmarketplaces"
    ),
    "extraKnownMarketplaces": VerifiedKey(
        "object[name, {source: concrete source}]", REFERENCE + "#extraknownmarketplaces"
    ),
    "allowManagedHooksOnly": VerifiedKey(
        "boolean", REFERENCE + "#allowmanagedhooksonly"
    ),
    "strictPluginOnlyCustomization": VerifiedKey(
        "true | array[skills|agents|hooks|mcp]; emit only [skills]",
        REFERENCE + "#strictpluginonlycustomization",
    ),
    "disableSkillShellExecution": VerifiedKey(
        "boolean", REFERENCE + "#disableskillshellexecution"
    ),
    "disableSideloadFlags": VerifiedKey("boolean", REFERENCE + "#disablesideloadflags"),
}
# File channel: https://code.claude.com/docs/en/managed-settings#split-a-file-based-policy-across-teams
# Checked 2026-11-25: system managed-settings.d/*.json, alphabetical after main file.
DROP_IN_MINIMUM_VERSION = "2.1.332"
