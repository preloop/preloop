"""Validate standalone runtime plugin marketplace metadata."""

from __future__ import annotations

import json
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_hermes_manifest_is_standalone() -> None:
    manifest = json.loads((ROOT / "hermes-preloop" / "preloop-plugin.json").read_text())

    assert manifest["runtime"] == "hermes"
    assert "preloop" in manifest["name"]
    assert "agent-control" in manifest["keywords"]
    assert "network:wss" in manifest["permissions"]
    assert "agent:tool_approval" in manifest["permissions"]
    assert manifest["verification"]["command"]
    assert manifest["configSchema"]["path"] == "preloop.control"
    assert manifest.get("capabilities", {}).get("tool_approval") is True
    assert "pre_tool_call" in manifest.get("hooks", [])
    assert set(manifest["configSchema"]["required"]) == {
        "control_ws_url",
        "bearer_token",
        "runtime_principal_id",
    }


# OpenClaw 2026.7.2-beta.7 tightened PluginManifest: the ClawHub validator
# (plugin-inspector rule manifest-unknown-fields) rejects any top-level key
# outside the published PluginManifest type. Runtime/packaging metadata now
# lives under package.json "openclaw" instead, and configSchema must be a
# real JSON Schema object rather than a {path, required} descriptor.
#
# PROVENANCE of the list below. Do not hand-edit it; regenerate.
#   source:  github.com/openclaw/openclaw, tag v2026.7.2-beta.7 (commit
#            dabe1915), file src/plugins/manifest-types.ts, type PluginManifest
#   extract: the inspector's own parser, so this cannot drift from how ClawHub
#            reads the type:
#     node -e 'import("@openclaw/plugin-inspector/src/openclaw-target.js")
#       .then(m => console.log(m.parseTypeFields(
#         require("fs").readFileSync(SRC, "utf8"), "PluginManifest").join("\n")))'
#   count:   45 fields (asserted below, so a partial paste fails loudly)
#
# There are TWO upstream surfaces and they do not agree. Beware of comparing
# against the wrong one:
#   1. PluginManifest in the OpenClaw source tree (45 fields) - what the
#      inspector uses when given a real checkout via `--openclaw <path>`.
#   2. PluginManifestRecord in the packed npm tarball's .d.ts (66 fields at
#      2026.7.1-2) - what the inspector falls back to for `npm:openclaw@...`.
#      It is the loaded/registry-side record: it adds packaging and runtime
#      bookkeeping (packageName, rootDir, origin, manifestPath, hooks...) that
#      an author must never write, and it OMITS six fields that are legal in
#      the source type (catalog, dashboard, mcpServers, providerCatalogEntry,
#      requiresPlugins, uiHints).
# Because either surface can be the one ClawHub runs, a field we ship must be
# accepted by BOTH. test_openclaw_manifest_fields_accepted_by_both_surfaces
# enforces that intersection against the committed inspector report.
OPENCLAW_MANIFEST_SOURCE_TAG = "v2026.7.2-beta.7"
OPENCLAW_MANIFEST_ALLOWED_TOP_LEVEL_FIELDS = {
    "activation",
    "autoEnableWhenConfiguredProviders",
    "catalog",
    "channelConfigs",
    "channels",
    "cliBackends",
    "commandAliases",
    "configContracts",
    "configSchema",
    "contracts",
    "dashboard",
    "description",
    "enabledByDefault",
    "enabledByDefaultOnPlatforms",
    "icon",
    "id",
    "imageGenerationProviderMetadata",
    "kind",
    "legacyPluginIds",
    "mcpServers",
    "mediaUnderstandingProviderMetadata",
    "modelCatalog",
    "modelIdNormalization",
    "modelPricing",
    "modelSupport",
    "musicGenerationProviderMetadata",
    "name",
    "nonSecretAuthMarkers",
    "providerAuthAliases",
    "providerAuthChoices",
    "providerCatalogEntry",
    "providerEndpoints",
    "providerRequest",
    "providerUsageAuthEnvVars",
    "providers",
    "qaRunners",
    "requiresPlugins",
    "secretProviderIntegrations",
    "setup",
    "skills",
    "syntheticAuthRefs",
    "toolMetadata",
    "uiHints",
    "version",
    "videoGenerationProviderMetadata",
}


def test_openclaw_manifest_uses_only_supported_top_level_fields() -> None:
    manifest = json.loads(
        (ROOT / "openclaw-preloop" / "openclaw.plugin.json").read_text()
    )

    unknown = set(manifest) - OPENCLAW_MANIFEST_ALLOWED_TOP_LEVEL_FIELDS
    assert not unknown, f"unsupported openclaw manifest fields: {sorted(unknown)}"

    assert manifest["id"] == "preloop-plugin"
    assert manifest["name"]
    assert manifest["configSchema"]["type"] == "object"
    assert set(manifest["configSchema"]["required"]) == {
        "control_ws_url",
        "bearer_token",
        "runtime_principal_id",
    }


def test_openclaw_allowlist_has_the_pinned_field_count() -> None:
    """Guard the allowlist against a truncated or padded hand-edit.

    The count is the one number a regeneration must reproduce; if upstream
    changes it, the tag in OPENCLAW_MANIFEST_SOURCE_TAG must move with it.
    """
    assert OPENCLAW_MANIFEST_SOURCE_TAG == "v2026.7.2-beta.7"
    assert len(OPENCLAW_MANIFEST_ALLOWED_TOP_LEVEL_FIELDS) == 45, (
        "allowlist no longer matches the 45 fields of PluginManifest at "
        f"{OPENCLAW_MANIFEST_SOURCE_TAG}; regenerate it with the command in "
        "the provenance comment rather than editing by hand"
    )


def _inspector_report() -> dict:
    return json.loads(
        (
            ROOT / "openclaw-preloop" / "reports" / "plugin-inspector-report.json"
        ).read_text()
    )


def test_openclaw_manifest_fields_accepted_by_both_surfaces() -> None:
    """Every shipped manifest key must satisfy source type AND packed record.

    This is the test that actually catches drift, because it checks the
    manifest against a generated artifact rather than against a hand-kept
    list. The committed plugin-inspector report carries the field list the
    validator derived from the npm target (PluginManifestRecord); the
    allowlist above carries the source-tree type. A key legal under only one
    of them passes on one ClawHub configuration and warns on the other, which
    is exactly the failure this suite exists to prevent.
    """
    manifest_keys = set(
        json.loads((ROOT / "openclaw-preloop" / "openclaw.plugin.json").read_text())
    )
    target = _inspector_report()["targetOpenClaw"]

    assert target["status"] == "ok", (
        "the committed inspector report was generated without an OpenClaw "
        "target, so manifest-unknown-fields never ran and the report proves "
        "nothing; regenerate with `clawhub package validate . --openclaw <checkout>`"
    )
    report_fields = set(target["manifestFields"])
    assert report_fields, "inspector report carries no manifestFields"

    rejected_by_report = manifest_keys - report_fields
    assert not rejected_by_report, (
        "manifest keys absent from the inspector's field list "
        f"({target['configuredPath']}): {sorted(rejected_by_report)}"
    )
    rejected_by_source_type = manifest_keys - OPENCLAW_MANIFEST_ALLOWED_TOP_LEVEL_FIELDS
    assert not rejected_by_source_type, (
        "manifest keys absent from PluginManifest at "
        f"{OPENCLAW_MANIFEST_SOURCE_TAG}: {sorted(rejected_by_source_type)}"
    )


def test_openclaw_inspector_report_is_a_clean_run() -> None:
    """A stale or warning-carrying report must not sit in the repo unnoticed.

    0.2.0 shipped with a report whose target was "disabled": it recorded a
    pass that had skipped the very check the hub later failed on.
    """
    report = _inspector_report()

    assert report["status"] == "pass"
    assert report["summary"]["breakageCount"] == 0
    assert report["summary"]["warningCount"] == 0


def test_openclaw_package_preserves_agent_control_semantics() -> None:
    openclaw = json.loads((ROOT / "openclaw-preloop" / "package.json").read_text())[
        "openclaw"
    ]

    assert openclaw["configPath"] == "plugins.entries.preloop-plugin.config"
    assert openclaw["capabilities"]["tool_approval"] is True
    assert "before_tool_call" in openclaw["hooks"]
    assert "network:wss" in openclaw["permissions"]
    assert "agent:tool_approval" in openclaw["permissions"]
    assert openclaw["verification"]["command"]
    assert openclaw["install"]["npmSpec"] == "@preloop-ai/openclaw-plugin"
    assert openclaw["release"]["publishToClawHub"] is True


def test_runtime_plugins_do_not_use_server_plugin_directory() -> None:
    for plugin_dir in [ROOT / "hermes-preloop", ROOT / "openclaw-preloop"]:
        assert "runtime-plugins" in plugin_dir.parts
        assert "plugins" not in plugin_dir.relative_to(ROOT.parents[1]).parts


def test_openclaw_package_metadata_matches_manifest() -> None:
    package = json.loads((ROOT / "openclaw-preloop" / "package.json").read_text())
    manifest = json.loads(
        (ROOT / "openclaw-preloop" / "openclaw.plugin.json").read_text()
    )

    assert package["name"] == "@preloop-ai/openclaw-plugin"
    # OpenClaw's PluginManifest "name" is the human-readable display name;
    # ClawHub derives the package displayName from it. The npm package name
    # lives in package.json only.
    assert manifest["name"] == package["displayName"] == "Preloop"
    assert manifest["version"] == package["version"]
    assert package["bin"]["preloop-openclaw-plugin"] == "dist/index.js"
    assert package["scripts"]["build"]
    assert package["scripts"]["verify"] == "node dist/index.js verify"
    assert package["openclaw"]["extensions"] == ["./dist/index.js"]
    assert package["openclaw"]["compat"]["pluginApi"]
    assert package["openclaw"]["build"]["openclawVersion"]
    assert "openclaw.plugin.json" in package["files"]
    assert "openclaw" not in package.get("dependencies", {})
    assert "openclaw" not in package.get("peerDependencies", {})


def test_hermes_package_metadata_matches_manifest() -> None:
    pyproject = tomllib.loads((ROOT / "hermes-preloop" / "pyproject.toml").read_text())
    manifest = json.loads((ROOT / "hermes-preloop" / "preloop-plugin.json").read_text())
    project = pyproject["project"]

    assert project["name"] == "preloop-hermes-plugin"
    assert manifest["name"] == project["name"]
    assert manifest["version"] == project["version"]
    assert project["scripts"]["preloop-hermes-plugin"] == (
        "preloop_hermes_plugin.plugin:main"
    )
    assert pyproject["project"]["entry-points"]["hermes_agent.plugins"]["preloop"] == (
        "preloop_hermes_plugin.plugin"
    )
    assert manifest["entrypoint"] == "preloop_hermes_plugin.plugin"


def test_opencode_package_metadata_is_standalone() -> None:
    package = json.loads((ROOT / "opencode-preloop" / "package.json").read_text())

    assert package["name"] == "@preloop-ai/opencode-plugin"
    # The plugin is distributed via npm only: no marketplace consumes the
    # `opencode` manifest block, so it must not be present.
    assert "opencode" not in package
    # OpenCode has no marketplace manifest; everything lives in package.json.
    assert not list((ROOT / "opencode-preloop").glob("*.plugin.json"))
    assert "opencode" not in package.get("dependencies", {})
    assert "opencode" not in package.get("peerDependencies", {})


def test_publishing_guide_covers_both_marketplaces() -> None:
    guide = (ROOT / "PUBLISHING.md").read_text()

    assert "@preloop-ai/openclaw-plugin" in guide
    assert "npm publish --access public" in guide
    assert "openclaw.plugin.json" in guide
    assert "clawhub package publish" in guide
    assert "--source-repo" in guide
    assert "--source-commit" in guide
    assert "preloop-hermes-plugin" in guide
    assert "twine upload" in guide
    assert "hermes_agent.plugins" in guide
    assert "without the Preloop CLI" in guide


def test_readmes_include_cli_free_manual_tests() -> None:
    expectations = {
        ROOT / "openclaw-preloop" / "README.md": [
            "Manual Test Without Preloop CLI",
            "openclaw plugins install @preloop-ai/openclaw-plugin",
            "preloop-openclaw-plugin verify",
            "preloop-openclaw-plugin run",
        ],
        ROOT / "hermes-preloop" / "README.md": [
            "Manual Test Without Preloop CLI",
            "pip install preloop-hermes-plugin",
            "preloop-hermes-plugin verify",
            "preloop-hermes-plugin run",
        ],
    }

    for readme_path, snippets in expectations.items():
        readme = readme_path.read_text()
        for snippet in snippets:
            assert snippet in readme
