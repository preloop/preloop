"""Validate standalone runtime plugin marketplace metadata."""

from __future__ import annotations

import json
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_plugin_manifests_are_standalone() -> None:
    manifests = [
        ROOT / "hermes-preloop" / "preloop-plugin.json",
        ROOT / "openclaw-preloop" / "openclaw.plugin.json",
    ]

    for manifest_path in manifests:
        manifest = json.loads(manifest_path.read_text())

        assert manifest["runtime"] in {"hermes", "openclaw"}
        assert "preloop" in manifest["name"]
        assert "agent-control" in manifest["keywords"]
        assert "network:wss" in manifest["permissions"]
        assert "agent:tool_approval" in manifest["permissions"]
        assert manifest["verification"]["command"]
        if manifest["runtime"] == "openclaw":
            assert manifest["id"] == "preloop-plugin"
            assert manifest["configSchema"]["path"] == (
                "plugins.entries.preloop-plugin.config"
            )
            assert manifest.get("capabilities", {}).get("tool_approval") is True
            assert "before_tool_call" in manifest.get("hooks", [])
        else:
            assert manifest["configSchema"]["path"] == "preloop.control"
            assert manifest.get("capabilities", {}).get("tool_approval") is True
            assert "pre_tool_call" in manifest.get("hooks", [])
        assert set(manifest["configSchema"]["required"]) == {
            "control_ws_url",
            "bearer_token",
            "runtime_principal_id",
        }


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
    assert manifest["name"] == package["name"]
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
